"""Workday career-site adapter and location/job URL helpers."""

import logging
from urllib.parse import urlencode, urljoin, urlparse

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class WorkdayAdapter:
    def _workday(self, url: str, locations: list[str] | None = None) -> list[Job]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if (parsed.hostname or "").endswith("myworkdaysite.com"):
            try:
                recruiting_index = next(i for i, part in enumerate(parts) if part.casefold() == "recruiting")
                tenant, site = parts[recruiting_index + 1:recruiting_index + 3]
            except (StopIteration, ValueError):
                raise ValueError("Workday URL must include /recruiting/<tenant>/<career-site>") from None
            locale_and_site_path = "/" + "/".join(parts[:recruiting_index + 3])
        else:
            if not parts:
                raise ValueError("Workday URL must include a career site path")
            site_index = next((i for i, part in enumerate(parts) if part.lower() not in {"en-us", "en-gb", "en-ca", "fr-fr", "de-de"}), 0)
            site = parts[site_index]
            tenant = parsed.hostname.split(".")[0]
            locale_and_site_path = "/" + "/".join(parts[:site_index + 1])
            if site_index == 0:
                locale_and_site_path = f"/en-US/{site}"
        api = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"
        result: list[Job] = []
        seen_job_links: set[str] = set()
        seen_page_keys: set[tuple[str, ...]] = set()
        applied_facets = {}
        offset = 0
        page_number = 0
        first_payload = None
        location_facets = []
        if locations:
            response = self.session.post(api, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}, timeout=30)
            response.raise_for_status()
            first_payload = response.json()
            if not isinstance(first_payload, dict) or not isinstance(first_payload.get("facets"), list):
                raise PortalStructureError("Workday response has no recognizable location facets; portal may need maintenance")
            location_facets = first_payload["facets"]
            location_ids = _workday_location_ids(location_facets, locations)
            if not location_ids:
                LOG.info("Workday has no current location facet matching %s at %s", ", ".join(locations), url)
                return []
            applied_facets = {"locations": location_ids}
        while True:
            if offset == 0 and first_payload is not None and not applied_facets:
                payload = first_payload
            else:
                response = self.session.post(
                    api,
                    json={"appliedFacets": applied_facets, "limit": 20, "offset": offset, "searchText": ""},
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if offset == 0 and first_payload is not None:
                    first_payload = None
            if not isinstance(payload, dict):
                raise PortalStructureError("Workday response has an unrecognizable search shape; portal may need maintenance")
            items = payload.get("jobPostings")
            if not isinstance(items, list):
                raise PortalStructureError("Workday response has no recognizable job postings; portal may need maintenance")
            if not items:
                break

            # Workday can return total=0 for a valid page after the first one.
            # Treat the postings themselves as the pagination signal, and guard
            # against a server that ignores offsets by detecting repeated pages.
            page_key = tuple(
                str(item.get("externalPath") or item.get("externalUrl") or item.get("title") or "")
                for item in items
            )
            if page_key in seen_page_keys:
                LOG.warning("Workday repeated page at offset %s for %s; stopping pagination", offset, url)
                break
            seen_page_keys.add(page_key)
            for item in items:
                title = item.get("title", "").strip()
                path = item.get("externalPath") or item.get("externalUrl") or ""
                location = item.get("locationsText") or item.get("location", "")
                link = _workday_job_url(
                    parsed,
                    locale_and_site_path,
                    path,
                    _workday_location_id_for_job(location_facets, location, location_ids)
                    if locations and site.casefold() == "apexgroupcareers" else "",
                    strip_location_segment=site.casefold() == "apexgroupcareers",
                )
                if title and link and link not in seen_job_links:
                    seen_job_links.add(link)
                    result.append(Job(title, link, location, " ".join(item.get("bulletFields", []))))
            offset += len(items)
            page_number += 1
            if page_number >= 100:
                LOG.warning("Workday reached pagination safety limit for %s", url)
                break
        return result


def _workday_location_ids(facets, locations: list[str]) -> list[str]:
    """Resolve configured location labels to the IDs required by Workday CXS."""
    options = _workday_location_options(facets)
    selected = []
    for location in locations:
        normalized = location.casefold().strip()
        exact = [identifier for label, identifier in options if label.casefold() == normalized]
        matches = exact or [identifier for label, identifier in options if normalized in label.casefold()]
        for identifier in matches:
            if identifier not in selected:
                selected.append(identifier)
    return selected



def _workday_location_options(facets):
    options = []

    def collect(node):
        if isinstance(node, dict):
            if node.get("descriptor") and node.get("id"):
                options.append((str(node["descriptor"]).strip(), str(node["id"])))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(facets)
    return options


def _workday_location_id_for_job(facets, job_location: str, selected_ids: list[str]) -> str:
    normalized = job_location.casefold().strip()
    if not normalized:
        return ""
    options = [(label, identifier) for label, identifier in _workday_location_options(facets) if identifier in selected_ids]
    exact = [identifier for label, identifier in options if label.casefold() == normalized]
    if exact:
        return exact[0]
    matches = [identifier for label, identifier in options if label.casefold() in normalized or normalized in label.casefold()]
    return matches[0] if matches else ""


def _workday_job_url(
    parsed,
    locale_and_site_path: str,
    external_path: str,
    location_id: str = "",
    *,
    strip_location_segment: bool = False,
) -> str:
    """Build a clickable Workday job page from the CXS externalPath."""
    origin = f"{parsed.scheme}://{parsed.netloc}"
    external = urlparse(external_path)
    path = external.path or external_path
    segments = [segment for segment in path.split("/") if segment]
    try:
        job_index = next(index for index, segment in enumerate(segments) if segment.casefold() == "job")
    except StopIteration:
        job_index = -1
    if job_index >= 0 and job_index + 1 < len(segments):
        # Apex uses the requisition slug and a location query facet. Other
        # Workday boards, including Northern Trust, keep the location in path.
        route_segments = segments[job_index:]
        if strip_location_segment and len(route_segments) > 2:
            route_segments = [route_segments[0], route_segments[-1]]
        route = "/" + "/".join(route_segments)
        link = origin + route
    elif external_path.startswith("http"):
        link = external_path
    else:
        link = urljoin(origin + locale_and_site_path.rstrip("/") + "/", path)
    query = external.query
    if location_id:
        query = urlencode({"locations": location_id})
    if query:
        link += "&" + query if "?" in link else "?" + query
    return link



"""Algolia adapter for bp job listings."""

import logging
import re
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class BpAdapter:
    def _bp(self, url: str, locations: list[str] | None = None) -> list[Job]:
        """Read bp's public Algolia job index, including its location refinements."""
        parsed = urlparse(url)
        endpoint = f"https://UM59DWRPA1-dsn.algolia.net/1/indexes/production_bp_jobs/query"
        headers = {
            "X-Algolia-Application-Id": "UM59DWRPA1",
            "X-Algolia-API-Key": "0f3e2a2d4ca48d7cfeba5e0ea876e172",
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }
        body = {"query": "", "page": 0, "hitsPerPage": 100, "facets": ["location_list"]}

        if locations:
            response = self.session.post(endpoint, json=body, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("facets"), dict):
                raise PortalStructureError("bp response has no recognizable location facets; portal may need maintenance")
            facets = payload.get("facets", {}).get("location_list", {})
            selected = _bp_location_facets(facets, locations)
            if not selected:
                LOG.info("bp has no location facet matching %s", ", ".join(locations))
                return []
            body["facetFilters"] = [[f"location_list:{label}" for label in selected]]

        result: dict[str, Job] = {}
        page = 0
        while page < 100:
            body["page"] = page
            response = self.session.post(endpoint, json=body, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            hits = payload.get("hits")
            if not isinstance(hits, list):
                raise PortalStructureError("bp response has no recognizable jobs list; portal may need maintenance")
            for hit in hits:
                codes = hit.get("job_code") or []
                code = codes[0] if isinstance(codes, list) and codes else codes
                if not code:
                    continue
                title = str(hit.get("title", "")).strip()
                locations_text = hit.get("location") or []
                if isinstance(locations_text, list):
                    location = " ".join(str(value).strip() for value in locations_text if value)
                else:
                    location = str(locations_text).strip()
                summary = ((hit.get("_highlightResult") or {}).get("summary") or {}).get("value", "")
                link = f"{parsed.scheme}://{parsed.netloc}/job-description/{quote(str(code), safe='')}"
                if title:
                    result.setdefault(link, Job(title, link, location, _bp_plain(summary)))
            page += 1
            if page >= int(payload.get("nbPages", 0)) or not hits:
                break
        else:
            LOG.warning("bp listings reached the 100-page safety limit for %s", url)
        return list(result.values())

def _bp_location_facets(facets: dict, locations: list[str]) -> list[str]:
    """Resolve dashboard location text to the location values in bp's index."""
    labels = [str(label).strip() for label in facets if str(label).strip()]
    selected = []
    for location in locations:
        query = re.sub(r"[,\s]+(?:india|maharashtra)$", "", location.casefold().strip()).strip()
        if not query:
            continue
        exact = [label for label in labels if label.casefold() == location.casefold().strip()]
        matches = exact or [label for label in labels if query in label.casefold()]
        for label in matches:
            if label not in selected:
                selected.append(label)
    return selected


def _bp_plain(value):
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())

"""IBM BrassRing adapter for UBS career listings."""

import json
import logging
import re
from copy import deepcopy
from html import unescape
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class UbsAdapter:
    def _ubs(self, url: str, locations: list[str] | None = None) -> list[Job]:
        """Read UBS's IBM BrassRing listings and apply its City facet when requested."""
        response = self.session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        preload = soup.select_one("#preLoadJSON")
        if not preload or not preload.get("value"):
            raise PortalStructureError("UBS careers page has no recognizable search results; portal may need maintenance")
        try:
            data = json.loads(preload["value"])
            search_response = data.get("searchResultsResponse") or {}
            smart_search = json.loads(data.get("SmartSearchJSONValue") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise PortalStructureError("UBS careers page returned unrecognizable search data; portal may need maintenance") from exc
        if not isinstance(search_response, dict) or not isinstance(search_response.get("Jobs"), dict):
            raise PortalStructureError("UBS careers page has no recognizable job results; portal may need maintenance")

        if not locations:
            return _ubs_jobs(search_response.get("Jobs", {}).get("Job", []), response.url)

        facets = (search_response.get("Facets") or {}).get("Facet", [])
        city_facet = next(
            (facet for facet in facets if facet.get("Description", "").casefold() == "city" or facet.get("Name", "").casefold() == "formtext2"),
            None,
        )
        if not city_facet:
            raise PortalStructureError("UBS careers page has no recognizable City facet; portal may need maintenance")

        token = soup.select_one('[name="__RequestVerificationToken"]')
        cookie_value = soup.select_one("#CookieValue")
        link_id = soup.select_one("#linkId")
        if not token or not cookie_value:
            raise PortalStructureError("UBS careers page is missing its search session token; portal may need maintenance")

        city_options = city_facet.get("Options", [])
        found: dict[str, Job] = {}
        for requested in locations:
            query = requested.strip().casefold()
            query = re.sub(r"[,\s]+(?:india|maharashtra)$", "", query).strip()
            matches = [
                option for option in city_options
                if query and (
                    query == str(option.get("OptionName", "")).casefold()
                    or query == str(option.get("OptionValue", "")).casefold()
                    or query in str(option.get("OptionName", "")).casefold()
                )
            ]
            if not matches:
                LOG.info("UBS has no City facet matching %s", requested)
                continue

            for option in matches:
                selected_facet = deepcopy(city_facet)
                selected_facet["Options"] = [dict(option, Selected=True)]
                result = self._ubs_search_page(
                    response.url,
                    smart_search,
                    token.get("value", ""),
                    cookie_value.get("value", ""),
                    link_id.get("value", "") if link_id else "",
                    selected_facet,
                    1,
                )
                for job in _ubs_jobs(result.get("Jobs", {}).get("Job", []), response.url):
                    found.setdefault(job.url, job)

        return list(found.values())

    def _ubs_search_page(self, page_url, smart_search, token, cookie_value, link_id, facet, page_number):
        parsed = urlparse(page_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
        body = {
            "partnerId": str(smart_search.get("PartnerId", "")),
            "siteId": str(smart_search.get("SiteId", "")),
            "keyword": "",
            "location": "",
            "keywordCustomSolrFields": smart_search.get("KeywordCustomSolrFields", ""),
            "locationCustomSolrFields": smart_search.get("LocationCustomSolrFields", ""),
            "linkId": link_id,
            "Latitude": 0,
            "Longitude": 0,
            "facetfilterfields": {"Facet": [facet]},
            "powersearchoptions": {"PowerSearchOption": []},
            "SortType": "LastUpdated",
            "pageNumber": page_number,
            "encryptedSessionValue": cookie_value,
        }
        result = self.session.post(endpoint, json=body, headers={"RFT": token}, timeout=45)
        result.raise_for_status()
        return result.json()

def _ubs_jobs(items, base_url: str) -> list[Job]:
    """Convert the question/value records in UBS's BrassRing response to jobs."""
    found: dict[str, Job] = {}
    for item in items or []:
        fields = {
            str(question.get("QuestionName", "")).casefold(): question.get("Value", "")
            for question in item.get("Questions", [])
        }
        title = unescape(str(fields.get("jobtitle", ""))).strip()
        link = urljoin(base_url, item.get("Link", ""))
        city = " ".join(str(fields.get("formtext2", "") or "").split())
        country = " ".join(str(fields.get("formtext23", "") or "").split())
        location = ", ".join(part for part in (city, country) if part)
        description = _ubs_plain(fields.get("jobdescription", ""))
        _ubs_add(found, title, link, location, description)
    return list(found.values())


def _ubs_add(target, title, url, location="", description=""):
    title = " ".join(str(title or "").split())
    if title and url.startswith(("http://", "https://")):
        target.setdefault(url.split("#", 1)[0], Job(title, url.split("#", 1)[0], location.strip(), description.strip()))


def _ubs_plain(value):
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())

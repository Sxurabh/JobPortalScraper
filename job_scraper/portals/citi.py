"""TalentBrew adapter for Citi career listings."""

import logging
import re
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class CitiAdapter:
    def _citi(self, url: str, locations: list[str] | None = None) -> list[Job]:
        """Read Citi's TalentBrew results and apply its location autocomplete."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        search_url = f"{origin}/search-jobs"
        page_response = self.session.get(search_url, timeout=30)
        page_response.raise_for_status()
        soup = BeautifulSoup(page_response.text, "html.parser")
        results = soup.select_one("#search-results")
        if not results:
            raise PortalStructureError("Citi careers page has no recognizable search results; portal may need maintenance")

        location_options = []
        if locations:
            for location in locations:
                query = re.sub(r"[,\s]+(?:india|maharashtra)$", "", location.casefold().strip()).strip()
                if not query:
                    continue
                response = self.session.get(
                    f"{origin}/search-jobs/locations",
                    params={"term": query},
                    headers={"Referer": search_url, "X-Requested-With": "XMLHttpRequest"},
                    timeout=30,
                )
                response.raise_for_status()
                suggestions = response.json()
                match = _citi_location_option(suggestions, query)
                if match and match not in location_options:
                    location_options.append(match)
                elif not match:
                    LOG.info("Citi has no location suggestion matching %s", location)
            if not location_options:
                return []

        found: dict[str, Job] = {}
        for location_option in location_options or [None]:
            page = 1
            total_pages = 1
            while page <= total_pages and page <= 500:
                body = {
                    "ActiveFacetID": 0,
                    "CurrentPage": page,
                    "RecordsPerPage": 100,
                    "TotalPages": 0,
                    "TotalResults": 0,
                    "TotalContentResults": 0,
                    "Distance": 50,
                    "RadiusUnitType": 2,
                    "Keywords": "",
                    "Location": location_option.get("value", "") if location_option else "",
                    "Latitude": location_option.get("lat") if location_option else None,
                    "Longitude": location_option.get("lon") if location_option else None,
                    "ShowRadius": bool(location_option),
                    "IsPagination": "False",
                    "CustomFacetName": "",
                    "FacetTerm": "",
                    "FacetType": None,
                    "FacetFilters": [],
                    "SearchResultsModuleName": "Search Results",
                    "SearchFiltersModuleName": None,
                    "SortCriteria": 5,
                    "SortDirection": 1,
                    "SearchType": 1 if location_option else int(results.get("data-search-type", 5)),
                    "SearchResultType": 0,
                    "CategoryFacetTerm": None,
                    "CategoryFacetType": None,
                    "LocationFacetTerm": None,
                    "LocationFacetType": None,
                    "KeywordType": None,
                    "LocationType": str(location_option.get("lt")) if location_option else None,
                    "LocationPath": location_option.get("lp") if location_option else None,
                    "OrganizationIds": "287",
                    "RefinedKeywords": [],
                    "PostalCode": "",
                    "ResultsType": 0,
                }
                response = self.session.post(
                    f"{origin}/search-jobs/resultspost",
                    json=body,
                    headers={"Referer": search_url, "X-Requested-With": "XMLHttpRequest"},
                    timeout=40,
                )
                response.raise_for_status()
                payload = response.json()
                html = payload.get("results", "")
                result_soup = BeautifulSoup(html, "html.parser")
                page_results = result_soup.select_one("#search-results")
                cards = result_soup.select(".sr-job-item")
                if payload.get("hasJobs") and not cards:
                    raise PortalStructureError("Citi search response has no recognizable job cards; portal may need maintenance")
                if page_results:
                    total_pages = max(1, int(page_results.get("data-total-pages", total_pages)))
                elif not payload.get("hasJobs"):
                    break

                for card in cards:
                    anchor = card.select_one("a.sr-job-item__link[href]")
                    if not anchor:
                        continue
                    title = anchor.get_text(" ", strip=True)
                    link = urljoin(origin, anchor["href"])
                    location_node = card.select_one(".sr-job-location")
                    job_location = location_node.get_text(" ", strip=True) if location_node else ""
                    if location_options and not any(
                        option.get("city", "").casefold() in job_location.casefold()
                        or option.get("value", "").casefold() in job_location.casefold()
                        for option in location_options
                    ):
                        continue
                    if title and link:
                        found.setdefault(link, Job(title, link, job_location, ""))
                page += 1
            if page > 500:
                LOG.warning("Citi listings reached the 500-page safety limit for %s", url)
        return list(found.values())

def _citi_location_option(suggestions, query: str):
    if not isinstance(suggestions, list):
        return None
    normalized = query.casefold().strip()
    exact = [
        option for option in suggestions
        if str(option.get("value", "")).casefold().strip() == normalized
        or str(option.get("city", "")).casefold().strip() == normalized
    ]
    if exact:
        return exact[0]
    starts = [
        option for option in suggestions
        if str(option.get("city", "")).casefold().startswith(normalized)
        or str(option.get("value", "")).casefold().startswith(normalized)
    ]
    if starts:
        return starts[0]
    contains = [option for option in suggestions if normalized in str(option.get("value", "")).casefold()]
    return contains[0] if contains else None

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
        description = _plain(fields.get("jobdescription", ""))
        _add(found, title, link, location, description)
    return list(found.values())

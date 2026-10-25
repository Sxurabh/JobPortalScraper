import json
import logging
import re
from copy import deepcopy
from html import unescape
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Job

LOG = logging.getLogger(__name__)
HEADERS = {"User-Agent": "JobPortalScraper/0.1 (+personal job alerts)"}


class PortalScraper:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str, locations: list[str] | None = None) -> list[Job]:
        lowered = url.lower()
        if "careers.statestreet.com" in lowered:
            return self._statestreet(url, locations or [])
        if "careers.fisglobal.com" in lowered:
            return self._fisglobal(url, locations or [])
        if "jobs.ubs.com" in lowered:
            return self._ubs(url, locations or [])
        if "careers.bp.com" in lowered:
            return self._bp(url, locations or [])
        if "jobs.citi.com" in lowered:
            return self._citi(url, locations or [])
        if "greenhouse.io" in lowered:
            return self._greenhouse(url)
        if "myworkdayjobs.com" in lowered or "myworkdaysite.com" in lowered:
            return self._workday(url, locations or [])
        return self._generic(url)

    def fetch_company(self, company) -> list[Job]:
        """Fetch using company-specific portal filters when supported."""
        return self.fetch(company.url, locations=company.locations)

    def _statestreet(self, url: str, locations: list[str]) -> list[Job]:
        """Read State Street's JavaScript-rendered Phenom search results."""
        parsed = urlparse(url)
        board_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/global/en/search-results")
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "State Street requires its JavaScript listings. Install the browser extra with "
                "'python -m pip install -e \".[browser]\"' and run "
                "'python -m playwright install chromium'."
            )

        locations = locations or [""]
        result: dict[str, Job] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for location in locations:
                    page = browser.new_page()
                    page_url = f"{board_url}?from=0&s=1"
                    page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.locator('a[href*="/global/en/job/"]').first.wait_for(timeout=20000)
                    except PlaywrightTimeoutError:
                        raise RuntimeError("State Street search page loaded without job cards")

                    if location:
                        page.locator("#CityAccordion").evaluate("element => element.click()")
                        checkbox = page.locator("input[type='checkbox']").evaluate_all(
                            "(items, city) => items.find(item => (item.getAttribute('aria-label') || '').startsWith(city + ' ')) || null",
                            location,
                        )
                        if not checkbox:
                            LOG.info("State Street has no City facet option for %s", location)
                            page.close()
                            continue
                        page.locator("input[type='checkbox']").evaluate_all(
                            "(items, city) => { const item = items.find(el => (el.getAttribute('aria-label') || '').startsWith(city + ' ')); item.checked = true; item.dispatchEvent(new Event('change', {bubbles: true})); }",
                            location,
                        )
                        page.wait_for_timeout(2500)
                        LOG.debug("State Street selected City filter %s; page URL is %s", location, page.url)

                    for page_number in range(200):
                        html = page.content()
                        page_jobs = _parse_statestreet_html(html, page.url)
                        LOG.debug("State Street %s page %d yielded %d cards", location or "all locations", page_number + 1, len(page_jobs))
                        for job in page_jobs:
                            if location and not job.location:
                                job = Job(job.title, job.url, location, job.description)
                            if location and location.casefold() not in job.location.casefold():
                                continue
                            result.setdefault(job.url, job)

                        next_link = page.locator('a[aria-label="View next page"]')
                        if not next_link.count() or not next_link.get_attribute("href"):
                            LOG.debug("State Street %s has no next page after page %d", location or "all locations", page_number + 1)
                            break
                        old_first = page.locator('a[href*="/global/en/job/"]').first.get_attribute("href")
                        next_link.evaluate("element => element.click()")
                        try:
                            page.wait_for_function(
                                "oldHref => { const link = document.querySelector('a[href*=\"/global/en/job/\"]'); return link && link.getAttribute('href') !== oldHref; }",
                                arg=old_first,
                                timeout=20000,
                            )
                        except PlaywrightTimeoutError:
                            break
                    else:
                        LOG.warning("State Street listings for %s reached the 200-page safety limit", location or "all locations")
                    page.close()
            finally:
                browser.close()
        return list(result.values())

    def _fisglobal(self, url: str, locations: list[str]) -> list[Job]:
        """Read FIS's Phenom listings, using its City facet for configured places."""
        parsed = urlparse(url)
        board_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/us/en/search-results")
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "FIS requires its JavaScript listings. Install the browser extra with "
                "'python -m pip install -e \".[browser]\"' and run "
                "'python -m playwright install chromium'."
            )

        locations = locations or [""]
        result: dict[str, Job] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for location in locations:
                    page = browser.new_page()
                    page.goto(f"{board_url}?from=0&s=1", wait_until="domcontentloaded", timeout=45000)
                    job_links = page.locator('a[href*="/us/en/job/"]')
                    try:
                        job_links.first.wait_for(timeout=20000)
                    except PlaywrightTimeoutError:
                        raise RuntimeError("FIS search page loaded without job cards")

                    if location:
                        city_facet = page.locator("#CityAccordion")
                        if city_facet.count():
                            city_facet.evaluate("element => element.click()")
                        checkbox = page.locator("input[type='checkbox']").evaluate_all(
                            "(items, city) => items.find(item => (item.getAttribute('aria-label') || '').startsWith(city + ' ')) || null",
                            location,
                        )
                        if not checkbox:
                            LOG.info("FIS has no City facet option for %s", location)
                            page.close()
                            continue
                        page.locator("input[type='checkbox']").evaluate_all(
                            "(items, city) => { const item = items.find(el => (el.getAttribute('aria-label') || '').startsWith(city + ' ')); item.checked = true; item.dispatchEvent(new Event('change', {bubbles: true})); }",
                            location,
                        )
                        page.wait_for_timeout(2000)

                    for page_number in range(200):
                        page_jobs = _parse_fisglobal_html(page.content(), page.url)
                        for job in page_jobs:
                            if location and location.casefold() not in job.location.casefold():
                                continue
                            result.setdefault(job.url, job)
                        next_link = page.locator('a[aria-label="View next page"]')
                        if not next_link.count() or not next_link.get_attribute("href"):
                            break
                        old_first = page.locator('a[href*="/us/en/job/"]').first.get_attribute("href")
                        next_link.evaluate("element => element.click()")
                        try:
                            page.wait_for_function(
                                "oldHref => { const link = document.querySelector('a[href*=\"/us/en/job/\"]'); return link && link.getAttribute('href') !== oldHref; }",
                                arg=old_first,
                                timeout=20000,
                            )
                        except PlaywrightTimeoutError:
                            break
                    else:
                        LOG.warning("FIS listings for %s reached the 200-page safety limit", location or "all locations")
                    page.close()
            finally:
                browser.close()
        return list(result.values())

    def _greenhouse(self, url: str) -> list[Job]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError(f"Could not identify Greenhouse board in {url}")
        board = parts[0]
        return self._greenhouse_board(board, "https://boards-api.greenhouse.io", content=True)

    def _ubs(self, url: str, locations: list[str] | None = None) -> list[Job]:
        """Read UBS's IBM BrassRing listings and apply its City facet when requested."""
        response = self.session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        preload = soup.select_one("#preLoadJSON")
        if not preload or not preload.get("value"):
            raise RuntimeError("UBS careers page did not include its search results")
        try:
            data = json.loads(preload["value"])
            search_response = data.get("searchResultsResponse") or {}
            smart_search = json.loads(data.get("SmartSearchJSONValue") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("UBS careers page returned invalid search data") from exc

        if not locations:
            return _ubs_jobs(search_response.get("Jobs", {}).get("Job", []), response.url)

        facets = (search_response.get("Facets") or {}).get("Facet", [])
        city_facet = next(
            (facet for facet in facets if facet.get("Description", "").casefold() == "city" or facet.get("Name", "").casefold() == "formtext2"),
            None,
        )
        if not city_facet:
            LOG.info("UBS has no City facet at %s", url)
            return []

        token = soup.select_one('[name="__RequestVerificationToken"]')
        cookie_value = soup.select_one("#CookieValue")
        link_id = soup.select_one("#linkId")
        if not token or not cookie_value:
            raise RuntimeError("UBS careers page is missing its search session token")

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
            facets = response.json().get("facets", {}).get("location_list", {})
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
            hits = payload.get("hits", [])
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
                    result.setdefault(link, Job(title, link, location, _plain(summary)))
            page += 1
            if page >= int(payload.get("nbPages", 0)) or not hits:
                break
        else:
            LOG.warning("bp listings reached the 100-page safety limit for %s", url)
        return list(result.values())

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
            raise RuntimeError("Citi careers page did not include its search results")

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
                if page_results:
                    total_pages = max(1, int(page_results.get("data-total-pages", total_pages)))
                elif not payload.get("hasJobs"):
                    break

                for card in result_soup.select(".sr-job-item"):
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

    def _greenhouse_board(self, board: str, api_origin: str, *, content: bool = False) -> list[Job]:
        api = f"{api_origin}/v1/boards/{quote(board)}/jobs"
        if content:
            api += "?content=true"
        response = self.session.get(api, timeout=30)
        response.raise_for_status()
        result = []
        for item in response.json().get("jobs", []):
            title = item.get("title", "").strip()
            link = item.get("absolute_url") or urljoin(f"https://boards.greenhouse.io/{board}/", item.get("url", ""))
            location_data = item.get("location") or {}
            location = location_data.get("name", "") if isinstance(location_data, dict) else str(location_data)
            if title and link:
                result.append(Job(title, link, location, _plain(item.get("content", ""))))
        return result

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
            location_facets = first_payload.get("facets", [])
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
            items = payload.get("jobPostings", [])
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
                    _workday_location_id_for_job(location_facets, location, location_ids) if locations else "",
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

    def fetch_job_details(self, company_url: str, job_url: str) -> dict | None:
        """Load full public job details for supported portals on demand."""
        company = urlparse(company_url)
        hostname = (company.hostname or "").casefold()
        posted = urlparse(job_url)
        if hostname == "careers.bp.com" and posted.hostname == hostname and posted.path.startswith("/job-description/"):
            response = self.session.get(job_url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.select('script[type="application/ld+json"]'):
                try:
                    data = json.loads(node.string or node.get_text())
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in _flatten_jsonld(data):
                    if item.get("@type") != "JobPosting":
                        continue
                    place = item.get("jobLocation", {})
                    if isinstance(place, list):
                        place = place[0] if place else {}
                    address = place.get("address", {}) if isinstance(place, dict) else {}
                    country = address.get("addressCountry", "") if isinstance(address, dict) else ""
                    if isinstance(country, dict):
                        country = country.get("name", "")
                    location = ", ".join(filter(None, [
                        address.get("addressLocality", "") if isinstance(address, dict) else "",
                        address.get("addressRegion", "") if isinstance(address, dict) else "",
                        country,
                    ]))
                    return {"description": _plain(item.get("description", "")), "location": location}
            return None
        if hostname == "jobs.citi.com" and posted.hostname == hostname and posted.path.startswith("/job/"):
            response = self.session.get(job_url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.select('script[type="application/ld+json"]'):
                try:
                    data = json.loads(node.string or node.get_text())
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in _flatten_jsonld(data):
                    if item.get("@type") != "JobPosting":
                        continue
                    return {"description": _plain(item.get("description", "")), "location": ""}
            return None
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in ("myworkdayjobs.com", "myworkdaysite.com")):
            return None
        parts = [part for part in company.path.split("/") if part]
        if (company.hostname or "").endswith("myworkdaysite.com"):
            try:
                recruiting_index = next(i for i, part in enumerate(parts) if part.casefold() == "recruiting")
                tenant, site = parts[recruiting_index + 1:recruiting_index + 3]
            except (StopIteration, ValueError):
                return None
        else:
            if not parts:
                return None
            site_index = next((i for i, part in enumerate(parts) if part.lower() not in {"en-us", "en-gb", "en-ca", "fr-fr", "de-de"}), 0)
            site = parts[site_index]
            tenant = company.hostname.split(".")[0]
        posted = urlparse(job_url)
        segments = [part for part in posted.path.split("/") if part]
        try:
            job_index = next(index for index, part in enumerate(segments) if part.casefold() == "job")
        except StopIteration:
            return None
        if job_index + 1 >= len(segments):
            return None
        external_path = "/" + "/".join(segments[job_index:])
        endpoint = f"{company.scheme}://{company.netloc}/wday/cxs/{tenant}/{site}{external_path}"
        response = self.session.get(endpoint, headers={"Accept": "application/json"}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        info = payload.get("jobPostingInfo", payload)
        return {
            "description": _plain(info.get("jobDescription") or info.get("description") or ""),
            "location": info.get("location", ""),
        }

    def _generic(self, url: str) -> list[Job]:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        greenhouse_jobs = self._greenhouse_embed(response.text, getattr(response, "url", None) or url)
        if greenhouse_jobs is not None:
            return greenhouse_jobs
        jobs = parse_generic_html(response.text, response.url)
        if jobs:
            return jobs
        # Optional dynamic-page fallback; install the `browser` extra and Playwright browser binaries.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            LOG.info("No static job links found at %s; install job-portal-scraper[browser] for browser fallback", url)
            return []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            html = page.content()
            final_url = page.url
            browser.close()
        return parse_generic_html(html, final_url)

    def _greenhouse_embed(self, html: str, page_url: str) -> list[Job] | None:
        """Read a Greenhouse widget embedded in a company's own careers page."""
        soup = BeautifulSoup(html, "html.parser")
        embedded = soup.select_one(".js-greenhouse-board, [data-block-type*='greenhouse_job_listing'], #greenhouse-jobs")
        if not embedded:
            return None

        board_pattern = re.compile(
            r"https?://(?P<host>(?:api|boards-api)\.greenhouse\.io)/v1/boards/(?P<board>[a-z0-9_-]+)/jobs",
            re.IGNORECASE,
        )
        match = board_pattern.search(html)
        if match:
            return self._greenhouse_board(match.group("board"), f"https://{match.group('host')}")

        page = urlparse(page_url)
        for script in soup.find_all("script", src=True):
            script_url = urljoin(page_url, script["src"])
            parsed_script = urlparse(script_url)
            # The embedded board configuration belongs to the company's own site bundle.
            if parsed_script.netloc.casefold() != (page.netloc or "").casefold():
                continue
            try:
                script_response = self.session.get(script_url, timeout=20)
                script_response.raise_for_status()
            except requests.RequestException as exc:
                LOG.debug("Could not inspect careers script %s (%s)", script_url, type(exc).__name__)
                continue
            match = board_pattern.search(script_response.text)
            if match:
                return self._greenhouse_board(match.group("board"), f"https://{match.group('host')}")

        LOG.warning("Found a Greenhouse widget at %s but could not discover its public board ID", page_url)
        return []


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
        link = origin + locale_and_site_path.rstrip("/") + route
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


def parse_generic_html(html: str, base_url: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Job] = {}
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _flatten_jsonld(data):
            if item.get("@type") == "JobPosting":
                title = item.get("title", "")
                url = item.get("url", "")
                place = item.get("jobLocation", {})
                if isinstance(place, list):
                    place = place[0] if place else {}
                address = place.get("address", {}) if isinstance(place, dict) else {}
                location = ", ".join(filter(None, [address.get("addressLocality", ""), address.get("addressRegion", ""), address.get("addressCountry", "")])) if isinstance(address, dict) else ""
                _add(found, title, urljoin(base_url, url), location, _plain(item.get("description", "")))
    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        href = urljoin(base_url, anchor["href"])
        context = " ".join(anchor.parent.get_text(" ", strip=True).split()) if anchor.parent else title
        hint = f"{title} {href} {context}".casefold()
        if title and re.search(r"\b(job|career|apply|position|engineer|developer|manager|analyst|designer|sales|workday|greenhouse)\b", hint):
            _add(found, title, href, _extract_location(context, title), "")
    return list(found.values())


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


def _parse_statestreet_html(html: str, base_url: str) -> list[Job]:
    """Extract job cards from State Street's Phenom search-results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Job] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        job_url = urlparse(href)
        if job_url.hostname != "careers.statestreet.com" or not job_url.path.casefold().startswith("/global/en/job/"):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title or title.casefold() in {"apply", "apply now", "view job", "view details"}:
            continue
        card = anchor
        # Use the nearest card ancestor with a requisition ID. A generic
        # "Location" ancestor can otherwise jump up to the global search form.
        for parent in anchor.parents:
            text = " ".join(parent.get_text(" ", strip=True).split())
            if len(text) > len(title) and re.search(r"\bR-\d+\b", text):
                card = parent
                break
            if parent.name in {"body", "html"}:
                break
        context = " ".join(card.get_text(" ", strip=True).split())
        if title.casefold() not in context.casefold():
            context = title
        context = re.sub(r"\s+", " ", context)
        location_match = re.search(r"\bLocation\s+(.+?)(?=\s+Category\b|\s+Job Id\b|$)", context, re.I)
        location = location_match.group(1).strip() if location_match else ""
        _add(found, title, href, location, context)
    return list(found.values())


def _parse_fisglobal_html(html: str, base_url: str) -> list[Job]:
    """Extract job cards from FIS's Phenom search results."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Job] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        job_url = urlparse(href)
        if job_url.hostname != "careers.fisglobal.com" or not job_url.path.casefold().startswith("/us/en/job/"):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title or title.casefold() in {"apply", "apply now", "view job", "view details"}:
            continue
        card = anchor
        for parent in anchor.parents:
            context = " ".join(parent.get_text(" ", strip=True).split())
            if len(context) > len(title) and re.search(r"\bJR\d+\b", context, re.I):
                card = parent
                break
            if parent.name in {"body", "html"}:
                break
        context = " ".join(card.get_text(" ", strip=True).split())
        location_match = re.search(r"\bLocation\s+(.+?)(?=\s+Job Id\b|\s+Category\b|\s+Posted Date\b|$)", context, re.I)
        location = location_match.group(1).strip() if location_match else ""
        description = re.sub(r"\s+", " ", context)
        _add(found, title, href, location, description)
    return list(found.values())


def _flatten_jsonld(data):
    if isinstance(data, list):
        for item in data:
            yield from _flatten_jsonld(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph", [])
        yield from _flatten_jsonld(graph)


def _add(target, title, url, location="", description=""):
    title = " ".join(str(title or "").split())
    if title and url.startswith(("http://", "https://")):
        target.setdefault(url.split("#", 1)[0], Job(title, url.split("#", 1)[0], location.strip(), description.strip()))


def _extract_location(context, title):
    tail = context.replace(title, "", 1).strip(" |·-\n\t")
    return tail[:120] if len(tail) <= 120 and tail else ""


def _plain(value):
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())

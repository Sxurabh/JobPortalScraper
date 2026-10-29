"""HTTP adapter for BlackRock's TalentBrew careers listings."""

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)
INDIAN_STATES_AND_TERRITORIES = (
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
    "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka",
    "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan",
    "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
)


class BlackRockAdapter:
    def _blackrock(self, url: str, locations: list[str]) -> list[Job]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # BlackRock exposes country landing pages with their own complete result
        # set and pagination. Filtering the global search locally can miss jobs
        # because its global results are paginated independently.
        country_routes = {
            "india": "/location/india-jobs/45831/1269750/2",
        }
        requested_country = next(
            (location.strip().casefold() for location in locations if location.strip().casefold() in country_routes),
            None,
        )
        search_url = urljoin(origin, country_routes[requested_country] if requested_country else "/search-jobs")
        page_url = search_url
        result: dict[str, Job] = {}
        seen_pages: set[str] = set()

        for page_number in range(100):
            if page_url in seen_pages:
                LOG.warning("BlackRock repeated a search page at %s; stopping pagination", page_url)
                break
            seen_pages.add(page_url)
            response = self.session.get(page_url, timeout=40)
            response.raise_for_status()
            html = response.text
            page_jobs, next_url = parse_blackrock_html(html, response.url or page_url)
            if not page_jobs and not _blackrock_explicit_empty(html):
                if _blackrock_has_scripts(html):
                    LOG.info("BlackRock returned no static job cards at %s; trying its rendered search page", response.url or page_url)
                    return self._blackrock_browser(search_url, locations)
                raise PortalStructureError("BlackRock search page has no recognizable job results; portal may need maintenance")
            for job in page_jobs:
                if locations and not any(location.casefold() in job.location.casefold() for location in locations):
                    continue
                result.setdefault(job.url, job)
            if not next_url:
                break
            page_url = next_url
        else:
            LOG.warning("BlackRock listings reached the 100-page safety limit for %s", search_url)
        return list(result.values())

    def _blackrock_browser(self, search_url: str, locations: list[str]) -> list[Job]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "BlackRock's search results require JavaScript. Install the browser extra with "
                "'python -m pip install -e \".[browser]\"' and run "
                "'python -m playwright install chromium'."
            )

        result: dict[str, Job] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.locator('a[href*="/job/"]').first.wait_for(timeout=25000)
                except PlaywrightTimeoutError:
                    html = page.content()
                    if _blackrock_explicit_empty(html):
                        return []
                    raise PortalStructureError(
                        f"BlackRock rendered search page has no job links at {page.url}; portal may need maintenance"
                    )

                for page_number in range(100):
                    html = page.content()
                    page_jobs, _ = parse_blackrock_html(html, page.url)
                    if not page_jobs:
                        if _blackrock_explicit_empty(html):
                            break
                        raise PortalStructureError(
                            f"BlackRock rendered search page has unrecognized job cards at {page.url}; portal may need maintenance"
                        )
                    for job in page_jobs:
                        if locations and not any(location.casefold() in job.location.casefold() for location in locations):
                            continue
                        result.setdefault(job.url, job)

                    page_numbers = _blackrock_page_numbers(html)
                    next_link = page.locator('a.next, a[rel="next"], button.next, a[aria-label*="Next"], button[aria-label*="Next"]')
                    if not next_link.count():
                        if page_numbers and page_numbers[0] < page_numbers[1]:
                            raise PortalStructureError(
                                f"BlackRock pagination stopped at page {page_numbers[0]} of {page_numbers[1]}; portal may need maintenance"
                            )
                        break
                    old_first = page.locator('a[href*="/job/"]').first.get_attribute("href")
                    # BlackRock's pagination link can have an empty href and be
                    # covered by its consent overlay, so invoke its handler in-page.
                    next_link.last.evaluate("element => element.click()")
                    try:
                        page.wait_for_function(
                            "oldHref => { const link = document.querySelector('a[href*=\\\"/job/\\\"]'); return link && link.getAttribute('href') !== oldHref; }",
                            arg=old_first,
                            timeout=20000,
                        )
                    except PlaywrightTimeoutError:
                        updated_numbers = _blackrock_page_numbers(page.content())
                        if updated_numbers and updated_numbers[0] >= updated_numbers[1]:
                            break
                        raise PortalStructureError(
                            f"BlackRock pagination did not advance after page {page_numbers[0] if page_numbers else page_number + 1}; portal may need maintenance"
                        ) from None
                else:
                    LOG.warning("BlackRock rendered listings reached the 100-page safety limit")
                page.close()
            finally:
                browser.close()
        return list(result.values())


def parse_blackrock_html(html: str, base_url: str) -> tuple[list[Job], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, Job] = {}
    for anchor in soup.find_all("a", href=True):
        link = urljoin(base_url, anchor["href"])
        parsed = urlparse(link)
        if parsed.hostname != "careers.blackrock.com" or not parsed.path.casefold().startswith("/job/"):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title or title.casefold() in {"apply", "save job", "save for later", "view job"}:
            continue
        card = anchor
        for parent in anchor.parents:
            context = " ".join(parent.get_text(" ", strip=True).split())
            if len(context) > len(title) and re.search(r"\bLocation:\s*.+\bTeam:\s*", context, re.I):
                card = parent
                break
            if parent.name in {"body", "html"}:
                break
        context = " ".join(card.get_text(" ", strip=True).split())
        location_match = re.search(
            r"\bLocation:\s*(.+?)(?=\s+(?:Additional Locations:|Team:|Job Requisition #|Date posted:)|$)",
            context,
            re.I,
        )
        location = location_match.group(1).strip() if location_match else ""
        location = _include_country_from_region(location)
        jobs.setdefault(link, Job(title, link, location, ""))

    next_link = None
    for anchor in soup.find_all("a", href=True):
        label = " ".join((anchor.get("aria-label") or anchor.get_text(" ", strip=True)).casefold().split())
        if label in {"next", "next page", "next ›", "next »", "›", "»"}:
            candidate = urljoin(base_url, anchor["href"])
            if urlparse(candidate).hostname == "careers.blackrock.com":
                next_link = candidate
                break
    return list(jobs.values()), next_link


def _blackrock_explicit_empty(html: str) -> bool:
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).casefold().split())
    return any(phrase in text for phrase in ("0 jobs found", "no jobs found", "no results found", "no results for"))


def _blackrock_has_scripts(html: str) -> bool:
    return bool(BeautifulSoup(html, "html.parser").find("script"))


def _blackrock_page_numbers(html: str) -> tuple[int, int] | None:
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
    match = re.search(r"currently on page\s+(\d+)\s*/\s*(\d+)", text, re.I)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _include_country_from_region(location: str) -> str:
    if not location or re.search(r"\bIndia\b", location, re.I):
        return location
    if any(re.search(rf"\b{re.escape(region)}\b", location, re.I) for region in INDIAN_STATES_AND_TERRITORIES):
        return f"{location}, India"
    return location

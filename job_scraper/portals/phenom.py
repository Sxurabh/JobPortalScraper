"""Shared browser-backed Phenom adapter and portal-specific card parsers."""

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class PhenomAdapter:
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
                        if _explicit_no_results(page.content()):
                            return []
                        raise PortalStructureError("State Street search page has no recognizable job cards; portal may need maintenance")

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
                        if not page_jobs and not _explicit_no_results(html):
                            raise PortalStructureError("State Street job cards are not recognizable; portal may need maintenance")
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
                        if _explicit_no_results(page.content()):
                            return []
                        raise PortalStructureError("FIS search page has no recognizable job cards; portal may need maintenance")

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
                        html = page.content()
                        page_jobs = _parse_fisglobal_html(html, page.url)
                        if not page_jobs and not _explicit_no_results(html):
                            raise PortalStructureError("FIS job cards are not recognizable; portal may need maintenance")
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

    def _mastercard(self, url: str, locations: list[str]) -> list[Job]:
        """Read Mastercard's JavaScript-rendered Phenom search results."""
        parsed = urlparse(url)
        board_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/us/en/search-results")
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Mastercard requires its JavaScript listings. Install the browser extra with "
                "'python -m pip install -e \".[browser]\"' and run "
                "'python -m playwright install chromium'."
            )

        result: dict[str, Job] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"{board_url}?from=0&s=1", wait_until="domcontentloaded", timeout=45000)
                try:
                    page.locator('a[href*="/us/en/job/"]').first.wait_for(timeout=20000)
                except PlaywrightTimeoutError:
                    if _explicit_no_results(page.content()):
                        return []
                    raise PortalStructureError("Mastercard search page has no recognizable job cards; portal may need maintenance")

                for page_number in range(200):
                    html = page.content()
                    page_jobs = _parse_mastercard_html(html, page.url)
                    if not page_jobs and not _explicit_no_results(html):
                        raise PortalStructureError("Mastercard job cards are not recognizable; portal may need maintenance")
                    for job in page_jobs:
                        result.setdefault(job.url, job)
                    next_link = page.locator('a[aria-label="View next page"]')
                    if not next_link.count() or not next_link.get_attribute("href"):
                        break
                    old_first = page.locator('a[href*="/us/en/job/"]').first.get_attribute("href")
                    next_link.evaluate("element => element.click()")
                    try:
                        page.wait_for_function(
                            "oldHref => { const link = document.querySelector('a[href*=\\\"/us/en/job/\\\"]'); return link && link.getAttribute('href') !== oldHref; }",
                            arg=old_first,
                            timeout=20000,
                        )
                    except PlaywrightTimeoutError:
                        break
                else:
                    LOG.warning("Mastercard listings reached the 200-page safety limit")
                page.close()
            finally:
                browser.close()

        jobs = list(result.values())
        if locations:
            jobs = [job for job in jobs if any(location.casefold() in job.location.casefold() for location in locations)]
        return jobs


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
        _phenom_add(found, title, href, location, context)
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
        _phenom_add(found, title, href, location, description)
    return list(found.values())


def _parse_mastercard_html(html: str, base_url: str) -> list[Job]:
    """Extract Mastercard job cards from its Phenom search results."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Job] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        parsed = urlparse(href)
        if parsed.hostname != "careers.mastercard.com" or not parsed.path.casefold().startswith("/us/en/job/"):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title or title.casefold() in {"apply", "apply now", "view job", "view details"}:
            continue
        card = anchor
        for parent in anchor.parents:
            context = " ".join(parent.get_text(" ", strip=True).split())
            if len(context) > len(title) and re.search(r"\b(?:Job Id|Job ID|Requisition)\b", context, re.I):
                card = parent
                break
            if parent.name in {"body", "html"}:
                break
        context = " ".join(card.get_text(" ", strip=True).split())
        location_match = re.search(
            r"\bLocation\s+(.+?)(?=\s+(?:Job Id|Job ID|Requisition|Category|Posted Date)\b|$)",
            context,
            re.I,
        )
        location = location_match.group(1).strip() if location_match else ""
        _phenom_add(found, title, href, location, context)
    return list(found.values())


def _explicit_no_results(html: str) -> bool:
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).casefold().split())
    return any(phrase in text for phrase in (
        "no results found",
        "no results for",
        "no jobs found",
        "no jobs match",
        "there are currently no open positions",
        "no openings at this time",
    ))


def _phenom_add(target, title, url, location="", description=""):
    title = " ".join(str(title or "").split())
    if title and url.startswith(("http://", "https://")):
        target.setdefault(url.split("#", 1)[0], Job(title, url.split("#", 1)[0], location.strip(), description.strip()))

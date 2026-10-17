import json
import logging
import re
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Job

LOG = logging.getLogger(__name__)
HEADERS = {"User-Agent": "JobPortalScraper/0.1 (+personal job alerts)"}


class PortalScraper:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str) -> list[Job]:
        lowered = url.lower()
        if "greenhouse.io" in lowered:
            return self._greenhouse(url)
        if "myworkdayjobs.com" in lowered:
            return self._workday(url)
        return self._generic(url)

    def _greenhouse(self, url: str) -> list[Job]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError(f"Could not identify Greenhouse board in {url}")
        board = parts[0]
        return self._greenhouse_board(board, "https://boards-api.greenhouse.io", content=True)

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

    def _workday(self, url: str) -> list[Job]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("Workday URL must include a tenant and career site path")
        site_index = next((i for i, part in enumerate(parts) if part.lower() not in {"en-us", "en-gb", "en-ca", "fr-fr", "de-de"}), 0)
        site = parts[site_index]
        tenant = parsed.hostname.split(".")[0]
        api = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"
        result: list[Job] = []
        offset = 0
        while True:
            response = self.session.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("jobPostings", [])
            for item in items:
                title = item.get("title", "").strip()
                path = item.get("externalPath") or item.get("externalUrl") or ""
                link = path if path.startswith("http") else urljoin(f"{parsed.scheme}://{parsed.netloc}", path)
                location = item.get("locationsText") or item.get("location", "")
                if title and link:
                    result.append(Job(title, link, location, " ".join(item.get("bulletFields", []))))
            total = payload.get("total", len(items))
            offset += len(items)
            if not items or offset >= total:
                break
        return result

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

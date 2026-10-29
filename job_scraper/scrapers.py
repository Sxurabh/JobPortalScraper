import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Job
from .portals.registry import handler_for
from .portals.errors import PortalStructureError
from .portals.phenom import (
    PhenomAdapter,
    _parse_fisglobal_html,
    _parse_mastercard_html,
    _parse_statestreet_html,
)
from .portals.workday import WorkdayAdapter
from .portals.greenhouse import GreenhouseAdapter
from .portals.ubs import UbsAdapter
from .portals.bp import BpAdapter
from .portals.citi import CitiAdapter
from .portals.blackrock import BlackRockAdapter
from .portals.details import parse_job_details_html

LOG = logging.getLogger(__name__)
HEADERS = {"User-Agent": "JobPortalScraper/0.1 (+personal job alerts)"}


class PortalScraper(PhenomAdapter, WorkdayAdapter, GreenhouseAdapter, UbsAdapter, BpAdapter, CitiAdapter, BlackRockAdapter):
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str, locations: list[str] | None = None) -> list[Job]:
        handler = handler_for(urlparse(url).hostname or "")
        if not handler:
            return self._generic(url)
        method = getattr(self, handler)
        if handler in {"_greenhouse", "_addepar"}:
            return method(url)
        return method(url, locations or [])

    def fetch_company(self, company) -> list[Job]:
        """Fetch using company-specific portal filters when supported."""
        return self.fetch(company.url, locations=company.locations)

    def needs_job_enrichment(self, company_url: str, job: Job) -> bool:
        """Return whether a listing has only a summary or no JD at all."""
        handler = handler_for(urlparse(company_url).hostname or "")
        if handler in {"_blackrock", "_statestreet", "_fisglobal", "_mastercard", "_bp", "_citi", "_workday"}:
            return True
        return not bool(job.description.strip())

    def fetch_job_details(self, company_url: str, job_url: str) -> dict | None:
        """Load full public job details for registered portals on demand."""
        company = urlparse(company_url)
        hostname = (company.hostname or "").casefold()
        posted = urlparse(job_url)
        posted_host = (posted.hostname or "").casefold()
        handler = handler_for(hostname)
        is_addepar_greenhouse_job = (
            handler == "_addepar"
            and (posted_host == "greenhouse.io" or posted_host.endswith(".greenhouse.io"))
        )
        if not hostname or (posted_host != hostname and not is_addepar_greenhouse_job):
            return None

        if handler == "_addepar":
            return self._greenhouse_job_details(job_url)
        if handler in {"_bp", "_citi"}:
            response = self.session.get(job_url, timeout=30)
            response.raise_for_status()
            return parse_job_details_html(response.text)

        if handler in {"_blackrock", "_statestreet", "_fisglobal", "_mastercard"}:
            response = self.session.get(job_url, timeout=30)
            response.raise_for_status()
            return parse_job_details_html(response.text)

        if handler not in {"_workday", None}:
            # Greenhouse and UBS provide descriptions in their listing payloads.
            return None

        if handler is None:
            response = self.session.get(job_url, timeout=30)
            response.raise_for_status()
            return parse_job_details_html(response.text)

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

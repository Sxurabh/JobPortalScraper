"""Greenhouse public board API and embedded-widget adapter."""

import logging
import re
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .errors import PortalStructureError

LOG = logging.getLogger(__name__)


class GreenhouseAdapter:
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
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise PortalStructureError("Greenhouse response has no recognizable jobs list; portal may need maintenance")
        for item in payload["jobs"]:
            title = item.get("title", "").strip()
            link = item.get("absolute_url") or urljoin(f"https://boards.greenhouse.io/{board}/", item.get("url", ""))
            location_data = item.get("location") or {}
            location = location_data.get("name", "") if isinstance(location_data, dict) else str(location_data)
            if title and link:
                result.append(Job(title, link, location, _greenhouse_plain(item.get("content", ""))))
        return result

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
        raise PortalStructureError("Greenhouse widget no longer exposes a recognizable public board ID; portal may need maintenance")


def _greenhouse_plain(value):
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())

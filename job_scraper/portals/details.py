"""Shared extraction helpers for public job detail pages."""

import json

from bs4 import BeautifulSoup


def parse_job_details_html(html: str) -> dict | None:
    """Extract description/location from JobPosting JSON-LD or known detail regions."""
    soup = BeautifulSoup(html, "html.parser")
    jsonld_location = ""
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _flatten(data):
            types = item.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if "JobPosting" not in types:
                continue
            description = _plain(item.get("description", ""))
            location = _jsonld_location(item.get("jobLocation"))
            if description:
                return {"description": description, "location": location}
            jsonld_location = jsonld_location or location
    if jsonld_location:
        return {"description": "", "location": jsonld_location}

    selectors = (
        '[itemprop="description"]',
        ".job-description",
        "#job-description",
        '[class*="job-description"]',
        '[id*="job-description"]',
        '[class*="jobDescription"]',
        '[id*="jobDescription"]',
    )
    candidates = []
    for selector in selectors:
        for node in soup.select(selector):
            # Ignore nested duplicates and navigation/control fragments.
            for unwanted in node.select("script, style, button, nav, form"):
                unwanted.decompose()
            text = _plain(node.get_text(" ", strip=True))
            if text.casefold() not in {"description", "job description", "job details"}:
                candidates.append(text)
    if candidates:
        description = max(candidates, key=len)
        return {"description": description, "location": ""}
    return None


def _flatten(value):
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    elif isinstance(value, dict):
        yield value
        yield from _flatten(value.get("@graph", []))


def _jsonld_location(value):
    places = value if isinstance(value, list) else [value]
    for place in places:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry", "")
        if isinstance(country, dict):
            country = country.get("name", "")
        location = ", ".join(str(part).strip() for part in (
            address.get("addressLocality", ""),
            address.get("addressRegion", ""),
            country,
        ) if str(part).strip())
        if location:
            return location
    return ""


def _plain(value):
    if not value:
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())

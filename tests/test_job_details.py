from pathlib import Path

import pytest

from job_scraper.scrapers import PortalScraper


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_blackrock_detail_fetch_extracts_description_without_page_chrome():
    url = "https://careers.blackrock.com/job/mumbai/senior-engineer/45831/123"
    session = FakeSession(FakeResponse(text=_fixture("blackrock_job.html")))

    details = PortalScraper(session).fetch_job_details("https://careers.blackrock.com/", url)

    assert details["description"] == (
        "Job description Build resilient services for investment operations and collaborate with product and engineering teams. "
        "Design, develop, test, and maintain software used by BlackRock clients around the world."
    )
    assert "Senior Software Engineer" not in details["description"]
    assert "Mumbai" not in details["description"]
    assert session.calls[0][0] == url


@pytest.mark.parametrize("host,path", [
    ("careers.statestreet.com", "/global/en/job/STST123/Software-Engineer"),
    ("careers.fisglobal.com", "/us/en/job/FIS123/Software-Engineer"),
    ("careers.mastercard.com", "/us/en/job/MC123/Software-Engineer"),
])
def test_phenom_portal_details_extract_full_description(host, path):
    company_url = f"https://{host}/"
    job_url = f"https://{host}{path}"
    session = FakeSession(FakeResponse(text=_fixture("phenom_job.html")))

    details = PortalScraper(session).fetch_job_details(company_url, job_url)

    assert "Design and build reliable payment systems" in details["description"]
    assert "Pune" not in details["description"]


@pytest.mark.parametrize("host,path", [
    ("careers.bp.com", "/job-description/R123"),
    ("jobs.citi.com", "/job/123/data-analyst"),
    ("unknown.example", "/jobs/data-analyst"),
])
def test_jsonld_job_details_are_supported_for_bp_citi_and_generic_sites(host, path):
    company_url = f"https://{host}/careers"
    job_url = f"https://{host}{path}"
    session = FakeSession(FakeResponse(text=_fixture("jobposting_details.html")))

    details = PortalScraper(session).fetch_job_details(company_url, job_url)

    assert details == {
        "description": "Analyze business data and present clear findings to stakeholders.",
        "location": "Pune, Maharashtra, India",
    }


def test_workday_details_use_the_job_posting_api():
    company_url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    job_url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Pune-India/Data_Analyst_R123"
    endpoint = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Pune-India/Data_Analyst_R123"
    session = FakeSession(FakeResponse(payload={"jobPostingInfo": {
        "jobDescription": "<p>Analyze reporting data and communicate actionable recommendations.</p>",
        "location": "Pune, India",
    }}))

    details = PortalScraper(session).fetch_job_details(company_url, job_url)

    assert details == {
        "description": "Analyze reporting data and communicate actionable recommendations.",
        "location": "Pune, India",
    }
    assert session.calls[0][0] == endpoint
    assert session.calls[0][1]["headers"] == {"Accept": "application/json"}


def test_cross_host_job_detail_urls_are_not_requested():
    session = FakeSession(FakeResponse(text=_fixture("jobposting_details.html")))

    assert PortalScraper(session).fetch_job_details(
        "https://careers.blackrock.com/",
        "https://attacker.example/job/1",
    ) is None
    assert session.calls == []


def test_valid_job_page_without_description_returns_none():
    session = FakeSession(FakeResponse(text="<main><h1>Analyst</h1><p>Location: Pune</p></main>"))

    assert PortalScraper(session).fetch_job_details(
        "https://careers.blackrock.com/",
        "https://careers.blackrock.com/job/pune/analyst/1/2",
    ) is None

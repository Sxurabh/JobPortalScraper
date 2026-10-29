from html import escape
import json
from pathlib import Path

import pytest

from job_scraper.portals.registry import handler_for
from job_scraper.portals.blackrock import parse_blackrock_html
from job_scraper.portals.phenom import _parse_mastercard_html, _parse_statestreet_html
from job_scraper.scrapers import PortalScraper, PortalStructureError, parse_generic_html


class FakeResponse:
    def __init__(self, payload=None, text="", url=""):
        self.payload = payload
        self.text = text
        self.url = url

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload=None, routes=None):
        self.payload = payload
        self.routes = routes or {}
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        response = self.routes.get(url)
        return response if response is not None else FakeResponse(self.payload, url=url)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        response = self.routes.get(url)
        return response if response is not None else FakeResponse(self.payload, url=url)


def test_generic_parser_extracts_jobposting_and_relative_job_links():
    html = '''
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"Data Engineer","url":"/jobs/data","jobLocation":{"address":{"addressLocality":"Toronto","addressRegion":"ON"}}}
    </script>
    <div><a href="/careers/platform-engineer">Platform Engineer</a></div>
    '''
    jobs = parse_generic_html(html, "https://example.test/careers")
    by_title = {job.title: job for job in jobs}
    assert by_title["Data Engineer"].url == "https://example.test/jobs/data"
    assert by_title["Data Engineer"].location == "Toronto, ON"
    assert by_title["Platform Engineer"].url == "https://example.test/careers/platform-engineer"


def test_greenhouse_uses_board_api():
    session = FakeSession({"jobs": [{"title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/123", "location": {"name": "Remote"}, "content": "<p>Build systems</p>"}]})
    jobs = PortalScraper(session).fetch("https://boards.greenhouse.io/acme")
    assert session.calls[0][1] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].location == "Remote"
    assert jobs[0].description == "Build systems"


def test_portal_registry_selects_specific_handlers_and_generic_fallback():
    assert handler_for("careers.mastercard.com") == "_mastercard"
    assert handler_for("careers.blackrock.com") == "_blackrock"
    assert handler_for("acme.myworkdayjobs.com") == "_workday"
    assert handler_for("jobs.citi.com") == "_citi"
    assert handler_for("careers.bp.com") == "_bp"
    assert handler_for("jobs.ubs.com") == "_ubs"
    assert handler_for("careers.fisglobal.com") == "_fisglobal"
    assert handler_for("careers.statestreet.com") == "_statestreet"
    assert handler_for("boards.greenhouse.io") == "_greenhouse"
    assert handler_for("unknown.example") is None


def test_mastercard_parser_extracts_job_card_from_saved_fixture():
    fixture = Path(__file__).parent / "fixtures" / "mastercard_search.html"
    jobs = _parse_mastercard_html(fixture.read_text(encoding="utf-8"), "https://careers.mastercard.com/us/en/search-results")
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].url == "https://careers.mastercard.com/us/en/job/MC-123/Software-Engineer"
    assert jobs[0].location == "Pune, India"


def test_blackrock_adapter_parses_locations_and_follows_pagination():
    fixture = Path(__file__).parent / "fixtures" / "blackrock_search.html"
    first_url = "https://careers.blackrock.com/search-jobs"
    second_url = "https://careers.blackrock.com/search-jobs?p=2"
    first_page = fixture.read_text(encoding="utf-8")
    second_page = '<main id="search-results"><article><a href="/job/london/analyst/45833/95865906322">Investment Analyst</a><p>Location: London, England Team: Research</p></article></main>'
    session = FakeSession(routes={
        first_url: FakeResponse(text=first_page, url=first_url),
        second_url: FakeResponse(text=second_page, url=second_url),
    })

    jobs = PortalScraper(session).fetch("https://careers.blackrock.com/", locations=["London"])

    assert len(jobs) == 1
    assert jobs[0].title == "Investment Analyst"
    assert jobs[0].location == "London, England"
    assert [call[1] for call in session.calls] == [first_url, second_url]


def test_blackrock_country_filter_matches_indian_state_locations():
    page_url = "https://careers.blackrock.com/location/india-jobs/45831/1269750/2"
    html = '<main><article><a href="/job/mumbai/analyst/1/2">Data Analyst</a><p>Location: Mumbai, Maharashtra Team: Technology</p></article></main>'
    session = FakeSession(routes={page_url: FakeResponse(text=html, url=page_url)})

    jobs = PortalScraper(session).fetch("https://careers.blackrock.com/", locations=["India"])

    assert len(jobs) == 1
    assert jobs[0].location == "Mumbai, Maharashtra, India"
    assert session.calls[0][1] == page_url


def test_blackrock_india_country_route_collects_all_paginated_results():
    base = "https://careers.blackrock.com/location/india-jobs/45831/1269750/2"
    routes = {}
    expected = 38
    for page_number in range(1, 5):
        page_url = base if page_number == 1 else f"{base}/{page_number}"
        start = (page_number - 1) * 10
        stop = min(start + 10, expected)
        cards = "".join(
            f'<article><a href="/job/mumbai/analyst-{index}/45831/{index}">Analyst {index}</a>'
            f'<p>Location: Mumbai, Maharashtra Team: Technology</p></article>'
            for index in range(start, stop)
        )
        next_link = (
            f'<a aria-label="Next page" href="{base}/{page_number + 1}">Next</a>'
            if page_number < 4 else ""
        )
        html = f"<main>{cards}<nav>{next_link}</nav></main>"
        routes[page_url] = FakeResponse(text=html, url=page_url)

    session = FakeSession(routes=routes)
    jobs = PortalScraper(session).fetch("https://careers.blackrock.com/", locations=["India"])

    assert len(jobs) == expected
    assert len(session.calls) == 4


def test_blackrock_valid_empty_results_are_successful_and_unrecognized_pages_flag_maintenance():
    empty_session = FakeSession(routes={
        "https://careers.blackrock.com/search-jobs": FakeResponse(
            text="<main><h1>0 jobs found</h1></main>",
            url="https://careers.blackrock.com/search-jobs",
        ),
    })
    assert PortalScraper(empty_session).fetch("https://careers.blackrock.com/") == []

    broken_session = FakeSession(routes={
        "https://careers.blackrock.com/search-jobs": FakeResponse(
            text="<main>Search jobs</main>",
            url="https://careers.blackrock.com/search-jobs",
        ),
    })
    with pytest.raises(PortalStructureError, match="may need maintenance"):
        PortalScraper(broken_session).fetch("https://careers.blackrock.com/")


def test_greenhouse_valid_empty_results_are_not_a_structure_error():
    session = FakeSession({"jobs": []})
    assert PortalScraper(session).fetch("https://boards.greenhouse.io/acme") == []


def test_greenhouse_changed_response_shape_reports_maintenance_signal():
    session = FakeSession({"openings": []})
    with pytest.raises(PortalStructureError, match="may need maintenance"):
        PortalScraper(session).fetch("https://boards.greenhouse.io/acme")


def test_workday_changed_response_shape_reports_maintenance_signal():
    session = FakeSession({"openings": []})
    with pytest.raises(PortalStructureError, match="may need maintenance"):
        PortalScraper(session).fetch("https://acme.wd5.myworkdayjobs.com/en-US/External")


def test_bp_adapter_paginates_index_results():
    session = FakeSession({"nbPages": 2, "hits": [{
        "title": "Data Engineer", "job_code": ["R123"], "location": ["Pune, India"],
        "_highlightResult": {"summary": {"value": "Build data systems"}},
    }]})
    jobs = PortalScraper(session).fetch("https://careers.bp.com/listing")
    assert len(jobs) == 1
    assert jobs[0].url.endswith("/job-description/R123")
    assert len(session.calls) == 2


def test_bp_location_filter_is_applied_to_search():
    session = FakeSession({
        "nbPages": 1,
        "facets": {"location_list": {"Pune, India": 1}},
        "hits": [{"title": "Data Analyst", "job_code": ["A1"], "location": ["Pune, India"]}],
    })
    jobs = PortalScraper(session).fetch("https://careers.bp.com/listing", locations=["Pune"])
    assert len(jobs) == 1
    assert session.calls[-1][2]["json"]["facetFilters"] == [["location_list:Pune, India"]]


def test_citi_adapter_parses_job_cards():
    search_url = "https://jobs.citi.com/search-jobs"
    results_url = "https://jobs.citi.com/search-jobs/resultspost"
    result_html = '<div id="search-results" data-total-pages="1"><div class="sr-job-item"><a class="sr-job-item__link" href="/job/123">Software Engineer</a><span class="sr-job-location">Pune</span></div></div>'
    session = FakeSession({"results": result_html, "hasJobs": True}, routes={
        search_url: FakeResponse(text='<div id="search-results" data-search-type="5"></div>', url=search_url),
    })
    jobs = PortalScraper(session).fetch("https://jobs.citi.com/")
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].location == "Pune"
    assert jobs[0].url == "https://jobs.citi.com/job/123"
    assert session.calls[-1][1] == results_url


def test_ubs_adapter_parses_embedded_search_payload():
    page_url = "https://jobs.ubs.com/search"
    preload = {
        "searchResultsResponse": {"Jobs": {"Job": [{
            "Link": "/job/analyst",
            "Questions": [
                {"QuestionName": "jobtitle", "Value": "Data Analyst"},
                {"QuestionName": "formtext2", "Value": "Pune"},
                {"QuestionName": "formtext23", "Value": "India"},
                {"QuestionName": "jobdescription", "Value": "<p>Analyze financial data and create reports.</p>"},
            ],
        }]}},
        "SmartSearchJSONValue": "{}",
    }
    page = f'<input id="preLoadJSON" value="{escape(json.dumps(preload), quote=True)}">'
    session = FakeSession(routes={page_url: FakeResponse(text=page, url=page_url)})
    jobs = PortalScraper(session).fetch(page_url)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"
    assert jobs[0].location == "Pune, India"
    assert jobs[0].description == "Analyze financial data and create reports."


def test_addepar_style_embedded_greenhouse_board_is_discovered():
    # Addepar's job widget is embedded in the page; the hash is optional and is
    # not included in the HTTP request by browsers or Requests.
    page_url = "https://addepar.com/careers"
    page_html = '<div data-block-type="greenhouse_job_listing"><div id="greenhouse-jobs"><div class="js-greenhouse-board"></div></div></div><script src="/js/addepar.js"></script>'
    script_url = "https://addepar.com/js/addepar.js"
    script = 'fetch("https://api.greenhouse.io/v1/boards/addepar1/jobs")'
    api_url = "https://api.greenhouse.io/v1/boards/addepar1/jobs"
    payload = {"jobs": [{
        "title": "Staff Data Engineer",
        "absolute_url": "https://job-boards.greenhouse.io/addepar1/jobs/123",
        "location": {"name": "Remote - US"},
        "content": "<p>Build data products</p>",
    }]}
    session = FakeSession(routes={
        page_url: FakeResponse(text=page_html, url="https://addepar.com/careers"),
        script_url: FakeResponse(text=script, url=script_url),
        api_url: FakeResponse(payload=payload, url=api_url),
    })

    jobs = PortalScraper(session).fetch(page_url)

    assert len(jobs) == 1
    assert jobs[0].title == "Staff Data Engineer"
    assert jobs[0].location == "Remote - US"
    assert jobs[0].url == "https://job-boards.greenhouse.io/addepar1/jobs/123"
    assert [call[1] for call in session.calls] == [page_url, script_url, api_url]


def test_workday_uses_public_search_endpoint():
    session = FakeSession({"total": 1, "jobPostings": [{"title": "Product Analyst", "externalPath": "/job/Analyst_R123", "locationsText": "Toronto, Canada", "bulletFields": ["R123"]}]})
    jobs = PortalScraper(session).fetch("https://acme.wd5.myworkdayjobs.com/en-US/External")
    method, url, options = session.calls[0]
    assert method == "post"
    assert url == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    assert options["json"]["searchText"] == ""
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123"
    assert jobs[0].location == "Toronto, Canada"


def test_workday_site_uses_exact_location_facet_for_ssc():
    api = "https://wd1.myworkdaysite.com/wday/cxs/ssctech/SSCTechnologies/jobs"
    payload = {
        "total": 1,
        "jobPostings": [{
            "title": "Financial Services Associate",
            "externalPath": "/job/Pune-India---Commerzone-Building-7/Associate_R123",
            "locationsText": "Pune India - Commerzone Building 7",
            "bulletFields": ["R123"],
        }],
        "facets": [{
            "facetParameter": "locationMainGroup",
            "values": [{
                "facetParameter": "locations",
                "values": [{"descriptor": "Pune India - Commerzone Building 7", "id": "pune-commerzone-id", "count": 1}],
            }],
        }],
    }
    session = FakeSession(payload)

    jobs = PortalScraper(session).fetch(
        "https://wd1.myworkdaysite.com/en-US/recruiting/ssctech/SSCTechnologies",
        locations=["Pune India - Commerzone Building 7"],
    )

    assert len(jobs) == 1
    assert jobs[0].location == "Pune India - Commerzone Building 7"
    assert jobs[0].url == (
        "https://wd1.myworkdaysite.com/en-US/recruiting/ssctech/SSCTechnologies/"
        "job/Pune-India---Commerzone-Building-7/Associate_R123?locations=pune-commerzone-id"
    )
    assert session.calls[0][1] == api
    assert session.calls[1][2]["json"]["appliedFacets"] == {"locations": ["pune-commerzone-id"]}


def test_statestreet_parser_extracts_job_card_and_ignores_external_apply_link():
    page_url = "https://careers.statestreet.com/global/en/search-results?from=0&s=1"
    html = '''
    <div class="job-card">
      <a href="/global/en/job/STSTGLOBALR123EXTERNALENGLOBAL/Analyst">Data Analyst</a>
      <p>Location Pune Category Other Job Id R-123</p>
      <p>Analyze investment operations data.</p>
    </div>
    <a href="https://statestreet.wd1.myworkdayjobs.com/Global/job/PUNE/Analyst_R-123/apply">Apply Now Data Analyst</a>
    '''

    jobs = _parse_statestreet_html(html, page_url)

    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"
    assert jobs[0].location == "Pune"
    assert jobs[0].url == "https://careers.statestreet.com/global/en/job/STSTGLOBALR123EXTERNALENGLOBAL/Analyst"

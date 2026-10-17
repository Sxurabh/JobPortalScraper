from job_scraper.scrapers import PortalScraper, parse_generic_html


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
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/job/Analyst_R123"
    assert jobs[0].location == "Toronto, Canada"

import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest

from job_scraper.mcp_server import DashboardAPIError, DashboardClient
from job_scraper.models import Company, Job
from job_scraper.web import create_app


class FlaskTestOpener:
    def __init__(self, app):
        self.client = app.test_client()

    def __call__(self, request, timeout):
        response = self.client.open(
            urlsplit(request.full_url).path + ("?" + urlsplit(request.full_url).query if urlsplit(request.full_url).query else ""),
            method=request.method,
            data=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "Dashboard error", response.headers, BytesIO(response.data))
        return BytesIO(response.data)


@pytest.fixture
def dashboard(tmp_path):
    app = create_app(
        tmp_path / "companies.json",
        tmp_path / "jobs.sqlite3",
        tmp_path / "settings.json",
        start_worker=False,
    )
    storage = app.config["STORAGE"]
    company = Company("Acme", "https://careers.example.test", ["engineer"], ["Remote"])
    storage.upsert_company(company)
    storage.save_baseline(company, [
        Job("Remote Engineer", "https://careers.example.test/job/1", "Remote", "Full description"),
    ])
    client = DashboardClient("http://127.0.0.1:8765", opener=FlaskTestOpener(app))
    try:
        yield client
    finally:
        storage.close()


def test_dashboard_client_reads_status_jobs_companies_and_saves_tracking(dashboard):
    status = dashboard.get("/api/status")
    assert status["all_jobs"] == 1
    assert dashboard.get("/api/companies")[0]["name"] == "Acme"
    jobs = dashboard.get("/api/jobs", params={"scope": "all", "limit": 10})
    assert jobs[0]["title"] == "Remote Engineer"

    response = dashboard.patch("/api/jobs/tracking", {
        "company_name": "Acme",
        "url": "https://careers.example.test/job/1",
        "application_status": "applied",
        "application_notes": "Submitted via agent",
    })
    assert response["ok"] is True
    updated = dashboard.get("/api/jobs", params={"scope": "all", "limit": 10})[0]
    assert updated["application_status"] == "applied"
    assert updated["application_notes"] == "Submitted via agent"


def test_dashboard_client_company_settings_and_control_actions(dashboard):
    saved = dashboard.post("/api/companies", {
        "name": "Other",
        "url": "https://boards.greenhouse.io/other",
        "keywords": ["analyst"],
        "locations": [],
        "enabled": True,
    })
    assert saved["company"]["name"] == "Other"
    assert dashboard.post("/api/control/scan")["ok"] is True
    with pytest.raises(DashboardAPIError, match="A scan is already running"):
        dashboard.post("/api/control/scan")
    assert dashboard.post("/api/control/start")["ok"] is True
    assert dashboard.post("/api/control/pause")["ok"] is True

    settings = dashboard.post("/api/settings", {
        "bot_token": "private-token",
        "chat_id": "@career_radar_test",
        "interval_minutes": 15,
    })
    assert settings["bot_token_configured"] is True
    assert "private-token" not in json.dumps(dashboard.get("/api/settings"))
    assert dashboard.post("/api/companies/remove", {"name": "Other"})["ok"] is True


def test_dashboard_client_reports_api_errors_and_unavailable_dashboard(tmp_path):
    def unavailable_opener(request, timeout):
        raise URLError(ConnectionRefusedError("connection refused"))

    unavailable = DashboardClient("http://127.0.0.1:1", timeout=0.2, opener=unavailable_opener)
    with pytest.raises(DashboardAPIError, match="dashboard is unavailable"):
        unavailable.get("/api/status")

    app = create_app(
        tmp_path / "companies.json",
        tmp_path / "jobs.sqlite3",
        tmp_path / "settings.json",
        start_worker=False,
    )
    try:
        client = DashboardClient("http://127.0.0.1:8765", opener=FlaskTestOpener(app))
        with pytest.raises(DashboardAPIError, match="Job was not found"):
            client.patch("/api/jobs/tracking", {
                "company_name": "Missing",
                "url": "https://example.test/job/1",
            })
    finally:
        app.config["STORAGE"].close()


@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://192.168.1.2:8765",
    "file:///tmp/jobs.sqlite3",
])
def test_dashboard_client_rejects_non_loopback_urls(url):
    with pytest.raises(ValueError, match="only connects to an HTTP dashboard on this computer"):
        DashboardClient(url)

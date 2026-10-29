import json
import sqlite3
import time

from job_scraper.web import create_app
from job_scraper.models import Company, Job


def test_dashboard_company_management_and_private_settings(tmp_path):
    config = tmp_path / "companies.json"
    database = tmp_path / "jobs.sqlite3"
    settings = tmp_path / "settings.json"
    app = create_app(config, database, settings, start_worker=False)
    app.testing = True
    client = app.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/api/status").json["companies"] == 0
    created = client.post("/api/companies", json={
        "name": "Acme",
        "url": "https://boards.greenhouse.io/acme",
        "keywords": ["engineer", "designer"],
        "locations": ["Remote"],
        "enabled": False,
    })
    assert created.status_code == 200
    assert client.get("/api/companies").json[0]["name"] == "Acme"
    assert client.get("/api/companies").json[0]["enabled"] is False
    assert json.loads(config.read_text(encoding="utf-8"))[0]["keywords"] == ["engineer", "designer"]
    assert json.loads(config.read_text(encoding="utf-8"))[0]["enabled"] is False

    invalid = client.post("/api/companies", json={"name": "No URL", "url": "javascript:bad"})
    assert invalid.status_code == 400
    assert client.post("/api/settings/test-telegram", json={}).status_code == 400
    saved = client.post("/api/settings", json={"bot_token": "secret-token", "chat_id": "@acme", "interval_minutes": 15})
    assert saved.status_code == 200
    assert saved.json["interval_minutes"] == 15
    assert "secret-token" not in client.get("/api/settings").get_data(as_text=True)
    removed = client.post("/api/companies/remove", json={"name": "Acme"})
    assert removed.status_code == 200
    assert client.get("/api/companies").json == []
    assert json.loads(config.read_text(encoding="utf-8")) == []
    app.config["STORAGE"].close()


def test_existing_database_is_migrated_without_losing_jobs(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE companies (name TEXT PRIMARY KEY COLLATE NOCASE, url TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]', locations TEXT NOT NULL DEFAULT '[]', baseline_done INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE jobs (company_name TEXT NOT NULL COLLATE NOCASE, url TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', notification_status TEXT NOT NULL DEFAULT 'suppressed', PRIMARY KEY(company_name,url));
        INSERT INTO companies(name,url,baseline_done) VALUES('Legacy','https://example.test/careers',1);
        INSERT INTO jobs(company_name,url,title,notification_status) VALUES('Legacy','https://example.test/job/1','Existing role','sent');
    """)
    connection.commit()
    connection.close()

    app = create_app(tmp_path / "missing.json", database, tmp_path / "settings.json", start_worker=False)
    client = app.test_client()
    jobs = client.get("/api/jobs").json
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Existing role"
    assert jobs[0]["discovered_at"]
    assert client.get("/api/companies").json[0]["name"] == "Legacy"
    assert client.get("/api/companies").json[0]["enabled"] is True
    app.config["STORAGE"].close()


def test_legacy_company_config_defaults_to_enabled():
    company = Company.from_dict({"name": "Legacy", "url": "https://example.test/jobs"})
    assert company.enabled is True
    assert Company.from_dict(company.to_dict()).enabled is True


def test_dashboard_start_and_pause_controls_background_monitor(tmp_path):
    app = create_app(tmp_path / "companies.json", tmp_path / "jobs.sqlite3", tmp_path / "settings.json")
    client = app.test_client()
    assert client.post("/api/control/start", json={}).status_code == 200
    deadline = time.time() + 2
    while not client.get("/api/status").json["last_scan_finished"] and time.time() < deadline:
        time.sleep(0.01)
    running = client.get("/api/status").json
    assert running["monitoring"] is True
    assert running["last_scan_finished"] is not None
    assert client.post("/api/control/pause", json={}).status_code == 200
    assert client.get("/api/status").json["monitoring"] is False
    app.config["CONTROLLER"].close()
    app.config["STORAGE"].close()


def test_jobs_view_lists_baseline_matches_and_all_openings(tmp_path):
    app = create_app(tmp_path / "companies.json", tmp_path / "jobs.sqlite3", tmp_path / "settings.json", start_worker=False)
    storage = app.config["STORAGE"]
    company = Company("Addepar", "https://addepar.com/careers", ["ADM Support Analyst"], [])
    storage.upsert_company(company)
    storage.save_baseline(company, [
        Job("ADM Support Analyst", "https://jobs.test/1", "Pune, India"),
        Job("Senior Product Designer", "https://jobs.test/2", "New York"),
    ])
    client = app.test_client()

    matching = client.get("/api/jobs").json
    all_openings = client.get("/api/jobs?scope=all").json
    assert [job["title"] for job in matching] == ["ADM Support Analyst"]
    assert matching[0]["location"] == "Pune, India"
    assert matching[0]["url"] == "https://jobs.test/1"
    assert len(all_openings) == 2
    assert all(job["notification_status"] == "suppressed" for job in all_openings)
    status = client.get("/api/status").json
    assert status["jobs"] == 1
    assert status["all_jobs"] == 2

    storage.upsert_company(Company("Addepar", "https://addepar.com/careers", ["Designer"], []))
    assert [job["title"] for job in client.get("/api/jobs").json] == ["Senior Product Designer"]
    assert client.get("/api/jobs?scope=invalid").status_code == 400
    storage.close()

import sqlite3

from datetime import datetime, timedelta, timezone

from job_scraper.models import Company, Job
from job_scraper.service import JobService
from job_scraper.storage import Storage


class EnrichmentScraper:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def fetch(self, url, locations=None):
        return []

    def fetch_job_details(self, company_url, job_url):
        if self.error:
            raise self.error
        return self.result


def test_legacy_database_migration_preserves_job_and_tracking_data(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE companies (name TEXT PRIMARY KEY COLLATE NOCASE, url TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]', locations TEXT NOT NULL DEFAULT '[]', baseline_done INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE jobs (company_name TEXT NOT NULL COLLATE NOCASE, url TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', notification_status TEXT NOT NULL DEFAULT 'sent', discovered_at TEXT NOT NULL DEFAULT '2025-02-03 04:05:06', application_status TEXT NOT NULL DEFAULT 'new', application_notes TEXT NOT NULL DEFAULT '', PRIMARY KEY(company_name,url));
        INSERT INTO companies(name,url) VALUES('Legacy','https://careers.legacy.test');
        INSERT INTO jobs(company_name,url,title,location,description,application_status,application_notes)
        VALUES('Legacy','https://careers.legacy.test/job/1','Old title','Remote','Previously saved JD','applied','Contact recruiter');
    """)
    connection.commit()
    connection.close()

    storage = Storage(path)
    job = storage.get_job("Legacy", "https://careers.legacy.test/job/1")

    assert job["description"] == "Previously saved JD"
    assert job["application_status"] == "applied"
    assert job["application_notes"] == "Contact recruiter"
    assert job["first_seen_at"] == "2025-02-03 04:05:06"
    assert job["last_seen_at"] == "2025-02-03 04:05:06"
    assert job["is_active"] is True
    storage.close()


def test_jobs_are_archived_after_three_successful_misses_and_reactivate_when_seen(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1", "Remote", "Full job details here.")
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    original = storage.get_job("Acme", job.url)
    assert original["first_seen_at"]
    assert original["last_seen_at"]
    assert original["is_active"] is True

    storage.record_scan("Acme", "ok", 0, seen_urls=[])
    storage.record_scan("Acme", "error", 0, "temporary failure")
    assert storage.get_job("Acme", job.url)["missing_successful_scans"] == 1
    storage.record_scan("Acme", "ok", 0, seen_urls=[])
    storage.record_scan("Acme", "ok", 0, seen_urls=[])
    archived = storage.get_job("Acme", job.url)
    assert archived["is_active"] is False
    assert archived["missing_successful_scans"] == 3

    storage.save_new_jobs(company, [job])
    reappeared = storage.get_job("Acme", job.url)
    assert reappeared["is_active"] is True
    assert reappeared["missing_successful_scans"] == 0
    storage.close()


def test_full_description_is_not_replaced_by_a_shorter_listing_summary(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1", "Remote", "Full description: " + "details " * 30)
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    storage.save_job_details("Acme", job.url, job.description)
    storage.save_new_jobs(company, [Job(job.title, job.url, job.location, "Short summary")])

    stored = storage.get_job("Acme", job.url)
    assert stored["description"] == job.description
    assert stored["description_status"] == "full"
    storage.close()


def test_new_job_description_enrichment_is_prioritized_and_saved(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    old_job = Job("Old", "https://careers.example.com/job/old")
    new_job = Job("New", "https://careers.example.com/job/new")
    storage.upsert_company(company)
    storage.save_baseline(company, [old_job])
    storage.save_new_jobs(company, [new_job])
    scraper = EnrichmentScraper({"description": "The complete description.", "location": "Remote"})
    service = JobService(storage, scraper=scraper)

    assert service.enrich_one_job() is True
    assert storage.get_job("Acme", new_job.url)["description"] == "The complete description."
    assert storage.get_job("Acme", new_job.url)["description_status"] == "full"
    assert storage.next_job_for_enrichment()["url"] == old_job.url
    storage.close()


def test_enrichment_failures_retry_later_without_blocking_scans(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("New", "https://careers.example.com/job/new")
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    service = JobService(storage, scraper=EnrichmentScraper(error=RuntimeError("portal timeout")))

    service.scan_once()
    assert storage.get_job("Acme", job.url)["description_status"] == "pending"
    assert service.enrich_one_job() is True
    stored = storage.get_job("Acme", job.url)
    assert stored["description_attempts"] == 1
    assert stored["description_error"] == "portal timeout"
    assert stored["description_retry_at"] > datetime.now(timezone.utc).isoformat()
    assert service.enrich_one_job() is False
    storage.close()


def test_sheet_sync_queue_is_idempotent_and_contains_complete_job_state(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1", "Remote", "Full JD")
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    storage.update_application_tracking("Acme", job.url, "applied", "Follow up next week")
    rows = storage.pending_sheet_jobs()

    assert len(rows) == 1
    row = rows[0]
    assert row["job_key"]
    assert row["description"] == "Full JD"
    assert row["application_status"] == "applied"
    assert row["application_notes"] == "Follow up next week"
    assert row["first_seen_at"] and row["last_seen_at"]
    storage.complete_sheet_sync(rows)
    assert storage.pending_sheet_jobs() == []
    storage.queue_all_jobs_for_sheet()
    assert len(storage.pending_sheet_jobs()) == 1
    storage.close()


def test_changing_sheet_target_requeues_entire_local_master_once(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1")
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    first_batch = storage.pending_sheet_jobs()
    storage.complete_sheet_sync(first_batch)

    storage.set_sheet_target("first-spreadsheet-id")
    assert len(storage.pending_sheet_jobs()) == 1
    storage.complete_sheet_sync(storage.pending_sheet_jobs())
    storage.set_sheet_target("first-spreadsheet-id")
    assert storage.pending_sheet_jobs() == []

    storage.set_sheet_target("second-spreadsheet-id")
    assert len(storage.pending_sheet_jobs()) == 1
    storage.close()


def test_sheet_sync_ack_does_not_drop_changes_queued_during_an_inflight_sync(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1")
    storage.upsert_company(company)
    storage.save_baseline(company, [job])
    in_flight = storage.pending_sheet_jobs()
    storage.update_application_tracking("Acme", job.url, "applied", "Changed during upload")

    storage.complete_sheet_sync(in_flight)

    latest = storage.pending_sheet_jobs()
    assert len(latest) == 1
    assert latest[0]["application_notes"] == "Changed during upload"
    storage.close()


def test_sheets_failures_record_retry_state(tmp_path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "https://careers.example.com")
    storage.upsert_company(company)
    storage.save_baseline(company, [Job("Engineer", "https://careers.example.com/job/1")])
    jobs = storage.pending_sheet_jobs()
    storage.fail_sheet_sync(jobs, "quota exceeded")

    health = storage.sheet_sync_health(configured=True)
    assert health["last_sync_status"] == "error"
    assert health["pending"] == 1
    assert health["last_sync_error"] == "quota exceeded"
    assert storage.pending_sheet_jobs() == []
    storage.close()


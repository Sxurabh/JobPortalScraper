from job_scraper.models import Company, Job
from job_scraper.portals.errors import PortalStructureError
from job_scraper.service import JobService
from job_scraper.storage import Storage


class FakeScraper:
    def __init__(self):
        self.listings = {}
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url == "broken":
            raise PortalStructureError("portal structure changed; may need maintenance")
        return list(self.listings.get(url, []))


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, *args):
        if self.fail:
            raise RuntimeError("telegram failed")
        self.sent.append(args)


def test_first_scan_is_silent_then_new_job_alerts_once(tmp_path):
    store = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "careers")
    store.upsert_company(company)
    scraper, notifier = FakeScraper(), FakeNotifier()
    scraper.listings["careers"] = [Job("Old role", "https://jobs.test/old", "Remote")]
    service = JobService(store, scraper, notifier)

    service.scan_once()
    assert notifier.sent == []
    scraper.listings["careers"].append(Job("New role", "https://jobs.test/new", "Remote"))
    service.scan_once()
    service.scan_once()

    assert len(notifier.sent) == 1
    assert notifier.sent[0][:3] == ("Acme", "New role", "Remote")
    store.close()


def test_keyword_and_location_filters_are_both_required(tmp_path):
    store = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "careers", ["engineer", "developer"], ["remote", "Toronto"])
    store.upsert_company(company)
    scraper, notifier = FakeScraper(), FakeNotifier()
    scraper.listings["careers"] = [Job("Engineer", "https://jobs.test/seed", "London")]
    service = JobService(store, scraper, notifier)
    service.scan_once()
    scraper.listings["careers"] = [
        Job("Senior Engineer", "https://jobs.test/good", "Remote - Canada"),
        Job("Designer", "https://jobs.test/keyword", "Remote"),
        Job("Engineer", "https://jobs.test/location", "London"),
    ]
    service.scan_once()
    assert [sent[3] for sent in notifier.sent] == ["https://jobs.test/good"]
    store.close()


def test_delivery_failure_is_retried_without_duplicate_job(tmp_path):
    store = Storage(tmp_path / "jobs.sqlite3")
    company = Company("Acme", "careers")
    store.upsert_company(company)
    scraper = FakeScraper()
    scraper.listings["careers"] = [Job("Seed", "https://jobs.test/seed")]
    notifier = FakeNotifier()
    service = JobService(store, scraper, notifier)
    service.scan_once()
    scraper.listings["careers"].append(Job("New", "https://jobs.test/new"))
    notifier.fail = True
    service.scan_once()
    assert len(store.pending()) == 1
    notifier.fail = False
    service.scan_once()
    service.scan_once()
    assert len(notifier.sent) == 1
    assert store.pending() == []
    store.close()


def test_portal_failure_does_not_block_other_companies(tmp_path):
    store = Storage(tmp_path / "jobs.sqlite3")
    broken = Company("Broken", "broken")
    healthy = Company("Healthy", "careers")
    store.upsert_company(broken)
    store.upsert_company(healthy)
    scraper, notifier = FakeScraper(), FakeNotifier()
    scraper.listings["careers"] = [Job("Seed", "https://jobs.test/seed")]
    service = JobService(store, scraper, notifier)
    service.scan_once()
    assert store.baseline_done("Broken") is False
    assert store.baseline_done("Healthy") is True
    health = {company["name"]: company for company in store.company_health()}
    assert health["Broken"]["last_scan_status"] == "error"
    assert "may need maintenance" in health["Broken"]["last_scan_error"]
    store.close()


def test_excluded_company_is_not_scanned_and_queued_alert_waits_until_reenabled(tmp_path):
    store = Storage(tmp_path / "jobs.sqlite3")
    paused = Company("Paused", "paused-careers", enabled=False)
    active = Company("Active", "active-careers")
    store.upsert_company(paused)
    store.upsert_company(active)
    store.save_new_jobs(paused, [Job("New role", "https://jobs.test/paused")])
    scraper, notifier = FakeScraper(), FakeNotifier()
    scraper.listings["active-careers"] = []
    service = JobService(store, scraper, notifier)

    service.scan_once()
    assert scraper.calls == ["active-careers"]
    assert notifier.sent == []
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE notification_status='pending'").fetchone()[0] == 1

    store.upsert_company(Company("Paused", "paused-careers", enabled=True))
    service.scan_once()
    assert "paused-careers" in scraper.calls
    assert notifier.sent[0][3] == "https://jobs.test/paused"
    assert store.pending() == []
    store.close()

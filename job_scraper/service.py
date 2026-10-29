import logging
import os
import threading
import time

from .notifier import TelegramNotifier
from .models import Job
from .scrapers import PortalScraper
from .storage import Storage
from .google_sheets import GoogleSheetsMirror

LOG = logging.getLogger(__name__)


class JobService:
    def __init__(self, storage: Storage, scraper=None, notifier=None):
        self.storage = storage
        self.scraper = scraper or PortalScraper()
        # Keep browser/HTTP enrichment work off the scanner's shared requests.Session.
        self.enrichment_scraper = PortalScraper() if scraper is None else self.scraper
        self.notifier = notifier or TelegramNotifier()
        self._worker_stop = threading.Event()
        self._worker_threads = []

    def scan_once(self, on_progress=None):
        companies = [company for company in self.storage.companies() if company.enabled]
        total = len(companies)
        if on_progress:
            on_progress(0, total, "")
        if not companies:
            LOG.info("No enabled companies to scan")
        for index, company in enumerate(companies, start=1):
            if on_progress:
                on_progress(index - 1, total, company.name)
            try:
                fetch_company = getattr(self.scraper, "fetch_company", None)
                jobs = fetch_company(company) if fetch_company else self.scraper.fetch(company.url)
                needs_enrichment = getattr(self.scraper, "needs_job_enrichment", None)
                if needs_enrichment:
                    enrich_urls = {job.url for job in jobs if needs_enrichment(company.url, job)}
                else:
                    enrich_urls = {job.url for job in jobs if not job.description}
                if not self.storage.baseline_done(company.name):
                    self.storage.save_baseline(company, jobs, enrich_urls=enrich_urls)
                    LOG.info("Baselined %d existing jobs for %s", len(jobs), company.name)
                else:
                    self.storage.save_new_jobs(company, jobs, enrich_urls=enrich_urls)
                    LOG.info("Scanned %s and found %d listings", company.name, len(jobs))
                self.storage.record_scan(company.name, "ok", len(jobs), seen_urls=[job.url for job in jobs])
            except Exception as exc:
                self.storage.record_scan(company.name, "error", 0, str(exc))
                LOG.exception("Scan failed for %s (%s)", company.name, company.url)
            finally:
                if on_progress:
                    on_progress(index, total, company.name)
        self._deliver_pending()

    def start_background_workers(self, settings_provider=None):
        if self._worker_threads:
            return
        self._queue_existing_enrichment()
        self._worker_stop.clear()
        provider = settings_provider or (lambda: {
            "google_spreadsheet_id": os.getenv("GOOGLE_SPREADSHEET_ID", ""),
        })
        self._worker_threads = [
            threading.Thread(target=self._enrichment_loop, name="job-description-enricher", daemon=True),
            threading.Thread(target=self._sheets_loop, args=(provider,), name="google-sheets-sync", daemon=True),
        ]
        for worker in self._worker_threads:
            worker.start()

    def _queue_existing_enrichment(self):
        needs_enrichment = getattr(self.enrichment_scraper, "needs_job_enrichment", None)
        for row in self.storage.all_job_rows():
            job = Job(row["title"], row["url"], row["location"], row["description"])
            should_fetch = needs_enrichment(row["company_url"], job) if needs_enrichment else not job.description
            if should_fetch and row["description_status"] != "full":
                self.storage.queue_job_enrichment(row["company_name"], row["url"])

    def close_background_workers(self):
        self._worker_stop.set()
        for worker in self._worker_threads:
            if worker.is_alive():
                worker.join(timeout=3)
        self._worker_threads = []

    def _enrichment_loop(self):
        while not self._worker_stop.is_set():
            if not self.enrich_one_job():
                self._worker_stop.wait(3)

    def enrich_one_job(self):
        row = self.storage.next_job_for_enrichment()
        if not row:
            return False
        try:
            fetch_details = getattr(self.enrichment_scraper, "fetch_job_details", None)
            if not fetch_details:
                self.storage.fail_job_details(row["company_name"], row["url"], "No detail fetcher is available")
                return True
            details = fetch_details(row["company_url"], row["url"])
            description = (details or {}).get("description", "").strip()
            location = (details or {}).get("location", "").strip()
            if description:
                self.storage.save_job_details(row["company_name"], row["url"], description, location)
                LOG.info("Enriched job description for %s at %s", row["company_name"], row["url"])
            else:
                self.storage.fail_job_details(row["company_name"], row["url"], "Portal returned no full job description")
        except Exception as exc:
            self.storage.fail_job_details(row["company_name"], row["url"], str(exc))
            LOG.warning("Job description enrichment failed for %s (%s)", row["url"], type(exc).__name__)
        return True

    def _sheets_loop(self, settings_provider):
        mirror = None
        current_id = None
        while not self._worker_stop.is_set():
            spreadsheet_id = (settings_provider().get("google_spreadsheet_id") or "").strip()
            if not spreadsheet_id:
                self._worker_stop.wait(5)
                continue
            if spreadsheet_id != current_id:
                current_id = spreadsheet_id
                mirror = GoogleSheetsMirror(spreadsheet_id)
            jobs = self.storage.pending_sheet_jobs(limit=100)
            if not jobs:
                self._worker_stop.wait(3)
                continue
            try:
                mirror.sync_jobs(jobs)
                self.storage.complete_sheet_sync(jobs)
                LOG.info("Synced %d job records to Google Sheets", len(jobs))
            except Exception as exc:
                self.storage.fail_sheet_sync(jobs, str(exc))
                LOG.warning("Google Sheets sync failed: %s", exc)
                self._worker_stop.wait(5)

    def _deliver_pending(self):
        for row in self.storage.pending():
            try:
                self.notifier.send(row["company_name"], row["title"], row["location"], row["url"])
                self.storage.mark_sent(row["company_name"], row["url"])
            except Exception:
                LOG.exception("Telegram delivery failed for %s", row["url"])

    def run_forever(self, interval_seconds: int = 1800):
        self.start_background_workers()
        try:
            while True:
                self.scan_once()
                time.sleep(interval_seconds)
        finally:
            self.close_background_workers()

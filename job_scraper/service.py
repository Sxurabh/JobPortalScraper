import logging
import time

from .notifier import TelegramNotifier
from .scrapers import PortalScraper
from .storage import Storage

LOG = logging.getLogger(__name__)


class JobService:
    def __init__(self, storage: Storage, scraper=None, notifier=None):
        self.storage = storage
        self.scraper = scraper or PortalScraper()
        self.notifier = notifier or TelegramNotifier()

    def scan_once(self):
        for company in self.storage.companies():
            try:
                jobs = self.scraper.fetch(company.url)
                if not self.storage.baseline_done(company.name):
                    self.storage.save_baseline(company, jobs)
                    LOG.info("Baselined %d existing jobs for %s", len(jobs), company.name)
                else:
                    self.storage.save_new_jobs(company, jobs)
                    LOG.info("Scanned %s and found %d listings", company.name, len(jobs))
                self.storage.record_scan(company.name, "ok", len(jobs))
            except Exception as exc:
                self.storage.record_scan(company.name, "error", 0, str(exc))
                LOG.exception("Scan failed for %s (%s)", company.name, company.url)
        self._deliver_pending()

    def _deliver_pending(self):
        for row in self.storage.pending():
            try:
                self.notifier.send(row["company_name"], row["title"], row["location"], row["url"])
                self.storage.mark_sent(row["company_name"], row["url"])
            except Exception:
                LOG.exception("Telegram delivery failed for %s", row["url"])

    def run_forever(self, interval_seconds: int = 1800):
        while True:
            self.scan_once()
            time.sleep(interval_seconds)

import json
import hashlib
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import Company, Job


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                url TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                locations TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                baseline_done INTEGER NOT NULL DEFAULT 0,
                last_scanned_at TEXT,
                last_scan_status TEXT NOT NULL DEFAULT 'waiting',
                last_scan_error TEXT,
                last_job_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                company_name TEXT NOT NULL COLLATE NOCASE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                notification_status TEXT NOT NULL DEFAULT 'suppressed',
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                missing_successful_scans INTEGER NOT NULL DEFAULT 0,
                description_status TEXT NOT NULL DEFAULT 'pending',
                description_priority INTEGER NOT NULL DEFAULT 0,
                description_attempts INTEGER NOT NULL DEFAULT 0,
                description_retry_at TEXT,
                description_error TEXT,
                PRIMARY KEY (company_name, url),
                FOREIGN KEY (company_name) REFERENCES companies(name) ON DELETE CASCADE
            );
            """
        )
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_sync_queue (
                company_name TEXT NOT NULL COLLATE NOCASE,
                url TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at TEXT,
                last_error TEXT,
                PRIMARY KEY (company_name, url)
            );
            CREATE TABLE IF NOT EXISTS integration_status (
                name TEXT PRIMARY KEY,
                target_id TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT,
                last_sync_status TEXT NOT NULL DEFAULT 'not_configured',
                last_sync_error TEXT
            );
            INSERT OR IGNORE INTO integration_status(name) VALUES('google_sheets');
            """
        )
        # Additive migration for databases created by earlier scraper versions.
        company_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(companies)")}
        integration_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(integration_status)")}
        if "target_id" not in integration_columns:
            self.db.execute("ALTER TABLE integration_status ADD COLUMN target_id TEXT NOT NULL DEFAULT ''")
        for column, declaration in {
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_scanned_at": "TEXT",
            "last_scan_status": "TEXT NOT NULL DEFAULT 'waiting'",
            "last_scan_error": "TEXT",
            "last_job_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in company_columns:
                self.db.execute(f"ALTER TABLE companies ADD COLUMN {column} {declaration}")
        job_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        sync_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sheet_sync_queue)")}
        if "revision" not in sync_columns:
            self.db.execute("ALTER TABLE sheet_sync_queue ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if "discovered_at" not in job_columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN discovered_at TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE jobs SET discovered_at=CURRENT_TIMESTAMP WHERE discovered_at=''")
        for column, declaration in {
            "application_status": "TEXT NOT NULL DEFAULT 'new'",
            "application_notes": "TEXT NOT NULL DEFAULT ''",
            "first_seen_at": "TEXT NOT NULL DEFAULT ''",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "missing_successful_scans": "INTEGER NOT NULL DEFAULT 0",
            "description_status": "TEXT NOT NULL DEFAULT 'pending'",
            "description_priority": "INTEGER NOT NULL DEFAULT 0",
            "description_attempts": "INTEGER NOT NULL DEFAULT 0",
            "description_retry_at": "TEXT",
            "description_error": "TEXT",
        }.items():
            if column not in job_columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
        self.db.execute(
            "UPDATE jobs SET first_seen_at=discovered_at WHERE first_seen_at='' OR first_seen_at IS NULL"
        )
        self.db.execute(
            "UPDATE jobs SET last_seen_at=discovered_at WHERE last_seen_at='' OR last_seen_at IS NULL"
        )
        self.db.execute(
            "UPDATE jobs SET description_status=CASE WHEN description<>'' THEN 'listing' ELSE 'pending' END "
            "WHERE description_status='pending' AND description<>''"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_company_active_idx ON jobs(company_name,is_active)")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_enrichment_idx ON jobs(description_status,description_retry_at,description_priority)")
        self.db.execute("CREATE INDEX IF NOT EXISTS sheet_sync_retry_idx ON sheet_sync_queue(retry_at)")
        self.db.commit()

    def close(self):
        self.db.close()

    def upsert_company(self, company: Company):
        with self.lock:
            self.db.execute(
                "INSERT INTO companies(name,url,keywords,locations,enabled) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url,keywords=excluded.keywords,locations=excluded.locations,enabled=excluded.enabled",
                (company.name, company.url, json.dumps(company.keywords), json.dumps(company.locations), int(company.enabled)),
            )
            self.db.commit()

    def remove_company(self, name: str) -> bool:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sheet_sync_queue WHERE company_name=? COLLATE NOCASE", (name,))
            cur = self.db.execute("DELETE FROM companies WHERE name = ? COLLATE NOCASE", (name,))
            return cur.rowcount > 0

    def companies(self) -> list[Company]:
        with self.lock:
            return [Company(row["name"], row["url"], json.loads(row["keywords"]), json.loads(row["locations"]), bool(row["enabled"]))
                    for row in self.db.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE")]

    def baseline_done(self, name: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT baseline_done FROM companies WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            return bool(row and row[0])

    def save_baseline(self, company: Company, jobs: list[Job], enrich_urls=()):
        new_urls = self._save_scanned_jobs(company, jobs, baseline=True, enrich_urls=set(enrich_urls))
        with self.lock, self.db:
            self.db.execute("UPDATE companies SET baseline_done=1 WHERE name=? COLLATE NOCASE", (company.name,))
        return new_urls

    def save_new_jobs(self, company: Company, jobs: list[Job], enrich_urls=()):
        return self._save_scanned_jobs(company, jobs, baseline=False, enrich_urls=set(enrich_urls))

    def _save_scanned_jobs(self, company: Company, jobs: list[Job], *, baseline: bool, enrich_urls: set[str]):
        new_urls = []
        now = _now()
        with self.lock, self.db:
            for job in jobs:
                status = "suppressed" if baseline else ("pending" if company.matches(job) else "suppressed")
                if not baseline:
                    self._update_existing_workday_url(company.name, job, status)
                existing = self.db.execute(
                    "SELECT description,description_status,notification_status FROM jobs "
                    "WHERE company_name=? COLLATE NOCASE AND url=?",
                    (company.name, job.url),
                ).fetchone()
                if existing:
                    current_description = existing["description"] or ""
                    incoming_description = job.description or ""
                    needs_enrichment = (job.url in enrich_urls and existing["description_status"] != "full") or not incoming_description
                    if incoming_description and (
                        not current_description
                        or (existing["description_status"] != "complete" and len(incoming_description) >= len(current_description))
                    ):
                        description = incoming_description
                    else:
                        description = current_description
                    if existing["description_status"] == "full" and description:
                        description_status = "full"
                    elif needs_enrichment:
                        description_status = "pending"
                    else:
                        description_status = "listing" if description else "pending"
                    notification_status = existing["notification_status"]
                    self.db.execute(
                        "UPDATE jobs SET title=?,location=CASE WHEN ?<>'' THEN ? ELSE location END,description=?,"
                        "description_status=?,last_seen_at=?,is_active=1,missing_successful_scans=0 "
                        "WHERE company_name=? COLLATE NOCASE AND url=?",
                        (job.title, job.location, job.location, description, description_status, now, company.name, job.url),
                    )
                else:
                    description_status = "pending" if job.url in enrich_urls or not job.description else "listing"
                    self.db.execute(
                        "INSERT INTO jobs(company_name,url,title,location,description,notification_status,discovered_at,"
                    "first_seen_at,last_seen_at,is_active,missing_successful_scans,description_status,description_priority) "
                        "VALUES(?,?,?,?,?,?,?,?,?,1,0,?,?)",
                        (company.name, job.url, job.title, job.location, job.description, status, now, now, now, description_status, 1 if job.url in enrich_urls else 0),
                    )
                    new_urls.append(job.url)
                self._queue_sheet_sync(company.name, job.url)
                if description_status == "pending":
                    priority = 1 if (not baseline and job.url in new_urls) else 0
                    self.db.execute(
                        "UPDATE jobs SET description_retry_at=COALESCE(description_retry_at,?) "
                        "WHERE company_name=? COLLATE NOCASE AND url=?",
                        (now, company.name, job.url),
                    )
                    self._promote_enrichment(company.name, job.url, priority)
        return new_urls

    def _promote_enrichment(self, company_name: str, url: str, priority: int = 0):
        self.db.execute(
            "UPDATE jobs SET description_priority=MAX(description_priority,?) "
            "WHERE company_name=? COLLATE NOCASE AND url=? AND description_status='pending'",
            (priority, company_name, url),
        )

    def _queue_sheet_sync(self, company_name: str, url: str):
        self.db.execute(
            "INSERT INTO sheet_sync_queue(company_name,url) VALUES(?,?) "
            "ON CONFLICT(company_name,url) DO UPDATE SET revision=revision+1,retry_at=NULL,attempts=0,last_error=NULL",
            (company_name, url),
        )

    def _update_existing_workday_url(self, company_name: str, job: Job, status: str):
        requisition_id = _workday_requisition_id(job.url)
        if not requisition_id:
            return
        rows = self.db.execute(
            "SELECT url,notification_status FROM jobs WHERE company_name=? COLLATE NOCASE",
            (company_name,),
        ).fetchall()
        prior = next(
            (row for row in rows if row["url"] != job.url and _workday_requisition_id(row["url"]) == requisition_id),
            None,
        )
        if not prior:
            return
        current = self.db.execute(
            "SELECT notification_status,application_status,application_notes FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
            (company_name, job.url),
        ).fetchone()
        if current:
            old_status = prior["notification_status"]
            merged_status = "sent" if "sent" in {old_status, current["notification_status"]} else (
                "pending" if "pending" in {old_status, current["notification_status"]} else status
            )
            prior_tracking = self.db.execute(
                "SELECT application_status,application_notes,description,description_status,first_seen_at,last_seen_at "
                "FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            ).fetchone()
            tracking_status = current["application_status"] if current["application_status"] != "new" else prior_tracking["application_status"]
            tracking_notes = current["application_notes"] or prior_tracking["application_notes"]
            self.db.execute(
                "UPDATE jobs SET notification_status=?,application_status=?,application_notes=?,"
                "description=CASE WHEN description_status='full' THEN description WHEN ?='full' THEN ? "
                "WHEN description='' THEN ? ELSE description END,"
                "description_status=CASE WHEN description_status='full' OR ?='full' THEN 'full' ELSE description_status END,"
                "first_seen_at=MIN(first_seen_at,?),last_seen_at=MAX(last_seen_at,?) "
                "WHERE company_name=? COLLATE NOCASE AND url=?",
                (merged_status, tracking_status, tracking_notes,
                 prior_tracking["description_status"], prior_tracking["description"], prior_tracking["description"],
                 prior_tracking["description_status"], prior_tracking["first_seen_at"], prior_tracking["last_seen_at"],
                 company_name, job.url),
            )
            self.db.execute(
                "DELETE FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            )
            self.db.execute(
                "DELETE FROM sheet_sync_queue WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            )
        else:
            self.db.execute(
                "UPDATE jobs SET url=?,title=?,location=CASE WHEN ?<>'' THEN ? ELSE location END,"
                "description=CASE WHEN description='' OR (description_status<>'full' AND length(?)>=length(description)) THEN ? ELSE description END,"
                "description_status=CASE WHEN description_status='full' THEN 'full' WHEN ?<>'' THEN 'listing' ELSE 'pending' END,"
                "last_seen_at=?,is_active=1,missing_successful_scans=0 WHERE company_name=? COLLATE NOCASE AND url=?",
                (job.url, job.title, job.location, job.location, job.description, job.description,
                 job.description, _now(), company_name, prior["url"]),
            )
            self.db.execute(
                "DELETE FROM sheet_sync_queue WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            )

    def pending(self):
        with self.lock:
            return self.db.execute(
                "SELECT j.company_name,j.url,j.title,j.location,j.description FROM jobs j "
                "JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "WHERE j.notification_status='pending' AND c.enabled=1 ORDER BY j.rowid"
            ).fetchall()

    def mark_sent(self, company_name: str, url: str):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET notification_status='sent' WHERE company_name=? AND url=?", (company_name, url))

    def record_scan(self, name: str, status: str, job_count: int = 0, error: str | None = None, seen_urls=()):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE companies SET last_scanned_at=CURRENT_TIMESTAMP,last_scan_status=?,last_scan_error=?,last_job_count=? WHERE name=? COLLATE NOCASE",
                (status, error, job_count, name),
            )
            if status == "ok":
                seen = set(seen_urls)
                for url in seen:
                    self.db.execute(
                        "UPDATE jobs SET missing_successful_scans=0,is_active=1 "
                        "WHERE company_name=? COLLATE NOCASE AND url=?",
                        (name, url),
                    )
                missing_rows = [
                    row for row in self.db.execute(
                        "SELECT url FROM jobs WHERE company_name=? COLLATE NOCASE AND is_active=1", (name,)
                    ).fetchall()
                    if row["url"] not in seen
                ]
                for row in missing_rows:
                    self.db.execute(
                        "UPDATE jobs SET missing_successful_scans=missing_successful_scans+1,"
                        "is_active=CASE WHEN missing_successful_scans+1>=3 THEN 0 ELSE 1 END "
                        "WHERE company_name=? COLLATE NOCASE AND url=?",
                        (name, row["url"]),
                    )
                    self._queue_sheet_sync(name, row["url"])

    def all_job_rows(self):
        with self.lock:
            return self.db.execute(
                "SELECT j.*,c.url AS company_url FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "ORDER BY j.discovered_at,j.company_name"
            ).fetchall()

    def queue_job_enrichment(self, company_name: str, url: str, priority: int = 0):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET description_status='pending',description_retry_at=COALESCE(description_retry_at,?),"
                "description_priority=MAX(description_priority,?) "
                "WHERE company_name=? COLLATE NOCASE AND url=? AND description_status<>'full'",
                (_now(), priority, company_name, url),
            )

    def next_job_for_enrichment(self):
        with self.lock:
            row = self.db.execute(
                "SELECT j.*,c.url AS company_url FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "WHERE c.enabled=1 AND j.description_status='pending' "
                "AND (j.description_retry_at IS NULL OR j.description_retry_at<=?) "
                "ORDER BY j.description_priority DESC,j.notification_status='pending' DESC,j.discovered_at DESC LIMIT 1",
                (_now(),),
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def save_job_details(self, company_name: str, url: str, description: str, location: str = "", *, complete: bool = True):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET description=CASE WHEN ?<>'' THEN ? ELSE description END,"
                "location=CASE WHEN ?<>'' THEN ? ELSE location END,"
                "description_status=CASE WHEN ?=1 AND ?<>'' THEN 'full' ELSE description_status END,"
                "description_attempts=CASE WHEN ?=1 THEN 0 ELSE description_attempts END,"
                "description_retry_at=CASE WHEN ?=1 THEN NULL ELSE description_retry_at END,"
                "description_error=CASE WHEN ?=1 THEN NULL ELSE description_error END,"
                "description_priority=CASE WHEN ?=1 THEN 0 ELSE description_priority END "
                "WHERE company_name=? COLLATE NOCASE AND url=?",
                (description, description, location, location, int(complete), description,
                 int(complete), int(complete), int(complete), int(complete), company_name, url),
            )
            self._queue_sheet_sync(company_name, url)

    def fail_job_details(self, company_name: str, url: str, error: str):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT description_attempts FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, url),
            ).fetchone()
            if not row:
                return
            attempts = row["description_attempts"] + 1
            retry_minutes = (1, 5, 30, 360, 1440)[min(attempts - 1, 4)]
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)).isoformat()
            self.db.execute(
                "UPDATE jobs SET description_attempts=?,description_retry_at=?,description_error=? "
                "WHERE company_name=? COLLATE NOCASE AND url=?",
                (attempts, retry_at, str(error)[:1000], company_name, url),
            )

    def pending_job_enrichment_count(self):
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "WHERE j.description_status='pending' AND c.enabled=1"
            ).fetchone()[0]

    def queue_all_jobs_for_sheet(self):
        with self.lock, self.db:
            rows = self.db.execute("SELECT company_name,url FROM jobs").fetchall()
            for row in rows:
                self._queue_sheet_sync(row["company_name"], row["url"])

    def set_sheet_target(self, spreadsheet_id: str):
        spreadsheet_id = (spreadsheet_id or "").strip()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT target_id FROM integration_status WHERE name='google_sheets'"
            ).fetchone()
            previous = row["target_id"] if row else ""
            if spreadsheet_id != previous:
                if spreadsheet_id:
                    jobs = self.db.execute("SELECT company_name,url FROM jobs").fetchall()
                    for job in jobs:
                        self._queue_sheet_sync(job["company_name"], job["url"])
                self.db.execute(
                    "UPDATE integration_status SET target_id=? WHERE name='google_sheets'",
                    (spreadsheet_id,),
                )

    def pending_sheet_jobs(self, limit: int = 100):
        with self.lock:
            queued = self.db.execute(
                "SELECT q.company_name,q.url,q.revision FROM sheet_sync_queue q "
                "WHERE q.retry_at IS NULL OR q.retry_at<=? ORDER BY q.rowid LIMIT ?",
                (_now(), limit),
            ).fetchall()
            jobs = []
            for item in queued:
                row = self.db.execute(
                    "SELECT j.*,c.url AS company_url FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                    "WHERE j.company_name=? COLLATE NOCASE AND j.url=?",
                    (item["company_name"], item["url"]),
                ).fetchone()
                if row:
                    record = dict(row)
                    record["job_key"] = _stable_job_key(record["company_name"], record["url"])
                    record["_sync_revision"] = item["revision"]
                    jobs.append(record)
            return jobs

    def complete_sheet_sync(self, jobs):
        with self.lock, self.db:
            self.db.executemany(
                "DELETE FROM sheet_sync_queue WHERE company_name=? COLLATE NOCASE AND url=? AND revision=?",
                [(job["company_name"], job["url"], job["_sync_revision"]) for job in jobs],
            )
            self.db.execute(
                "UPDATE integration_status SET last_sync_at=?,last_sync_status='ok',last_sync_error=NULL WHERE name='google_sheets'",
                (_now(),),
            )

    def fail_sheet_sync(self, jobs, error: str):
        with self.lock, self.db:
            for job in jobs:
                row = self.db.execute(
                    "SELECT attempts FROM sheet_sync_queue WHERE company_name=? COLLATE NOCASE AND url=? AND revision=?",
                    (job["company_name"], job["url"], job["_sync_revision"]),
                ).fetchone()
                if not row:
                    continue
                attempts = row["attempts"] + 1
                retry_minutes = (1, 5, 30, 360, 1440)[min(attempts - 1, 4)]
                retry_at = (datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)).isoformat()
                self.db.execute(
                    "UPDATE sheet_sync_queue SET attempts=?,retry_at=?,last_error=? WHERE company_name=? COLLATE NOCASE AND url=? AND revision=?",
                    (attempts, retry_at, str(error)[:1000], job["company_name"], job["url"], job["_sync_revision"]),
                )
            self.db.execute(
                "UPDATE integration_status SET last_sync_status='error',last_sync_error=? WHERE name='google_sheets'",
                (str(error)[:1000],),
            )

    def sheet_sync_health(self, configured: bool = False, credentials_configured: bool = False):
        with self.lock:
            state = self.db.execute(
                "SELECT last_sync_at,last_sync_status,last_sync_error FROM integration_status WHERE name='google_sheets'"
            ).fetchone()
            pending = self.db.execute("SELECT COUNT(*) FROM sheet_sync_queue").fetchone()[0]
            result = dict(state) if state else {}
            result["pending"] = pending
            result["configured"] = bool(configured)
            result["credentials_configured"] = bool(credentials_configured)
            if not configured:
                result["last_sync_status"] = "not_configured"
            return result

    def company_health(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT name,url,keywords,locations,enabled,last_scanned_at,last_scan_status,last_scan_error,last_job_count,baseline_done FROM companies ORDER BY name COLLATE NOCASE"
            ).fetchall()
            companies = [dict(row) for row in rows]
            for company in companies:
                company["enabled"] = bool(company["enabled"])
            return companies

    def recent_jobs(self, limit: int = 1000, scope: str = "matching") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
            "SELECT j.company_name,j.url,j.title,j.location,j.description,j.notification_status,j.discovered_at,"
            "j.first_seen_at,j.last_seen_at,j.is_active,j.description_status,j.application_status,j.application_notes "
                "FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "ORDER BY j.rowid DESC",
            ).fetchall()
            if scope not in {"matching", "all"}:
                raise ValueError("scope must be 'matching' or 'all'")
            if scope == "all":
                return [_with_job_key(dict(row)) for row in rows[:limit]]

            companies = {
                company.name.casefold(): company
                for company in self.companies()
            }
            matches = []
            for row in rows:
                company = companies.get(row["company_name"].casefold())
                job = Job(row["title"], row["url"], row["location"], row["description"])
                if company and company.matches(job):
                    matches.append(_with_job_key(dict(row)))
                    if len(matches) >= limit:
                        break
            return matches

    def update_application_tracking(self, company_name: str, url: str, status: str, notes: str) -> bool:
        allowed = {"new", "interested", "applied", "interviewing", "offer", "rejected", "withdrawn"}
        if status not in allowed:
            raise ValueError("Unsupported application status")
        with self.lock, self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET application_status=?,application_notes=? WHERE company_name=? COLLATE NOCASE AND url=?",
                (status, notes.strip(), company_name, url),
            )
            if cursor.rowcount:
                self._queue_sheet_sync(company_name, url)
            return cursor.rowcount > 0

    def get_job(self, company_name: str, url: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT company_name,url,title,location,description,discovered_at,first_seen_at,last_seen_at,"
                "is_active,missing_successful_scans,description_status,description_attempts,description_retry_at,"
                "description_error,notification_status,application_status,application_notes "
                "FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, url),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["is_active"] = bool(result["is_active"])
            return result

    def dashboard_counts(self) -> dict:
        with self.lock:
            all_jobs = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            return {
                "companies": self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
                "jobs": len(self.recent_jobs(limit=all_jobs or 1, scope="matching")),
                "all_jobs": all_jobs,
                "pending": self.db.execute("SELECT COUNT(*) FROM jobs WHERE notification_status='pending'").fetchone()[0],
            }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stable_job_key(company_name: str, url: str) -> str:
    parsed = urlparse(url)
    requisition_id = _workday_requisition_id(url)
    identity = f"workday:{requisition_id}" if requisition_id else f"{(parsed.hostname or '').casefold()}{parsed.path.rstrip('/')}"
    digest = hashlib.sha256(f"{company_name.casefold()}|{identity}".encode("utf-8")).hexdigest()
    return digest


def _with_job_key(row: dict) -> dict:
    row["is_active"] = bool(row.get("is_active", True))
    row["job_key"] = _stable_job_key(row["company_name"], row["url"])
    return row


def _workday_requisition_id(url: str) -> str:
    hostname = (urlparse(url).hostname or "").casefold()
    if not hostname.endswith(("myworkdayjobs.com", "myworkdaysite.com")):
        return ""
    match = re.search(r"_((?:JR|R)-?[A-Z0-9-]+)$", urlparse(url).path, re.IGNORECASE)
    return match.group(1).casefold() if match else ""


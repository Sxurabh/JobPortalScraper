import json
import re
import sqlite3
import threading
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
                PRIMARY KEY (company_name, url),
                FOREIGN KEY (company_name) REFERENCES companies(name) ON DELETE CASCADE
            );
            """
        )
        # Additive migration for databases created by earlier scraper versions.
        company_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(companies)")}
        for column, declaration in {
            "last_scanned_at": "TEXT",
            "last_scan_status": "TEXT NOT NULL DEFAULT 'waiting'",
            "last_scan_error": "TEXT",
            "last_job_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in company_columns:
                self.db.execute(f"ALTER TABLE companies ADD COLUMN {column} {declaration}")
        job_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "discovered_at" not in job_columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN discovered_at TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE jobs SET discovered_at=CURRENT_TIMESTAMP WHERE discovered_at=''")
        for column, declaration in {
            "application_status": "TEXT NOT NULL DEFAULT 'new'",
            "application_notes": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in job_columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
        self.db.commit()

    def close(self):
        self.db.close()

    def upsert_company(self, company: Company):
        with self.lock:
            self.db.execute(
                "INSERT INTO companies(name,url,keywords,locations) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url,keywords=excluded.keywords,locations=excluded.locations",
                (company.name, company.url, json.dumps(company.keywords), json.dumps(company.locations)),
            )
            self.db.commit()

    def remove_company(self, name: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM companies WHERE name = ? COLLATE NOCASE", (name,))
            self.db.commit()
            return cur.rowcount > 0

    def companies(self) -> list[Company]:
        with self.lock:
            return [Company(row["name"], row["url"], json.loads(row["keywords"]), json.loads(row["locations"]))
                    for row in self.db.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE")]

    def baseline_done(self, name: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT baseline_done FROM companies WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            return bool(row and row[0])

    def save_baseline(self, company: Company, jobs: list[Job]):
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO jobs(company_name,url,title,location,description,notification_status) VALUES(?,?,?,?,?,'suppressed')",
                [(company.name, j.url, j.title, j.location, j.description) for j in jobs],
            )
            self.db.execute("UPDATE companies SET baseline_done=1 WHERE name=? COLLATE NOCASE", (company.name,))

    def save_new_jobs(self, company: Company, jobs: list[Job]):
        with self.lock, self.db:
            for job in jobs:
                status = "pending" if company.matches(job) else "suppressed"
                self._update_existing_workday_url(company.name, job, status)
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(company_name,url,title,location,description,notification_status) VALUES(?,?,?,?,?,?)",
                    (company.name, job.url, job.title, job.location, job.description, status),
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
                "SELECT application_status,application_notes FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            ).fetchone()
            tracking_status = current["application_status"] if current["application_status"] != "new" else prior_tracking["application_status"]
            tracking_notes = current["application_notes"] or prior_tracking["application_notes"]
            self.db.execute(
                "UPDATE jobs SET notification_status=?,application_status=?,application_notes=? WHERE company_name=? COLLATE NOCASE AND url=?",
                (merged_status, tracking_status, tracking_notes, company_name, job.url),
            )
            self.db.execute(
                "DELETE FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, prior["url"]),
            )
        else:
            self.db.execute(
                "UPDATE jobs SET url=?,title=?,location=?,description=? WHERE company_name=? COLLATE NOCASE AND url=?",
                (job.url, job.title, job.location, job.description, company_name, prior["url"]),
            )

    def pending(self):
        with self.lock:
            return self.db.execute(
                "SELECT company_name,url,title,location,description FROM jobs WHERE notification_status='pending' ORDER BY rowid"
            ).fetchall()

    def mark_sent(self, company_name: str, url: str):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET notification_status='sent' WHERE company_name=? AND url=?", (company_name, url))

    def record_scan(self, name: str, status: str, job_count: int = 0, error: str | None = None):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE companies SET last_scanned_at=CURRENT_TIMESTAMP,last_scan_status=?,last_scan_error=?,last_job_count=? WHERE name=? COLLATE NOCASE",
                (status, error, job_count, name),
            )

    def company_health(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT name,url,keywords,locations,last_scanned_at,last_scan_status,last_scan_error,last_job_count,baseline_done FROM companies ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_jobs(self, limit: int = 1000, scope: str = "matching") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
            "SELECT j.company_name,j.url,j.title,j.location,j.description,j.notification_status,j.discovered_at,j.application_status,j.application_notes "
                "FROM jobs j JOIN companies c ON c.name=j.company_name COLLATE NOCASE "
                "ORDER BY j.rowid DESC",
            ).fetchall()
            if scope not in {"matching", "all"}:
                raise ValueError("scope must be 'matching' or 'all'")
            if scope == "all":
                return [dict(row) for row in rows[:limit]]

            companies = {
                company.name.casefold(): company
                for company in self.companies()
            }
            matches = []
            for row in rows:
                company = companies.get(row["company_name"].casefold())
                job = Job(row["title"], row["url"], row["location"], row["description"])
                if company and company.matches(job):
                    matches.append(dict(row))
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
            return cursor.rowcount > 0

    def get_job(self, company_name: str, url: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT company_name,url,title,location,description,discovered_at,application_status,application_notes "
                "FROM jobs WHERE company_name=? COLLATE NOCASE AND url=?",
                (company_name, url),
            ).fetchone()
            return dict(row) if row else None

    def save_job_details(self, company_name: str, url: str, description: str, location: str = ""):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobs SET description=CASE WHEN ?<>'' THEN ? ELSE description END,location=CASE WHEN ?<>'' THEN ? ELSE location END "
                "WHERE company_name=? COLLATE NOCASE AND url=?",
                (description, description, location, location, company_name, url),
            )

    def dashboard_counts(self) -> dict:
        with self.lock:
            all_jobs = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            return {
                "companies": self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
                "jobs": len(self.recent_jobs(limit=all_jobs or 1, scope="matching")),
                "all_jobs": all_jobs,
                "pending": self.db.execute("SELECT COUNT(*) FROM jobs WHERE notification_status='pending'").fetchone()[0],
            }


def _workday_requisition_id(url: str) -> str:
    if "myworkdayjobs.com" not in (urlparse(url).hostname or "").casefold():
        return ""
    match = re.search(r"_((?:JR|R)-?[A-Z0-9-]+)$", urlparse(url).path, re.IGNORECASE)
    return match.group(1).casefold() if match else ""


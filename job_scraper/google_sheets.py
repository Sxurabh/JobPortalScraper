"""Google Sheets mirror client using a service-account credential file."""

import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from .storage import _stable_job_key


SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
JOB_COLUMNS = (
    "job_key", "company_name", "title", "location", "url", "description",
    "first_seen_at", "last_seen_at", "is_active", "notification_status",
    "application_status", "application_notes",
)

GITHUB_JOB_COLUMNS = (
    "job_key", "company_name", "company_url", "title", "location", "url", "description",
    "description_status", "notification_status", "discovered_at", "first_seen_at", "last_seen_at",
    "is_active", "missing_successful_scans", "description_attempts", "description_retry_at",
    "description_error", "description_priority", "application_status", "application_notes",
)
GITHUB_STATE_COLUMNS = (
    "name", "url", "keywords", "locations", "enabled", "baseline_done", "last_scanned_at",
    "last_successful_scan_at", "last_scan_status", "last_scan_error", "last_scan_error_type",
    "scan_failure_streak", "next_scan_retry_at", "last_job_count",
)


class GoogleSheetsMirror:
    def __init__(self, spreadsheet_id: str, *, credentials_file: str | None = None, session=None):
        self.spreadsheet_id = spreadsheet_id.strip()
        self.credentials_file = credentials_file or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        self.session = session or requests
        self._credentials = None

    def configured(self) -> bool:
        return bool(self.spreadsheet_id and self.credentials_file)

    def sync_jobs(self, jobs: list[dict]):
        if not self.spreadsheet_id:
            raise RuntimeError("Set a Google spreadsheet ID in Settings.")
        if not self.credentials_file or not Path(self.credentials_file).is_file():
            raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_FILE to a readable service-account JSON file.")
        self._ensure_jobs_tab()
        values_response = self._request(
            "GET", f"{self._sheet_url()}/values/Jobs!A:A"
        )
        values = values_response.get("values", [])
        rows_by_key = {}
        for row_number, row in enumerate(values[1:], start=2):
            if row and row[0] and row[0] not in rows_by_key:
                rows_by_key[row[0]] = row_number

        updates = []
        additions = []
        deduplicated = {job["job_key"]: job for job in jobs}
        for job in deduplicated.values():
            row = [self._cell(job, column) for column in JOB_COLUMNS]
            existing_row = rows_by_key.get(job["job_key"])
            if existing_row:
                updates.append({"range": f"Jobs!A{existing_row}:L{existing_row}", "values": [row]})
            else:
                additions.append(row)
                rows_by_key[job["job_key"]] = -1

        if updates:
            self._request(
                "POST", f"{self._sheet_url()}/values:batchUpdate",
                params={"valueInputOption": "RAW"},
                body={"data": updates, "valueInputOption": "RAW"},
            )
        if additions:
            self._request(
                "POST", f"{self._sheet_url()}/values/Jobs!A:L:append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                body={"values": additions},
            )

    @staticmethod
    def _cell(job: dict, column: str):
        value = job.get(column, "")
        if column == "is_active":
            return "Active" if value else "Inactive"
        return "" if value is None else str(value)

    def _ensure_jobs_tab(self):
        spreadsheet = self._request(
            "GET", self._sheet_url(), params={"fields": "sheets(properties(title))"}
        )
        titles = {
            item.get("properties", {}).get("title")
            for item in spreadsheet.get("sheets", [])
        }
        if "Jobs" not in titles:
            self._request(
                "POST", f"{self._sheet_url()}:batchUpdate",
                body={"requests": [{"addSheet": {"properties": {"title": "Jobs"}}}]},
            )
        current = self._request("GET", f"{self._sheet_url()}/values/Jobs!A1:L1")
        current_values = current.get("values", [])
        if not current_values:
            self._request(
                "PUT", f"{self._sheet_url()}/values/Jobs!A1:L1",
                params={"valueInputOption": "RAW"},
                body={"values": [list(JOB_COLUMNS)]},
            )
        elif current_values[0] != list(JOB_COLUMNS):
            raise RuntimeError("The Jobs tab has different columns. Use a blank tab named Jobs for the master mirror.")

    def _sheet_url(self):
        return f"{SHEETS_API}/{quote(self.spreadsheet_id, safe='')}"

    def _access_token(self):
        if self._credentials is None:
            try:
                from google.auth.transport.requests import Request
                from google.oauth2 import service_account
            except ImportError as exc:
                raise RuntimeError('Install Google Sheets support with "python -m pip install -e .[google]".') from exc
            self._credentials = service_account.Credentials.from_service_account_file(
                self.credentials_file,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._credentials.refresh(Request())
        elif not self._credentials.valid:
            from google.auth.transport.requests import Request
            self._credentials.refresh(Request())
        return self._credentials.token

    def _request(self, method: str, url: str, *, params=None, body=None):
        response = self.session.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            # requests' default exception text only includes the URL, hiding
            # Google's useful permission/configuration error from the dashboard.
            details = self._api_error_details(response)
            status = getattr(response, "status_code", None)
            if status == 403:
                message = (
                    "Google Sheets denied access (403). Share the spreadsheet with "
                    "the service account email as an Editor and confirm the Google "
                    "Sheets API is enabled for its Cloud project."
                )
                if details:
                    message += f" Google says: {details}"
                raise RuntimeError(message) from exc
            if details:
                raise RuntimeError(f"Google Sheets API error ({status}): {details}") from exc
            raise
        return response.json() if response.content else {}

    @staticmethod
    def _api_error_details(response):
        try:
            payload = response.json()
        except (ValueError, AttributeError):
            try:
                payload = json.loads(response.text)
            except (ValueError, AttributeError, TypeError):
                return ""
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            return ""
        parts = []
        if error.get("status"):
            parts.append(str(error["status"]))
        if error.get("message"):
            parts.append(str(error["message"]))
        for item in error.get("errors", []):
            if isinstance(item, dict) and item.get("reason"):
                parts.append(f"reason: {item['reason']}")
        return "; ".join(dict.fromkeys(parts))


class GitHubSheetsStore(GoogleSheetsMirror):
    """Persistent store for the scheduled GitHub Actions scan.

    This deliberately uses separate tabs from the local app's existing Jobs mirror.
    The in-memory interface matches the subset JobService uses; ``save`` batches
    changed rows back to Sheets after a scan or notification update.
    """

    JOB_TAB = "GitHub Jobs"
    STATE_TAB = "GitHub State"

    def __init__(self, spreadsheet_id: str, *, credentials_file: str | None = None, session=None):
        super().__init__(spreadsheet_id, credentials_file=credentials_file, session=session)
        if not self.spreadsheet_id:
            raise RuntimeError("Set GOOGLE_SPREADSHEET_ID for the GitHub Actions scan.")
        if not self.credentials_file or not Path(self.credentials_file).is_file():
            raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_FILE to a readable service-account JSON file.")
        self._ensure_github_tabs()
        self._jobs, self._job_rows = self._load_tab(self.JOB_TAB, GITHUB_JOB_COLUMNS, key="job_key")
        self._companies, self._company_rows = self._load_tab(self.STATE_TAB, GITHUB_STATE_COLUMNS, key="name", casefold=True)
        self._job_originals = self._original_rows(self._jobs, GITHUB_JOB_COLUMNS)
        self._company_originals = self._original_rows(self._companies, GITHUB_STATE_COLUMNS)

    def _ensure_github_tabs(self):
        spreadsheet = self._request("GET", self._sheet_url(), params={"fields": "sheets(properties(title))"})
        titles = {item.get("properties", {}).get("title") for item in spreadsheet.get("sheets", [])}
        missing = [name for name in (self.JOB_TAB, self.STATE_TAB) if name not in titles]
        if missing:
            self._request(
                "POST", f"{self._sheet_url()}:batchUpdate",
                body={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]},
            )
        for tab, columns in ((self.JOB_TAB, GITHUB_JOB_COLUMNS), (self.STATE_TAB, GITHUB_STATE_COLUMNS)):
            header = self._get_values(tab, "A1:Z1")
            if not header:
                self._update_values(tab, "A1", [list(columns)])
            elif header[0] != list(columns):
                raise RuntimeError(
                    f"The {tab!r} tab has unexpected columns. Rename it or use a blank tab with that name."
                )

    def _get_values(self, tab: str, cell_range: str):
        range_name = quote(f"'{tab}'!{cell_range}", safe="!:$")
        return self._request("GET", f"{self._sheet_url()}/values/{range_name}").get("values", [])

    def _update_values(self, tab: str, cell_range: str, values):
        range_name = quote(f"'{tab}'!{cell_range}", safe="!:$")
        self._request(
            "PUT", f"{self._sheet_url()}/values/{range_name}",
            params={"valueInputOption": "RAW"}, body={"values": values},
        )

    def _load_tab(self, tab: str, columns, *, key: str, casefold: bool = False):
        values = self._get_values(tab, f"A1:{_column_name(len(columns))}")
        records, row_numbers = {}, {}
        normalize = (lambda value: str(value).casefold()) if casefold else str
        for row_number, raw in enumerate(values[1:], start=2):
            padded = list(raw[:len(columns)]) + [""] * max(0, len(columns) - len(raw))
            record = dict(zip(columns, padded))
            raw_key = record.get(key, "")
            if not raw_key:
                continue
            normalized_key = normalize(raw_key)
            if normalized_key in records:
                raise RuntimeError(f"Duplicate {key} {raw_key!r} in {tab!r}; remove duplicate rows before scanning.")
            records[normalized_key] = _decode_record(record, columns)
            row_numbers[normalized_key] = row_number
        return records, row_numbers

    @staticmethod
    def _original_rows(records, columns):
        return {
            key: tuple(_sheet_cell(record.get(column, "")) for column in columns)
            for key, record in records.items()
        }

    def companies(self):
        from .models import Company

        return [
            Company(
                row["name"], row["url"], _json_list(row.get("keywords")),
                _json_list(row.get("locations")), _as_bool(row.get("enabled")),
            )
            for row in sorted(self._companies.values(), key=lambda item: item["name"].casefold())
        ]

    @property
    def job_count(self):
        return len(self._jobs)

    @property
    def company_count(self):
        return len(self._companies)

    def upsert_company(self, company):
        key = company.name.casefold()
        previous = self._companies.get(key, {})
        self._companies[key] = {
            **previous,
            "name": company.name,
            "url": company.url,
            "keywords": json.dumps(company.keywords, ensure_ascii=False),
            "locations": json.dumps(company.locations, ensure_ascii=False),
            "enabled": company.enabled,
            "baseline_done": _as_bool(previous.get("baseline_done")),
            "last_scanned_at": previous.get("last_scanned_at", ""),
            "last_successful_scan_at": previous.get("last_successful_scan_at", ""),
            "last_scan_status": previous.get("last_scan_status", "waiting"),
            "last_scan_error": previous.get("last_scan_error", ""),
            "last_scan_error_type": previous.get("last_scan_error_type", ""),
            "scan_failure_streak": _as_int(previous.get("scan_failure_streak")),
            "next_scan_retry_at": previous.get("next_scan_retry_at", ""),
            "last_job_count": _as_int(previous.get("last_job_count")),
        }

    def remove_company(self, name: str) -> bool:
        key = name.casefold()
        existed = key in self._companies
        self._companies.pop(key, None)
        self._jobs = {job_key: job for job_key, job in self._jobs.items() if job["company_name"].casefold() != key}
        return existed

    def baseline_done(self, name: str) -> bool:
        return _as_bool(self._companies.get(name.casefold(), {}).get("baseline_done"))

    def save_baseline(self, company, jobs, enrich_urls=()):
        new_urls = self._save_scanned_jobs(company, jobs, baseline=True, enrich_urls=set(enrich_urls))
        self._companies[company.name.casefold()]["baseline_done"] = True
        return new_urls

    def save_new_jobs(self, company, jobs, enrich_urls=()):
        return self._save_scanned_jobs(company, jobs, baseline=False, enrich_urls=set(enrich_urls))

    def _save_scanned_jobs(self, company, jobs, *, baseline: bool, enrich_urls: set[str]):
        now = _utc_now()
        new_urls = []
        for job in jobs:
            key = _stable_job_key(company.name, job.url)
            previous = self._jobs.get(key)
            notification_status = (
                previous.get("notification_status", "suppressed" if baseline else "pending")
                if previous else ("suppressed" if baseline or not company.matches(job) else "pending")
            )
            incoming_description = job.description or ""
            current_description = (previous or {}).get("description", "")
            desc_status = (previous or {}).get("description_status", "")
            if desc_status != "full" and incoming_description and len(incoming_description) >= len(current_description):
                description = incoming_description
                desc_status = "listing"
            else:
                description = current_description or incoming_description
            if key in enrich_urls and desc_status != "full" or not description:
                desc_status = "pending"
            elif not desc_status:
                desc_status = "listing" if description else "pending"
            if previous and previous.get("description_status") == "full":
                description, desc_status = current_description, "full"
            first_seen = (previous or {}).get("first_seen_at") or now
            self._jobs[key] = {
                **(previous or {}),
                "job_key": key,
                "company_name": company.name,
                "company_url": company.url,
                "title": job.title,
                "location": job.location or (previous or {}).get("location", ""),
                "url": job.url,
                "description": description,
                "description_status": desc_status,
                "notification_status": notification_status,
                "discovered_at": (previous or {}).get("discovered_at") or first_seen,
                "first_seen_at": first_seen,
                "last_seen_at": now,
                "is_active": True,
                "missing_successful_scans": 0,
                "description_attempts": _as_int((previous or {}).get("description_attempts")),
                "description_retry_at": (previous or {}).get("description_retry_at", ""),
                "description_error": (previous or {}).get("description_error", ""),
                "description_priority": 1 if job.url in enrich_urls else _as_int((previous or {}).get("description_priority")),
                "application_status": (previous or {}).get("application_status", "new") or "new",
                "application_notes": (previous or {}).get("application_notes", ""),
            }
            if not previous:
                new_urls.append(job.url)
        return new_urls

    def pending(self):
        enabled = {company.name.casefold() for company in self.companies() if company.enabled}
        return [
            {key: row.get(key, "") for key in ("company_name", "url", "title", "location", "description")}
            for row in self._jobs.values()
            if row.get("notification_status") == "pending" and row["company_name"].casefold() in enabled
        ]

    def mark_sent(self, company_name: str, url: str):
        row = self._find_job(company_name, url)
        if row:
            row["notification_status"] = "sent"

    def record_scan(self, name: str, status: str, job_count: int = 0, error: str | None = None,
                    seen_urls=(), error_type: str | None = None, retry_after_seconds: int | None = None):
        company = self._companies.get(name.casefold())
        if not company:
            return
        now = _utc_now()
        company["last_scanned_at"] = now
        if status == "ok":
            company.update({
                "last_successful_scan_at": now, "last_scan_status": "ok", "last_scan_error": "",
                "last_scan_error_type": "", "scan_failure_streak": 0, "next_scan_retry_at": "",
                "last_job_count": int(job_count),
            })
            seen = set(seen_urls)
            for row in self._jobs.values():
                if row["company_name"].casefold() != name.casefold():
                    continue
                if row["url"] in seen:
                    row["missing_successful_scans"] = 0
                    row["is_active"] = True
                elif row.get("is_active", True):
                    misses = _as_int(row.get("missing_successful_scans")) + 1
                    row["missing_successful_scans"] = misses
                    row["is_active"] = misses < 3
        else:
            streak = _as_int(company.get("scan_failure_streak")) + 1
            retry = ""
            if retry_after_seconds is not None:
                retry = (datetime.now(timezone.utc) + timedelta(seconds=max(0, retry_after_seconds))).isoformat(timespec="seconds")
            company.update({
                "last_scan_status": "error", "last_scan_error": str(error or ""),
                "last_scan_error_type": error_type or "unknown", "scan_failure_streak": streak,
                "next_scan_retry_at": retry,
            })

    def scan_failure_streak(self, name: str) -> int:
        return _as_int(self._companies.get(name.casefold(), {}).get("scan_failure_streak"))

    def company_scan_deferred(self, name: str) -> bool:
        retry_at = self._companies.get(name.casefold(), {}).get("next_scan_retry_at") or ""
        if not retry_at:
            return False
        try:
            retry = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if retry.tzinfo is None:
            retry = retry.replace(tzinfo=timezone.utc)
        return retry > datetime.now(timezone.utc)

    def all_job_rows(self):
        companies = {company.name.casefold(): company.url for company in self.companies()}
        return [
            {**row, "company_url": companies.get(row["company_name"].casefold(), row.get("company_url", ""))}
            for row in self._jobs.values()
        ]

    def queue_job_enrichment(self, company_name: str, url: str, priority: int = 0):
        row = self._find_job(company_name, url)
        if row and row.get("description_status") != "full":
            row["description_status"] = "pending"
            row["description_retry_at"] = row.get("description_retry_at") or _utc_now()
            row["description_priority"] = max(_as_int(row.get("description_priority")), priority)

    def next_job_for_enrichment(self):
        enabled = {company.name.casefold() for company in self.companies() if company.enabled}
        now = datetime.now(timezone.utc)
        candidates = []
        for row in self._jobs.values():
            if row["company_name"].casefold() not in enabled or row.get("description_status") != "pending":
                continue
            retry_at = row.get("description_retry_at") or ""
            if retry_at:
                try:
                    retry = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                    if retry.tzinfo is None:
                        retry = retry.replace(tzinfo=timezone.utc)
                    if retry > now:
                        continue
                except ValueError:
                    pass
            candidates.append(row)
        if not candidates:
            return None
        candidates.sort(key=lambda row: (
            -_as_int(row.get("description_priority")),
            row.get("notification_status") != "pending",
            row.get("discovered_at", ""),
        ))
        row = candidates[0]
        company = self._companies.get(row["company_name"].casefold(), {})
        return {**row, "company_url": company.get("url", row.get("company_url", ""))}

    def save_job_details(self, company_name: str, url: str, description: str, location: str = "", *, complete: bool = True):
        row = self._find_job(company_name, url)
        if not row:
            return
        if description:
            row["description"] = description
        if location:
            row["location"] = location
        if complete and description:
            row.update({
                "description_status": "full", "description_attempts": 0,
                "description_retry_at": "", "description_error": "", "description_priority": 0,
            })

    def fail_job_details(self, company_name: str, url: str, error: str):
        row = self._find_job(company_name, url)
        if not row:
            return
        attempts = _as_int(row.get("description_attempts")) + 1
        retry_minutes = (1, 5, 30, 360, 1440)[min(attempts - 1, 4)]
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)).isoformat()
        row.update({"description_attempts": attempts, "description_retry_at": retry_at, "description_error": str(error)[:1000]})

    def pending_job_enrichment_count(self):
        enabled = {company.name.casefold() for company in self.companies() if company.enabled}
        return sum(
            row.get("description_status") == "pending" and row["company_name"].casefold() in enabled
            for row in self._jobs.values()
        )

    def update_application_tracking(self, company_name: str, url: str, status: str, notes: str) -> bool:
        allowed = {"new", "interested", "applied", "interviewing", "offer", "rejected", "withdrawn"}
        if status not in allowed:
            raise ValueError("Unsupported application status")
        row = self._find_job(company_name, url)
        if not row:
            return False
        row["application_status"] = status
        row["application_notes"] = notes.strip()
        return True

    def company_health(self):
        return [
            {**row, "enabled": _as_bool(row.get("enabled")), "baseline_done": _as_bool(row.get("baseline_done"))}
            for row in self._companies.values()
        ]

    def _find_job(self, company_name: str, url: str):
        key = _stable_job_key(company_name, url)
        row = self._jobs.get(key)
        if row:
            return row
        return next((item for item in self._jobs.values()
                     if item["company_name"].casefold() == company_name.casefold() and item["url"] == url), None)

    def save(self):
        jobs = self._save_tab(self.JOB_TAB, GITHUB_JOB_COLUMNS, self._jobs, self._job_rows, self._job_originals)
        companies = self._save_tab(self.STATE_TAB, GITHUB_STATE_COLUMNS, self._companies, self._company_rows, self._company_originals)
        return {
            "jobs_added": jobs["added"], "jobs_updated": jobs["updated"],
            "companies_added": companies["added"], "companies_updated": companies["updated"],
        }

    def _save_tab(self, tab, columns, records, row_numbers, originals):
        updates, additions = [], []
        update_keys = []
        addition_keys = []
        for map_key, record in records.items():
            row = [_sheet_cell(record.get(column, "")) for column in columns]
            row_number = row_numbers.get(map_key)
            if row_number:
                if tuple(row) != originals.get(map_key):
                    updates.append({"range": f"'{tab}'!A{row_number}:{_column_name(len(columns))}{row_number}", "values": [row]})
                    update_keys.append((map_key, tuple(row)))
            else:
                additions.append(row)
                addition_keys.append((map_key, tuple(row)))
        if updates:
            self._request(
                "POST", f"{self._sheet_url()}/values:batchUpdate",
                params={"valueInputOption": "RAW"}, body={"data": updates, "valueInputOption": "RAW"},
            )
            originals.update(update_keys)
        if additions:
            range_name = quote(f"'{tab}'!A:{_column_name(len(columns))}", safe="!:$")
            try:
                response = self._request(
                    "POST", f"{self._sheet_url()}/values/{range_name}:append",
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                    body={"values": additions},
                )
            except Exception:
                # An HTTP timeout can happen after Google has accepted an append.
                # Refresh keys before any caller retries this same in-memory save.
                try:
                    self._refresh_tab_index(tab, columns, row_numbers, originals)
                except Exception:
                    pass
                raise
            originals.update(addition_keys)
            updated_range = (response.get("updates") or {}).get("updatedRange", "")
            first_row = _row_number_from_range(updated_range)
            if first_row:
                for offset, (map_key, _) in enumerate(addition_keys):
                    row_numbers[map_key] = first_row + offset
            else:
                self._refresh_tab_index(tab, columns, row_numbers, originals)
        return {"added": len(addition_keys), "updated": len(update_keys)}

    def _refresh_tab_index(self, tab, columns, row_numbers, originals):
        key_column = "job_key" if tab == self.JOB_TAB else "name"
        records, refreshed_rows = self._load_tab(
            tab, columns, key=key_column, casefold=(key_column == "name")
        )
        row_numbers.clear()
        row_numbers.update(refreshed_rows)
        originals.clear()
        originals.update(self._original_rows(records, columns))


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _row_number_from_range(value: str) -> int:
    import re
    match = re.search(r"![A-Z]+(\d+):", value)
    return int(match.group(1)) if match else 0


def _sheet_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _decode_record(record, columns):
    result = dict(record)
    for name in ("is_active", "enabled", "baseline_done"):
        if name in result:
            result[name] = _as_bool(result[name])
    for name in ("missing_successful_scans", "description_attempts", "description_priority", "scan_failure_streak", "last_job_count"):
        if name in result:
            result[name] = _as_int(result[name])
    return result


def _as_bool(value):
    return value is True or str(value).strip().casefold() in {"true", "1", "yes", "active"}


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []

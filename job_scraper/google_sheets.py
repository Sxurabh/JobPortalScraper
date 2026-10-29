"""Google Sheets mirror client using a service-account credential file."""

import os
import json
from pathlib import Path
from urllib.parse import quote

import requests


SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
JOB_COLUMNS = (
    "job_key", "company_name", "title", "location", "url", "description",
    "first_seen_at", "last_seen_at", "is_active", "notification_status",
    "application_status", "application_notes",
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

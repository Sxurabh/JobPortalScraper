"""MCP tools for controlling a locally running Career Radar dashboard."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class DashboardAPIError(RuntimeError):
    """A request to the local dashboard failed."""


class DashboardClient:
    """Small JSON client for the dashboard's loopback-only API."""

    def __init__(self, base_url=None, timeout=10, opener=None):
        if base_url is None:
            port = os.getenv("JOB_SCRAPER_PORT", "8765")
            base_url = f"http://127.0.0.1:{port}"
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Career Radar MCP only connects to an HTTP dashboard on this computer.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urlopen

    def request(self, method, path, *, params=None, payload=None):
        if params:
            path = f"{path}?{urlencode(params)}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
            message = body.get("error") if isinstance(body, dict) else None
            raise DashboardAPIError(message or f"Dashboard returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DashboardAPIError(
                "Career Radar dashboard is unavailable at "
                f"{self.base_url}. Start it with `python start_dashboard.py`, then retry."
            ) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardAPIError("Dashboard returned an invalid JSON response.") from exc

    def get(self, path, *, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload or {})

    def patch(self, path, payload):
        return self.request("PATCH", path, payload=payload)


def create_server(client=None):
    """Build the MCP server; kept separate so importing this module is safe."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError("MCP support is missing. Reinstall Career Radar with its declared dependencies.") from exc

    api = client or DashboardClient()
    server = FastMCP("Career Radar")

    @server.tool()
    def get_status() -> dict:
        """Read monitoring status, scan health, job totals, and integration health."""
        return api.get("/api/status")

    @server.tool()
    def list_jobs(scope: str = "matching", limit: int = 1000) -> list:
        """List saved jobs. Scope is 'matching' or 'all'; limit is 1 to 10000."""
        return api.get("/api/jobs", params={"scope": scope, "limit": limit})

    @server.tool()
    def list_companies() -> list:
        """List tracked companies and their recent scan health."""
        return api.get("/api/companies")

    @server.tool()
    def get_settings() -> dict:
        """Read dashboard settings and integration status; saved tokens are omitted."""
        return api.get("/api/settings")

    @server.tool()
    def save_company(name: str, url: str, keywords: list[str] | None = None,
                     locations: list[str] | None = None, enabled: bool = True) -> dict:
        """Add a tracked company or update its URL, filters, and enabled state."""
        return api.post("/api/companies", {
            "name": name, "url": url, "keywords": keywords or [],
            "locations": locations or [], "enabled": enabled,
        })

    @server.tool()
    def remove_company(name: str) -> dict:
        """Remove a tracked company and its stored jobs."""
        return api.post("/api/companies/remove", {"name": name})

    @server.tool()
    def start_monitoring() -> dict:
        """Start scheduled monitoring and request an immediate scan."""
        return api.post("/api/control/start")

    @server.tool()
    def pause_monitoring() -> dict:
        """Pause scheduled monitoring."""
        return api.post("/api/control/pause")

    @server.tool()
    def scan_now() -> dict:
        """Queue a scan now; reports a conflict if a scan is already running."""
        return api.post("/api/control/scan")

    @server.tool()
    def update_job_tracking(company_name: str, url: str, application_status: str = "new",
                            application_notes: str = "") -> dict:
        """Update a job's application status and personal notes."""
        return api.patch("/api/jobs/tracking", {
            "company_name": company_name, "url": url,
            "application_status": application_status,
            "application_notes": application_notes,
        })

    @server.tool()
    def update_settings(interval_minutes: int | None = None,
                        google_spreadsheet_id: str | None = None,
                        bot_token: str | None = None, chat_id: str | None = None) -> dict:
        """Update dashboard settings. Omit fields to retain their current values."""
        payload = {
            key: value for key, value in {
                "interval_minutes": interval_minutes,
                "google_spreadsheet_id": google_spreadsheet_id,
                "bot_token": bot_token,
                "chat_id": chat_id,
            }.items() if value is not None
        }
        return api.post("/api/settings", payload)

    @server.tool()
    def test_telegram(bot_token: str = "", chat_id: str = "") -> dict:
        """Send a Telegram connection test using supplied or saved credentials."""
        return api.post("/api/settings/test-telegram", {
            "bot_token": bot_token, "chat_id": chat_id,
        })

    @server.tool()
    def quit_dashboard() -> dict:
        """Stop the running dashboard and its background workers."""
        return api.post("/api/control/quit")

    return server


def main():
    """Run the MCP server over stdio for a local MCP host."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()

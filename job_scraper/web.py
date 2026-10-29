"""Local web dashboard for the job scraper."""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from .models import Company
from .env import load_project_env
from .notifier import TelegramNotifier
from .service import JobService
from .storage import Storage

LOG = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _data_path(value, default_name):
    """Resolve default app data from the project, independent of launch cwd."""
    if value is None:
        return PROJECT_ROOT / default_name
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class MonitorController:
    def __init__(self, service: JobService, interval_seconds: int = 1800):
        self.service = service
        self.interval_seconds = interval_seconds
        self.condition = threading.Condition()
        self.monitoring = False
        self.scanning = False
        self.scan_requested = False
        self.scan_progress = 0
        self.scan_company = ""
        self.shutdown = False
        self.last_scan_started = None
        self.last_scan_finished = None
        self.next_scan_at = None
        self.next_due_monotonic = None
        self.thread = threading.Thread(target=self._loop, name="job-scraper-monitor", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            with self.condition:
                while not self.shutdown:
                    if self.scan_requested:
                        self.scan_requested = False
                        break
                    if self.monitoring:
                        if self.next_due_monotonic is None:
                            self.next_due_monotonic = time.monotonic()
                        remaining = self.next_due_monotonic - time.monotonic()
                        if remaining <= 0:
                            break
                        self.condition.wait(remaining)
                        continue
                    self.condition.wait()
                if self.shutdown:
                    return
                self.scanning = True
                self.scan_progress = 0
                self.scan_company = ""
                self.last_scan_started = _utc_now()
                self.next_scan_at = None
            try:
                self.service.scan_once(on_progress=self._update_scan_progress)
            except Exception:
                LOG.exception("Unexpected scan failure")
            finally:
                with self.condition:
                    self.scanning = False
                    self.scan_progress = 0
                    self.scan_company = ""
                    self.last_scan_finished = _utc_now()
                    self.next_due_monotonic = time.monotonic() + self.interval_seconds if self.monitoring else None
                    self.next_scan_at = _utc_after(self.interval_seconds) if self.monitoring else None
                    self.condition.notify_all()

    def start(self):
        with self.condition:
            self.monitoring = True
            if not self.scanning:
                self.scan_requested = True
            self.next_due_monotonic = time.monotonic() if not self.scanning else self.next_due_monotonic
            self.condition.notify_all()

    def _update_scan_progress(self, completed, total, company_name):
        with self.condition:
            self.scan_progress = round(completed * 100 / total) if total else 100
            self.scan_company = company_name

    def pause(self):
        with self.condition:
            self.monitoring = False
            self.next_scan_at = None
            self.next_due_monotonic = None
            self.condition.notify_all()

    def scan_now(self):
        with self.condition:
            if self.scanning or self.scan_requested:
                return False
            self.scan_requested = True
            self.condition.notify_all()
            return True

    def set_interval(self, seconds: int):
        with self.condition:
            self.interval_seconds = seconds
            if self.monitoring:
                self.next_due_monotonic = time.monotonic() + seconds
            self.next_scan_at = _utc_after(seconds) if self.monitoring else None
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.shutdown = True
            self.monitoring = False
            self.condition.notify_all()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.service.close_background_workers()

    def snapshot(self):
        with self.condition:
            return {
                "monitoring": self.monitoring,
                "scanning": self.scanning,
                "scan_requested": self.scan_requested,
                "scan_progress": self.scan_progress,
                "scan_company": self.scan_company,
                "interval_seconds": self.interval_seconds,
                "last_scan_started": self.last_scan_started,
                "last_scan_finished": self.last_scan_finished,
                "next_scan_at": self.next_scan_at,
            }


def _utc_after(seconds: int):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


class ConfiguredNotifier:
    def __init__(self, settings):
        self.settings = settings

    def send(self, company, title, location, url):
        values = self.settings()
        token = values.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = values.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")
        TelegramNotifier(token=token, chat_id=chat_id).send(company, title, location, url)


def create_app(config_path=None, db_path=None, settings_path=None, *, scraper=None, start_worker=True):
    load_project_env()
    app = Flask(__name__)
    config_path = _data_path(config_path, "companies.json")
    db_path = _data_path(db_path, "jobs.sqlite3")
    settings_path = _data_path(settings_path, "settings.json")
    storage = Storage(db_path)
    if config_path.exists():
        raw_companies = json.loads(config_path.read_text(encoding="utf-8"))
        for entry in raw_companies:
            storage.upsert_company(Company.from_dict(entry))

    settings_lock = threading.RLock()
    try:
        settings_data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        settings_data = {}
    interval = _validated_interval(settings_data.get("interval_minutes", 30))
    storage.set_sheet_target(settings_data.get("google_spreadsheet_id", ""))

    def get_settings():
        with settings_lock:
            return dict(settings_data)

    def save_settings(values):
        nonlocal settings_data
        with settings_lock:
            settings_data = values
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                LOG.warning("Could not restrict permissions on local settings file")
            temp_path.replace(settings_path)

    service = JobService(storage, scraper=scraper, notifier=ConfiguredNotifier(get_settings))
    if start_worker:
        service.start_background_workers(settings_provider=get_settings)
    controller = MonitorController(service, interval_seconds=interval * 60) if start_worker else None
    if controller is None:
        controller = MonitorController.__new__(MonitorController)
        controller.service = service
        controller.interval_seconds = interval * 60
        controller.condition = threading.Condition()
        controller.monitoring = controller.scanning = controller.scan_requested = controller.shutdown = False
        controller.scan_progress = 0
        controller.scan_company = ""
        controller.last_scan_started = controller.last_scan_finished = controller.next_scan_at = None
        controller.next_due_monotonic = None
        controller.thread = None

    app.config.update(STORAGE=storage, CONTROLLER=controller, CONFIG_PATH=config_path, SETTINGS_PATH=settings_path)

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/status")
    def status():
        state = controller.snapshot()
        companies = storage.company_health()
        latest_scan = max((item["last_scanned_at"] for item in companies if item["last_scanned_at"]), default=None)
        if not state["last_scan_finished"]:
            state["last_scan_finished"] = latest_scan
        settings = get_settings()
        state.update(storage.dashboard_counts())
        state["companies_detail"] = companies
        spreadsheet_id = settings.get("google_spreadsheet_id", "")
        credential_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        state["google_sheets"] = storage.sheet_sync_health(
            configured=bool(spreadsheet_id),
            credentials_configured=bool(credential_file and Path(credential_file).is_file()),
        )
        state["job_descriptions_pending"] = storage.pending_job_enrichment_count()
        state["telegram_configured"] = bool((settings.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")) and (settings.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")))
        state["interval_minutes"] = state["interval_seconds"] // 60
        return jsonify(state)

    @app.get("/api/jobs")
    def jobs():
        scope = request.args.get("scope", "matching")
        if scope not in {"matching", "all"}:
            return jsonify(error="Scope must be 'matching' or 'all'."), 400
        try:
            limit = min(max(int(request.args.get("limit", "1000")), 1), 10000)
        except ValueError:
            return jsonify(error="Limit must be a whole number."), 400
        return jsonify(storage.recent_jobs(limit=limit, scope=scope))

    @app.patch("/api/jobs/tracking")
    def update_job_tracking():
        payload = request.get_json(silent=True) or {}
        company_name = str(payload.get("company_name", "")).strip()
        url = str(payload.get("url", "")).strip()
        status = str(payload.get("application_status", "new")).strip().casefold()
        notes = str(payload.get("application_notes", ""))[:5000]
        if not company_name or not url.startswith(("https://", "http://")):
            return jsonify(error="A company and valid job URL are required."), 400
        try:
            updated = storage.update_application_tracking(company_name, url, status, notes)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not updated:
            return jsonify(error="Job was not found."), 404
        return jsonify(ok=True)

    @app.get("/api/jobs/details")
    def job_details():
        company_name = request.args.get("company", "").strip()
        url = request.args.get("url", "").strip()
        job = storage.get_job(company_name, url)
        if not job:
            return jsonify(error="Job was not found."), 404
        details = {"description": job["description"], "location": job["location"]}
        company = next((item for item in storage.companies() if item.name.casefold() == company_name.casefold()), None)
        fetch_details = getattr(service.scraper, "fetch_job_details", None)
        if company and fetch_details:
            try:
                full_details = fetch_details(company.url, url)
                if full_details:
                    details.update({key: value for key, value in full_details.items() if value})
                    storage.save_job_details(
                        company_name, url, full_details.get("description", ""), details["location"],
                        complete=bool(full_details.get("description")),
                    )
            except Exception as exc:
                LOG.info("Could not load full job details for %s (%s)", company_name, type(exc).__name__)
        return jsonify(details)

    @app.get("/api/companies")
    def companies():
        return jsonify(storage.company_health())

    @app.post("/api/companies")
    def save_company():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        url = str(payload.get("url", "")).strip()
        if not name or not url.startswith(("https://", "http://")):
            return jsonify(error="Enter a company name and a valid http(s) careers URL."), 400
        company = Company(
            name,
            url,
            _string_list(payload.get("keywords")),
            _string_list(payload.get("locations")),
            bool(payload.get("enabled", True)),
        )
        storage.upsert_company(company)
        _write_company_config(config_path, storage.companies())
        return jsonify(ok=True, company=next(item for item in storage.company_health() if item["name"].casefold() == name.casefold()))

    @app.post("/api/companies/remove")
    def delete_company():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        if not name:
            return jsonify(error="Company name is required."), 400
        removed = storage.remove_company(name)
        if removed:
            _write_company_config(config_path, storage.companies())
        return (jsonify(ok=True), 200) if removed else (jsonify(error="Company not found."), 404)

    @app.post("/api/control/<action>")
    def control(action):
        if action == "start":
            controller.start()
            return jsonify(ok=True)
        if action == "pause":
            controller.pause()
            return jsonify(ok=True)
        if action == "scan":
            if not controller.scan_now():
                return jsonify(error="A scan is already running."), 409
            return jsonify(ok=True)
        if action == "quit":
            server = app.config.get("HTTP_SERVER")
            if server:
                def stop_server():
                    controller.close()
                    server.shutdown()
                threading.Thread(target=stop_server, daemon=True).start()
            else:
                controller.close()
            return jsonify(ok=True)
        return jsonify(error="Unknown monitoring action."), 404

    @app.get("/api/settings")
    def read_settings():
        values = get_settings()
        return jsonify(
            bot_token_configured=bool(values.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")),
            chat_id=values.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", ""),
            interval_minutes=controller.snapshot()["interval_seconds"] // 60,
            google_spreadsheet_id=values.get("google_spreadsheet_id", ""),
            google_credentials_configured=bool(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
                and Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")).is_file()
            ),
        )

    @app.post("/api/settings")
    def update_settings():
        payload = request.get_json(silent=True) or {}
        try:
            minutes = _validated_interval(payload.get("interval_minutes", 30))
        except (TypeError, ValueError, OverflowError):
            return jsonify(error="Polling interval must be between 1 and 1440 minutes."), 400
        previous = get_settings()
        token = str(payload.get("bot_token", "")).strip() or previous.get("bot_token", "")
        token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = str(payload.get("chat_id", "")).strip()
        if not chat_id:
            chat_id = previous.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
        if bool(token) != bool(chat_id):
            return jsonify(error="Telegram needs both a bot token and a channel or chat ID. Enter both, then save settings."), 400
        try:
            spreadsheet_id = _validated_spreadsheet_id(
                payload.get("google_spreadsheet_id", previous.get("google_spreadsheet_id", ""))
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        values = {
            "bot_token": token,
            "chat_id": chat_id,
            "interval_minutes": minutes,
            "google_spreadsheet_id": spreadsheet_id,
        }
        storage.set_sheet_target(spreadsheet_id)
        save_settings(values)
        controller.set_interval(minutes * 60)
        return jsonify(ok=True, bot_token_configured=bool(token or os.getenv("TELEGRAM_BOT_TOKEN")), chat_id=chat_id, interval_minutes=minutes, google_spreadsheet_id=spreadsheet_id)

    @app.post("/api/settings/test-telegram")
    def test_telegram():
        values = get_settings()
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("bot_token", "")).strip() or values.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = str(payload.get("chat_id", "")).strip() or values.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return jsonify(error="Add your Telegram bot token and channel or chat ID first."), 400
        try:
            TelegramNotifier(token=token, chat_id=chat_id).send("Job Scraper", "Connection test", "", "https://telegram.org")
        except Exception as exc:
            LOG.exception("Telegram test message failed")
            return jsonify(error=str(exc)), 502
        return jsonify(ok=True, message="Test message sent. Check your Telegram channel.")

    return app


def _validated_interval(value):
    minutes = int(value)
    if not 1 <= minutes <= 1440:
        raise ValueError("Interval out of range")
    return minutes


def _validated_spreadsheet_id(value):
    value = str(value or "").strip()
    if not value:
        return ""
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    spreadsheet_id = match.group(1) if match else value
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", spreadsheet_id):
        raise ValueError("Enter a Google spreadsheet ID or URL.")
    return spreadsheet_id


def _string_list(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _write_company_config(path: Path, companies: list[Company]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps([company.to_dict() for company in companies], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(path)


def main():
    log_path = Path("logs") / "job-scraper.log"
    log_path.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )
    app = create_app()
    port = int(os.getenv("JOB_SCRAPER_PORT", "8765"))
    LOG.info("Starting local dashboard at http://127.0.0.1:%s", port)
    server = make_server("127.0.0.1", port, app, threaded=True)
    app.config["HTTP_SERVER"] = server
    try:
        server.serve_forever()
    finally:
        app.config["CONTROLLER"].close()
        app.config["STORAGE"].close()


if __name__ == "__main__":
    main()

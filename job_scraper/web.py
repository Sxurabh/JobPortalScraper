"""Local web dashboard for the job scraper."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from .models import Company
from .notifier import TelegramNotifier
from .service import JobService
from .storage import Storage

LOG = logging.getLogger(__name__)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class MonitorController:
    def __init__(self, service: JobService, interval_seconds: int = 1800):
        self.service = service
        self.interval_seconds = interval_seconds
        self.condition = threading.Condition()
        self.monitoring = False
        self.scanning = False
        self.scan_requested = False
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
                self.last_scan_started = _utc_now()
                self.next_scan_at = None
            try:
                self.service.scan_once()
            except Exception:
                LOG.exception("Unexpected scan failure")
            finally:
                with self.condition:
                    self.scanning = False
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

    def snapshot(self):
        with self.condition:
            return {
                "monitoring": self.monitoring,
                "scanning": self.scanning,
                "scan_requested": self.scan_requested,
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


def create_app(config_path="companies.json", db_path="jobs.sqlite3", settings_path="settings.json", *, scraper=None, start_worker=True):
    app = Flask(__name__)
    config_path = Path(config_path).resolve()
    settings_path = Path(settings_path).resolve()
    storage = Storage(Path(db_path).resolve())
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
            temp_path.replace(settings_path)

    service = JobService(storage, scraper=scraper, notifier=ConfiguredNotifier(get_settings))
    controller = MonitorController(service, interval_seconds=interval * 60) if start_worker else None
    if controller is None:
        controller = MonitorController.__new__(MonitorController)
        controller.service = service
        controller.interval_seconds = interval * 60
        controller.condition = threading.Condition()
        controller.monitoring = controller.scanning = controller.scan_requested = controller.shutdown = False
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
        state["telegram_configured"] = bool((settings.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")) and (settings.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")))
        state["interval_minutes"] = state["interval_seconds"] // 60
        return jsonify(state)

    @app.get("/api/jobs")
    def jobs():
        scope = request.args.get("scope", "matching")
        if scope not in {"matching", "all"}:
            return jsonify(error="Scope must be 'matching' or 'all'."), 400
        return jsonify(storage.recent_jobs(scope=scope))

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
        chat_id = str(payload.get("chat_id", "")).strip()
        if not chat_id:
            chat_id = previous.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
        values = {"bot_token": token, "chat_id": chat_id, "interval_minutes": minutes}
        save_settings(values)
        controller.set_interval(minutes * 60)
        return jsonify(ok=True, bot_token_configured=bool(token or os.getenv("TELEGRAM_BOT_TOKEN")), chat_id=chat_id, interval_minutes=minutes)

    @app.post("/api/settings/test-telegram")
    def test_telegram():
        values = get_settings()
        token = values.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = values.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")
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

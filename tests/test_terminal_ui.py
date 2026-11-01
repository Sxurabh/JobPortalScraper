import threading

from job_scraper.terminal_ui import handle_terminal_key, render_dashboard


def test_terminal_dashboard_renders_live_counts_and_recent_jobs_without_color():
    frame = render_dashboard(
        {
            "monitoring": True,
            "scanning": True,
            "scan_progress": 40,
            "scan_company": "Acme",
            "interval_minutes": 15,
            "companies": 3,
            "jobs": 8,
            "all_jobs": 21,
            "pending": 2,
            "companies_detail": [{"name": "Acme", "enabled": True}],
        },
        [{"title": "Senior Engineer", "company_name": "Acme", "location": "Remote"}],
        "http://127.0.0.1:8765",
        width=88,
        color=False,
    )

    assert "CAREER RADAR" in frame
    assert "╭─────╮" in frame
    assert "YOUR PERSONAL JOB DISCOVERY RADAR" in frame
    assert "SCANNING" in frame
    assert "40%" in frame
    assert "MATCHING JOBS  8" in frame
    assert "Senior Engineer" in frame
    assert "S start  P pause  R scan  O browser  Q quit" in frame
    assert "\x1b[" not in frame


def test_terminal_dashboard_reports_portal_issues_and_clips_to_width():
    frame = render_dashboard(
        {
            "monitoring": False,
            "companies": 1,
            "companies_detail": [{
                "name": "Example Company",
                "enabled": True,
                "last_scan_status": "error",
                "last_scan_error": "Portal response structure changed",
            }],
        },
        [],
        "http://127.0.0.1:8765",
        width=70,
        color=False,
    )

    assert "PAUSED" in frame
    assert "Latest issue" in frame
    assert all(len(line) <= 70 for line in frame.splitlines())


def test_terminal_hotkeys_dispatch_actions_and_quit_dashboard():
    actions = []
    stop_event = threading.Event()

    def perform(action):
        actions.append(action)
        return f"{action} accepted"

    assert handle_terminal_key("s", perform, owner=True, stop_event=stop_event) == "start accepted"
    assert handle_terminal_key("r", perform, owner=True, stop_event=stop_event) == "scan accepted"
    assert handle_terminal_key("o", perform, owner=True, stop_event=stop_event) == "open accepted"
    assert handle_terminal_key("q", perform, owner=True, stop_event=stop_event) == "quit accepted"
    assert actions == ["start", "scan", "open", "quit"]
    assert stop_event.is_set()


def test_terminal_hotkey_quit_only_closes_an_attached_view():
    actions = []
    stop_event = threading.Event()
    notice = handle_terminal_key("q", actions.append, owner=False, stop_event=stop_event)

    assert notice == "Closed terminal view; the dashboard is still running."
    assert actions == []
    assert stop_event.is_set()


def test_failed_terminal_quit_keeps_dashboard_view_open():
    stop_event = threading.Event()

    def fail_to_quit(action):
        raise RuntimeError("connection refused")

    notice = handle_terminal_key("q", fail_to_quit, owner=True, stop_event=stop_event)
    assert notice == "Could not stop dashboard: connection refused"
    assert not stop_event.is_set()

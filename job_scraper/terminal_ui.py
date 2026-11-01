"""A small live terminal companion for the browser dashboard."""

import json
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from urllib.error import URLError
from urllib.request import Request, urlopen


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RESET = "\x1b[0m"


def _paint(value, color, enabled):
    return f"\x1b[{color}m{value}{_RESET}" if enabled else value


def _clip(value, width):
    value = str(value).replace("\n", " ")
    if len(value) > width:
        return value[:max(0, width - 1)] + "…"
    return value


def _box_line(content, width):
    clean = _ANSI.sub("", content)
    available = width - 4
    if len(clean) > available:
        content = _clip(clean, available)
        clean = content
    return "║ " + content + " " * max(0, available - len(clean)) + " ║"


def render_dashboard(snapshot, jobs, dashboard_url, *, width=88, color=True,
                     owner=True, notice=""):
    """Render one compact terminal dashboard frame."""
    width = max(48, min(int(width), 112))
    state = snapshot
    companies = state.get("companies_detail", [])
    errors = [item for item in companies if item.get("last_scan_status") == "error"]
    active = sum(1 for item in companies if item.get("enabled"))
    monitoring = state.get("monitoring", False)
    scanning = state.get("scanning", False)
    state_label = "SCANNING" if scanning else ("MONITORING" if monitoring else "PAUSED")
    state_color = "33" if scanning else ("32" if monitoring else "33")
    indicator = _paint("●", state_color, color)

    top = "╔" + "═" * (width - 2) + "╗"
    divider = "╠" + "═" * (width - 2) + "╣"
    bottom = "╚" + "═" * (width - 2) + "╝"
    rows = [top]
    rows.append(_box_line(_paint("╭─────╮", "1;32", color) + "  " + _paint("CAREER RADAR", "1;36", color)
                          + "  │  LIVE JOB MONITOR" + " " + indicator + " " + _paint(state_label, state_color, color), width))
    rows.append(_box_line(_paint("│  /\\ │", "1;32", color) + "  " + _paint("YOUR PERSONAL JOB DISCOVERY RADAR", "2", color), width))
    rows.append(_box_line(_paint("│ /_\\ │", "1;32", color) + "  " + _paint("DASHBOARD", "2", color) + "  " + dashboard_url, width))
    rows.append(_box_line(_paint("╰─────╯", "1;32", color) + "  " + _paint("LOCAL-FIRST CAREER SEARCH", "2", color), width))
    rows.append(divider)
    stats = (
        f"COMPANIES  {active}/{state.get('companies', len(companies))}     "
        f"MATCHING JOBS  {state.get('jobs', 0)}     "
        f"ALL OPENINGS  {state.get('all_jobs', 0)}     "
        f"PENDING ALERTS  {state.get('pending', 0)}"
    )
    rows.append(_box_line(_paint(stats, "1", color), width))
    interval = state.get("interval_minutes", state.get("interval_seconds", 1800) // 60)
    next_scan = state.get("next_scan_at")
    next_text = f" · next {str(next_scan)[11:19]} UTC" if next_scan else ""
    rows.append(_box_line(f"Scan interval  {interval} min{next_text}    ·    Portal issues  {len(errors)}", width))

    if scanning or state.get("scan_requested"):
        percent = max(0, min(100, int(state.get("scan_progress", 0))))
        bar_width = min(30, width - 38)
        filled = round(bar_width * percent / 100)
        bar = _paint("━" * filled, "36", color) + "─" * (bar_width - filled)
        company = state.get("scan_company") or ("starting scan" if state.get("scan_requested") else "scanning")
        rows.append(_box_line(f"Progress  [{bar}] {percent:3d}%   {_clip(company, 24)}", width))
    else:
        last_scan = state.get("last_scan_finished")
        last_text = f"Last scan  {str(last_scan).replace('T', ' ')[:19]} UTC" if last_scan else "No scan completed yet"
        rows.append(_box_line(last_text, width))

    rows.append(divider)
    rows.append(_box_line(_paint("RECENT MATCHING JOBS", "1;36", color), width))
    if jobs:
        for job in jobs[:3]:
            title = _clip(job.get("title", "Untitled role"), max(18, width - 38))
            company = _clip(job.get("company_name", ""), 18)
            place = _clip(job.get("location", ""), 19)
            rows.append(_box_line(f"{title}  ·  {company}  ·  {place}", width))
    else:
        rows.append(_box_line("No matching jobs saved yet. Add companies in the browser dashboard.", width))

    if errors:
        first = errors[0]
        issue = f"Latest issue  {first.get('name', 'Company')}: {first.get('last_scan_error') or 'scan failed'}"
        rows.append(_box_line(_paint(_clip(issue, width - 5), "31", color), width))
    rows.append(divider)
    if notice:
        rows.append(_box_line(_paint(notice, "1;33", color), width))
    if width < 78:
        controls = "S start  P pause  R scan  O open  Q quit" if owner else "S start  P pause  R scan  O open  Q close"
    else:
        controls = "S start  P pause  R scan  O browser  Q quit" if owner else "S start  P pause  R scan  O browser  Q close view"
    rows.append(_box_line(controls + "     ·     Ctrl+C also works", width))
    rows.append(bottom)
    return "\n".join(rows)


def fetch_dashboard_snapshot(dashboard_url):
    """Fetch status and a few recent jobs when attaching to an existing process."""
    try:
        with urlopen(f"{dashboard_url}/api/status", timeout=3) as response:
            status = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{dashboard_url}/api/jobs?scope=matching&limit=5", timeout=3) as response:
            jobs = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read dashboard status. Check that it is still running.") from exc
    return status, jobs


def dashboard_action_handler(dashboard_url):
    """Create local API actions for the interactive terminal controls."""
    def perform(action):
        if action == "open":
            webbrowser.open(dashboard_url)
            return "Opened the browser dashboard."
        if action not in {"start", "pause", "scan", "quit"}:
            raise ValueError(f"Unsupported terminal action: {action}")
        request = Request(
            f"{dashboard_url}/api/control/{action}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=4) as response:
                result = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                raise RuntimeError("Could not reach the local dashboard API.") from exc
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "The dashboard could not complete that action."))
        return {
            "start": "Monitoring started.",
            "pause": "Monitoring paused.",
            "scan": "Scan queued.",
            "quit": "Dashboard is shutting down.",
        }[action]
    return perform


def handle_terminal_key(key, action_handler, *, owner, stop_event):
    """Map a single key to an action. Returns a short notice for the UI."""
    key = (key or "").casefold()
    actions = {"s": "start", "p": "pause", "r": "scan", "o": "open"}
    if key == "q":
        if owner:
            try:
                notice = action_handler("quit")
            except Exception as exc:
                return f"Could not stop dashboard: {exc}"
        else:
            notice = "Closed terminal view; the dashboard is still running."
        stop_event.set()
        return notice
    action = actions.get(key)
    if not action:
        return "Unknown key. Use S, P, R, O, or Q."
    try:
        return action_handler(action)
    except Exception as exc:
        return f"Action failed: {exc}"


def _terminal_reader():
    """Return a non-blocking key reader and a terminal-mode restoration hook."""
    if os.name == "nt":
        import msvcrt

        def read_key():
            return msvcrt.getwch() if msvcrt.kbhit() else None

        return read_key, lambda: None

    import select
    import termios
    import tty

    if not sys.stdin.isatty():
        return lambda: None, lambda: None
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)

    def read_key():
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return os.read(descriptor, 1).decode("utf-8", errors="ignore") if readable else None

    def restore():
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)

    return read_key, restore


def run_terminal_dashboard(snapshot_provider, dashboard_url, *, interval=2, stop_event=None,
                           owner=True, action_handler=None):
    """Draw a live interactive dashboard until Q or Ctrl+C."""
    if not sys.stdout.isatty() or os.getenv("TERM", "").casefold() == "dumb":
        return
    terminal_width = shutil.get_terminal_size((88, 24)).columns
    use_color = os.getenv("NO_COLOR") is None
    stop = stop_event or threading.Event()
    actions = action_handler or dashboard_action_handler(dashboard_url)
    read_key, restore_input = _terminal_reader()
    next_refresh = 0
    notice = ""
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()
    try:
        while not stop.is_set():
            key = read_key()
            if key:
                notice = handle_terminal_key(key, actions, owner=owner, stop_event=stop)
                next_refresh = 0
            if time.monotonic() >= next_refresh and not stop.is_set():
                try:
                    snapshot, jobs = snapshot_provider()
                    frame = render_dashboard(
                        snapshot, jobs, dashboard_url,
                        width=terminal_width, color=use_color, owner=owner, notice=notice,
                    )
                except Exception as exc:
                    frame = render_dashboard(
                        {"companies": 0}, [], dashboard_url,
                        width=terminal_width, color=use_color, owner=owner,
                        notice=f"Waiting for dashboard: {_clip(exc, terminal_width - 4)}",
                    )
                sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
                sys.stdout.flush()
                next_refresh = time.monotonic() + interval
            stop.wait(0.08)
    except KeyboardInterrupt:
        pass
    finally:
        restore_input()
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

"""Desktop-friendly local dashboard entry point."""

import logging
import os
import socket
import webbrowser
from pathlib import Path
from threading import Timer

from .web import main as serve_dashboard


def main():
    port = int(os.getenv("JOB_SCRAPER_PORT", "8765"))
    address = ("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}"
    try:
        with socket.create_connection(address, timeout=0.4):
            webbrowser.open(url)
            return
    except OSError:
        pass

    log_path = Path("logs") / "job-scraper.log"
    log_path.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )
    Timer(1.1, lambda: webbrowser.open(url)).start()
    logging.getLogger(__name__).info("Opening Career Radar at %s", url)
    serve_dashboard()

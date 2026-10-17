import os

import requests


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, session=None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.session = session or requests.Session()

    def send(self, company: str, title: str, location: str, url: str):
        if not self.token or not self.chat_id:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before sending alerts")
        lines = [f"New job at {company}", title]
        if location:
            lines.append(f"Location: {location}")
        lines.append(url)
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": "\n".join(lines), "disable_web_page_preview": True},
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # Telegram places the bot token in the URL path; never let Requests' exception text leak it to logs.
            raise RuntimeError(f"Telegram request failed ({type(exc).__name__})") from None
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected alert: {payload.get('description', 'unknown error')}")

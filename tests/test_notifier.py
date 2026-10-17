import requests

from job_scraper.notifier import TelegramNotifier


class BrokenSession:
    def post(self, url, **kwargs):
        raise requests.ConnectionError(f"failed to connect to {url}")


def test_telegram_transport_errors_do_not_expose_bot_token():
    notifier = TelegramNotifier("secret-token", "@channel", session=BrokenSession())
    try:
        notifier.send("Acme", "Engineer", "Remote", "https://example.test/job")
    except RuntimeError as error:
        assert "secret-token" not in str(error)
        assert "ConnectionError" in str(error)
    else:
        raise AssertionError("Expected transport failure")

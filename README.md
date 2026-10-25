# Job Portal Scraper

Monitor company career pages and send Telegram alerts when new matching jobs appear. The scraper runs on your computer or server; it does not require a hosted service.

## Install

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

After setup, start the dashboard from the project folder:

```bash
python start_dashboard.py
```

It opens the dashboard in your browser. Leave this Terminal window running while you want monitoring. Choose **Quit dashboard** in the sidebar to stop it. Logs are written to `logs/job-scraper.log`.

The dashboard is available at [http://127.0.0.1:8765](http://127.0.0.1:8765) and is bound to this computer only. Use **Add a company** to manage career pages and optional role and location filters. On the overview, choose **Start monitoring** to run scans every 30 minutes, or use **Scan now** for a one-off check. The first scan silently records current listings; later scans alert Telegram for new matching jobs. Use **Settings** to enter your Telegram bot token and channel/chat ID, select an interval, and send a test message.

Company settings continue to be saved in `companies.json`; job and scan history remain in `jobs.sqlite3`. Telegram settings are saved locally in the git-ignored `settings.json`. If you already set Telegram environment variables, the dashboard can use those until you save settings in the UI.

Optional browser fallback for career sites rendered by JavaScript:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Create a Telegram bot with BotFather, add it to your channel or group, and grant permission to post. Set the bot token and destination ID before running the scraper:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="@your_channel_or_chat_id"
```

For a private channel, use its numeric chat ID (often beginning with `-100`). Keep the bot token private. The application reads environment variables and does not load `.env` automatically.

## Add companies

```bash
job-scraper company add "Acme" "https://boards.greenhouse.io/acme" --keyword engineer --location remote
job-scraper company add "Example Corp" "https://example.wd5.myworkdayjobs.com/en-US/External"
job-scraper company list
job-scraper company remove "Acme"
```

Use `--keyword` and `--location` more than once to add alternatives. A job must match at least one configured keyword and at least one configured location. With no filters, all newly discovered jobs qualify. `company add` updates a company with the same name. The command stores company entries in `companies.json`; you can also edit this file directly:

```json
[
  {
    "name": "Acme",
    "url": "https://boards.greenhouse.io/acme",
    "keywords": ["engineer", "developer"],
    "locations": ["remote", "New York"]
  }
]
```

The config file is loaded into the SQLite database when a command runs. The default database is `jobs.sqlite3`. Keep both files in a persistent directory and back them up if needed.

## Run

Run a single scan to check setup:

```bash
job-scraper scan
```

Start continuous monitoring; it scans immediately and then every 30 minutes:

```bash
job-scraper run
```

Change the interval in seconds with `--interval` (for example, `--interval 900` for 15 minutes). Global options go before the command. Logs report scan and Telegram failures. Failed Telegram alerts remain pending and are retried on the next scan. The first successful scan records current jobs silently; subsequent scans alert only for new matching job URLs.

## Portal support

- Greenhouse URLs use the public boards API. Careers pages with an embedded Greenhouse widget are detected from the page and its site script, even when the company URL itself does not contain `greenhouse.io`.
- Workday career-site URLs use the site's public job search endpoint.
- Other pages are parsed for JobPosting structured data and likely job links. If no static listings are found, installing the optional browser extra enables a Chromium fallback.

Career pages vary and may change their layout or block automated requests. Check logs for a portal that returns no listings or errors. Polling every 30 minutes is the default; respect each site's terms and request limits.

## Tests

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

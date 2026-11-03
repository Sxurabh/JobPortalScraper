"""Run one GitHub Actions scan with Google Sheets as persistent storage."""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .env import load_project_env
from .google_sheets import GitHubSheetsStore
from .models import Company
from .notifier import TelegramNotifier
from .service import JobService

LOG = logging.getLogger(__name__)


class _DeferredDeliveryService(JobService):
    """Keep pending alerts pending until the scan has been saved to Sheets."""

    def _deliver_pending(self):
        return


class _GitHubProgressGroups:
    """Make each company's scraper output a collapsible Actions log group."""

    def __init__(self):
        self.current_company = ""

    def __call__(self, completed, total, company_name):
        if not company_name or company_name == self.current_company:
            return
        self.close()
        print(f"::group::Company {min(completed + 1, total)}/{total}: {company_name}", flush=True)
        self.current_company = company_name

    def close(self):
        if self.current_company:
            print("::endgroup::", flush=True)
            self.current_company = ""


def _read_companies(path: Path) -> list[Company]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Company configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Company configuration is not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("Company configuration must be a non-empty JSON array.")
    return [Company.from_dict(item) for item in raw]


def _write_step_summary(lines):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")


def _markdown_cell(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip() or "—"


def _build_summary(*, started_at, store, save_counts, new_job_count, enrichment_count,
                   enrichment_remaining, alert_count, delivered, delivery_failures):
    health = store.company_health()
    rows = []
    succeeded = failed = deferred = paused = 0
    for company in health:
        name = company["name"]
        if not company.get("enabled"):
            result = "Paused"
            paused += 1
        elif store.company_scan_deferred(name):
            result = "Deferred (retry backoff)"
            deferred += 1
        elif company.get("last_scan_status") == "error":
            result = "Failed"
            failed += 1
        else:
            result = "Scanned"
            succeeded += 1
        baseline = "Set" if company.get("baseline_done") else "Pending"
        issue = company.get("last_scan_error", "") if result == "Failed" else ""
        count = company.get("last_job_count", "") if result == "Scanned" else ""
        rows.append(
            f"| {_markdown_cell(name)} | {result} | {_markdown_cell(count)} | {baseline} | {_markdown_cell(issue)} |"
        )

    return [
        "## Daily job scan summary",
        f"- Started: {started_at}",
        f"- Companies: {len(health)} ({succeeded} scanned, {failed} failed, {deferred} deferred, {paused} paused)",
        f"- GitHub Jobs rows: {store.job_count} total; {new_job_count} newly discovered this run",
        f"- Sheet writes: {save_counts['jobs_added']} rows appended, {save_counts['jobs_updated']} updated; "
        f"{save_counts['companies_added']} company states added, {save_counts['companies_updated']} updated",
        f"- Descriptions enriched this run: {enrichment_count}; queued for later: {enrichment_remaining}",
        f"- Telegram alerts: {delivered}/{alert_count} sent; {delivery_failures} delivery failures remain queued",
        "",
        "### Per-company results",
        "| Company | Result | Jobs found | Baseline | Latest issue |",
        "| --- | --- | ---: | --- | --- |",
        *rows,
    ]


def main(argv=None):
    load_project_env()
    parser = argparse.ArgumentParser(description="Scan company portals and persist GitHub Actions history to Google Sheets.")
    parser.add_argument("--config", default="companies.json", help="Company configuration JSON file")
    parser.add_argument(
        "--max-enrichment-seconds", type=int,
        default=int(os.getenv("JOB_SCRAPER_MAX_ENRICHMENT_SECONDS", "900")),
        help="Maximum time to spend enriching descriptions during this run (default: 900)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Verify configuration and Google Sheets access, creating the GitHub tabs if needed, without scanning portals",
    )
    args = parser.parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    companies = _read_companies(config_path)
    print(f"Watchlist loaded: {len(companies)} companies", flush=True)
    store = GitHubSheetsStore(os.getenv("GOOGLE_SPREADSHEET_ID", ""))
    if args.check:
        print(
            f"Google Sheets connected; GitHub Jobs has {store.job_count} saved jobs and "
            f"GitHub State has {store.company_count} company records.",
            flush=True,
        )
        return 0

    configured_names = {company.name.casefold() for company in companies}
    for existing in store.companies():
        if existing.name.casefold() not in configured_names:
            existing.enabled = False
            store.upsert_company(existing)
    for company in companies:
        store.upsert_company(company)
    # Persist new company configuration before scraping so even an empty first
    # scan has durable baseline state on its next run.
    store.save()

    service = _DeferredDeliveryService(store, notifier=TelegramNotifier())
    starting_keys = set(store._jobs)
    progress = _GitHubProgressGroups()
    try:
        service.scan_once(on_progress=progress)
    finally:
        progress.close()

    deadline = time.monotonic() + max(0, args.max_enrichment_seconds)
    enrichment_count = 0
    while time.monotonic() < deadline and service.enrich_one_job():
        enrichment_count += 1
    remaining = store.pending_job_enrichment_count()
    if remaining:
        LOG.info("Leaving %d job descriptions queued for a later run", remaining)

    # Persist scanned jobs, baseline/retry metadata, and enrichment results
    # before attempting Telegram delivery. If a Sheets write has an ambiguous
    # network result, the next workflow run reloads keys before trying again.
    save_counts = store.save()
    alert_candidates = store.pending()
    delivered = delivery_failures = 0
    for row in alert_candidates:
        try:
            service.notifier.send(row["company_name"], row["title"], row["location"], row["url"])
        except Exception:
            LOG.exception("Telegram delivery failed for %s; it remains queued", row["url"])
            delivery_failures += 1
            continue
        store.mark_sent(row["company_name"], row["url"])
        alert_save = store.save()
        save_counts["jobs_updated"] += alert_save["jobs_updated"]
        delivered += 1

    _write_step_summary(_build_summary(
        started_at=started_at,
        store=store,
        save_counts=save_counts,
        new_job_count=len(set(store._jobs) - starting_keys),
        enrichment_count=enrichment_count,
        enrichment_remaining=remaining,
        alert_count=len(alert_candidates),
        delivered=delivered,
        delivery_failures=delivery_failures,
    ))
    print(
        f"Scan complete: {store.job_count} GitHub Jobs rows; "
        f"{len(set(store._jobs) - starting_keys)} new; {delivered} Telegram alerts sent.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

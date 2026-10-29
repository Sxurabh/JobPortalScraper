import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .models import Company
from .service import JobService
from .storage import Storage


def _parser():
    parser = argparse.ArgumentParser(prog="job-scraper", description="Monitor career pages and send new job alerts to Telegram.")
    parser.add_argument("--config", default="companies.json", help="Company configuration file (default: companies.json)")
    parser.add_argument("--db", default="jobs.sqlite3", help="SQLite database path (default: jobs.sqlite3)")
    parser.add_argument("--interval", type=int, default=1800, help="Polling interval in seconds (default: 1800)")
    sub = parser.add_subparsers(dest="command", required=True)
    company = sub.add_parser("company", help="Manage tracked companies")
    company_sub = company.add_subparsers(dest="company_command", required=True)
    add = company_sub.add_parser("add", help="Add or update a company")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--keyword", action="append", default=[], help="Include matching term; repeatable")
    add.add_argument("--location", action="append", default=[], help="Include matching location; repeatable")
    remove = company_sub.add_parser("remove", help="Remove a company and its stored jobs")
    remove.add_argument("name")
    company_sub.add_parser("list", help="List tracked companies")
    sub.add_parser("run", help="Scan continuously")
    sub.add_parser("scan", help="Run one scan")
    return parser


def _load_config(path: Path) -> list[Company]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array of company objects")
    return [Company.from_dict(entry) for entry in data]


def _save_config(path: Path, companies: list[Company]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([company.to_dict() for company in companies], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None):
    args = _parser().parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(args.config)
    storage = Storage(args.db)
    try:
        for company in _load_config(config_path):
            storage.upsert_company(company)
        if args.command == "company":
            companies = storage.companies()
            if args.company_command == "add":
                company = Company(args.name, args.url, args.keyword, args.location)
                if not company.name or not company.url.startswith(("https://", "http://")):
                    raise ValueError("Company name and a valid http(s) career URL are required")
                storage.upsert_company(company)
                companies = [c for c in companies if c.name.casefold() != company.name.casefold()] + [company]
                _save_config(config_path, sorted(companies, key=lambda c: c.name.casefold()))
                print(f"Saved {company.name}")
            elif args.company_command == "remove":
                removed = storage.remove_company(args.name)
                _save_config(config_path, [c for c in companies if c.name.casefold() != args.name.casefold()])
                print("Removed" if removed else "Company not found")
            else:
                for company in storage.companies():
                    filters = []
                    filters.append("included" if company.enabled else "excluded from scans")
                    if company.keywords:
                        filters.append("keywords=" + ",".join(company.keywords))
                    if company.locations:
                        filters.append("locations=" + ",".join(company.locations))
                    print(f"{company.name}\t{company.url}\t" + " | ".join(filters))
            return 0
        service = JobService(storage)
        if args.command == "scan":
            service.scan_once()
        else:
            if args.interval < 1:
                raise ValueError("Interval must be at least one second")
            service.run_forever(args.interval)
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())

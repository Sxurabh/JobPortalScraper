from urllib.parse import unquote

from job_scraper.google_sheets import (
    GITHUB_JOB_COLUMNS,
    GITHUB_STATE_COLUMNS,
    JOB_COLUMNS,
    GitHubSheetsStore,
)
from job_scraper.models import Company, Job
from job_scraper.github_scan import _DeferredDeliveryService
from job_scraper.storage import _stable_job_key


class Response:
    def __init__(self, data=None):
        self.data = data or {}
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class MemorySheets:
    def __init__(self):
        self.tabs = {"Jobs": [list(JOB_COLUMNS), ["local-row", "local-only"]]}
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        if method == "GET" and url.endswith("/spreadsheets/sheet-id"):
            return Response({"sheets": [{"properties": {"title": tab}} for tab in self.tabs]})
        if method == "POST" and url.endswith("/values:batchUpdate"):
            for item in json["data"]:
                tab, cell_range = _parse_range(item["range"])
                row_number = int(cell_range.split(":", 1)[0].rsplit("A", 1)[1])
                self.tabs[tab][row_number - 1] = item["values"][0]
            return Response()
        if method == "POST" and url.endswith(":batchUpdate"):
            for request in json.get("requests", []):
                title = request["addSheet"]["properties"]["title"]
                self.tabs.setdefault(title, [])
            return Response()
        if "/values/" in url:
            suffix = url.split("/values/", 1)[1]
            if method == "GET":
                tab, cell_range = _parse_range(suffix)
                start = int(cell_range.split(":", 1)[0][1:] or "1") if cell_range.startswith("A") else 1
                rows = self.tabs.get(tab, [])
                if cell_range.endswith("1"):
                    return Response({"values": rows[:1]})
                return Response({"values": rows[start - 1:]})
            if method == "PUT":
                tab, cell_range = _parse_range(suffix)
                start = int(cell_range.split(":", 1)[0][1:] or "1") if cell_range.startswith("A") else 1
                rows = self.tabs.setdefault(tab, [])
                for offset, row in enumerate(json["values"]):
                    while len(rows) < start + offset:
                        rows.append([])
                    rows[start - 1 + offset] = row
                return Response()
            if method == "POST" and ":append" in suffix:
                tab, _ = _parse_range(suffix)
                rows = self.tabs.setdefault(tab, [])
                first = len(rows) + 1
                rows.extend(json["values"])
                end_column = _column(len(json["values"][0]))
                return Response({"updates": {"updatedRange": f"'{tab}'!A{first}:{end_column}{len(rows)}"}})
        raise AssertionError(f"Unexpected Sheets request: {method} {url}")


def _parse_range(value):
    value = unquote(value)
    tab_range = value.split("/", 1)[0]
    if "!" not in tab_range:
        tab_range = value
    tab, cell_range = tab_range.split("!", 1)
    return tab.strip("'"), cell_range


def _column(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _store(tmp_path, session):
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    class TestStore(GitHubSheetsStore):
        def _access_token(self):
            return "token"

    return TestStore("sheet-id", credentials_file=str(credentials), session=session)


def test_store_creates_separate_tabs_and_leaves_local_jobs_tab_untouched(tmp_path):
    remote = MemorySheets()
    store = _store(tmp_path, remote)

    assert remote.tabs["GitHub Jobs"][0] == list(GITHUB_JOB_COLUMNS)
    assert remote.tabs["GitHub State"][0] == list(GITHUB_STATE_COLUMNS)
    assert remote.tabs["Jobs"] == [list(JOB_COLUMNS), ["local-row", "local-only"]]
    store.save()
    assert remote.tabs["Jobs"] == [list(JOB_COLUMNS), ["local-row", "local-only"]]


def test_scan_state_deduplicates_existing_keys_and_appends_only_new_jobs(tmp_path):
    remote = MemorySheets()
    company = Company("Acme", "https://careers.example.com", locations=["Pune"])
    first = Job("Engineer", "https://careers.example.com/job/1", "Pune", "Original description")
    second = Job("Analyst", "https://careers.example.com/job/2", "Pune", "Second description")
    store = _store(tmp_path, remote)
    store.upsert_company(company)
    store.save_baseline(company, [first])
    store.save()

    assert store.baseline_done("Acme") is True
    assert len(remote.tabs["GitHub Jobs"]) == 2
    assert remote.tabs["GitHub Jobs"][1][0] == _stable_job_key("Acme", first.url)
    assert remote.tabs["GitHub Jobs"][1][8] == "suppressed"

    reopened = _store(tmp_path, remote)
    reopened.upsert_company(company)
    reopened.save_new_jobs(company, [Job("Engineer II", first.url, "Pune", "Updated description with extra detail"), second])
    assert len(reopened.pending()) == 1
    reopened.save()

    rows = remote.tabs["GitHub Jobs"]
    assert len(rows) == 3
    assert sum(row[0] == _stable_job_key("Acme", first.url) for row in rows) == 1
    assert rows[1][3] == "Engineer II"
    assert rows[1][6] == "Updated description with extra detail"
    assert rows[2][0] == _stable_job_key("Acme", second.url)

    # A repeat after reopening updates the same rows and does not append duplicates.
    repeated = _store(tmp_path, remote)
    repeated.upsert_company(company)
    repeated.save_new_jobs(company, [Job("Engineer II", first.url, "Pune", "Updated description with extra detail"), second])
    repeated.save()
    assert len(remote.tabs["GitHub Jobs"]) == 3


def test_successive_scans_archive_and_reactivate_jobs_and_errors_do_not_count_as_misses(tmp_path):
    remote = MemorySheets()
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1")
    store = _store(tmp_path, remote)
    store.upsert_company(company)
    store.save_baseline(company, [job])

    store.record_scan("Acme", "ok", 0, seen_urls=[])
    store.record_scan("Acme", "error", error="temporary", retry_after_seconds=60)
    assert next(iter(store._jobs.values()))["missing_successful_scans"] == 1
    store.record_scan("Acme", "ok", 0, seen_urls=[])
    store.record_scan("Acme", "ok", 0, seen_urls=[])
    row = next(iter(store._jobs.values()))
    assert row["is_active"] is False
    assert row["missing_successful_scans"] == 3

    store.record_scan("Acme", "ok", 1, seen_urls=[job.url])
    assert row["is_active"] is True
    assert row["missing_successful_scans"] == 0


def test_company_failure_and_retry_state_survive_reopening(tmp_path):
    remote = MemorySheets()
    store = _store(tmp_path, remote)
    store.upsert_company(Company("Acme", "https://careers.example.com"))
    store.record_scan("Acme", "error", error="timeout", error_type="network", retry_after_seconds=300)
    store.save()

    reopened = _store(tmp_path, remote)
    assert reopened.scan_failure_streak("Acme") == 1
    assert reopened.company_scan_deferred("Acme") is True
    reopened.record_scan("Acme", "ok", 0, seen_urls=[])
    reopened.save()
    again = _store(tmp_path, remote)
    assert again.scan_failure_streak("Acme") == 0
    assert again.company_scan_deferred("Acme") is False


def test_full_description_and_application_tracking_are_preserved_on_refresh(tmp_path):
    remote = MemorySheets()
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1", "Pune", "Long full description" * 10)
    store = _store(tmp_path, remote)
    store.upsert_company(company)
    store.save_baseline(company, [job])
    store.save_job_details("Acme", job.url, job.description)
    store.update_application_tracking("Acme", job.url, "applied", "Follow up Friday")
    store.save()

    reopened = _store(tmp_path, remote)
    reopened.upsert_company(company)
    reopened.save_new_jobs(company, [Job("Engineer", job.url, "", "Short summary")])
    row = next(iter(reopened._jobs.values()))
    assert row["description"] == job.description
    assert row["description_status"] == "full"
    assert row["application_status"] == "applied"
    assert row["application_notes"] == "Follow up Friday"


def test_empty_first_scan_persists_baseline_and_new_jobs_are_alerted_once(tmp_path):
    remote = MemorySheets()
    company = Company("Acme", "https://careers.example.com")
    store = _store(tmp_path, remote)
    store.upsert_company(company)
    store.save()

    class SequenceScraper:
        batches = [[], [Job("Engineer", "https://careers.example.com/job/1", "Pune", "Full description")]]

        def fetch_company(self, _company):
            return self.batches.pop(0)

    scraper = SequenceScraper()
    first = _DeferredDeliveryService(store, scraper=scraper)
    first.scan_once()
    store.save()
    assert store.baseline_done("Acme") is True
    assert store.pending() == []

    second = _DeferredDeliveryService(store, scraper=scraper)
    second.scan_once()
    store.save()
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["title"] == "Engineer"

    store.mark_sent("Acme", pending[0]["url"])
    store.save()
    reopened = _store(tmp_path, remote)
    assert reopened.pending() == []


def test_ambiguous_append_response_refreshes_keys_before_retry(tmp_path):
    class AppendThenTimeout(MemorySheets):
        fail_after_append = True

        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            if method == "POST" and ":append" in url and self.fail_after_append:
                self.fail_after_append = False
                result = super().request(method, url, params=params, json=json, headers=headers, timeout=timeout)
                raise TimeoutError("simulated response timeout after append")
            return super().request(method, url, params=params, json=json, headers=headers, timeout=timeout)

    remote = AppendThenTimeout()
    store = _store(tmp_path, remote)
    company = Company("Acme", "https://careers.example.com")
    job = Job("Engineer", "https://careers.example.com/job/1")
    store.upsert_company(company)
    store.save_baseline(company, [job])

    try:
        store.save()
    except TimeoutError:
        pass
    else:
        raise AssertionError("Expected the simulated ambiguous append failure")

    store.save()
    assert len(remote.tabs["GitHub Jobs"]) == 2
    assert sum(row[0] == _stable_job_key("Acme", job.url) for row in remote.tabs["GitHub Jobs"]) == 1


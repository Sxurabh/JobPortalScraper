from job_scraper.google_sheets import GoogleSheetsMirror, JOB_COLUMNS


class FakeResponse:
    def __init__(self, data=None):
        self.data = data or {}
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        if method == "GET" and url.endswith("/values/Jobs!A:A"):
            return FakeResponse({"values": self.values})
        if method == "GET" and url.endswith("/values/Jobs!A1:L1"):
            return FakeResponse({"values": [list(JOB_COLUMNS)]})
        if method == "GET":
            return FakeResponse({"sheets": [{"properties": {"title": "Jobs"}}]})
        return FakeResponse()


def _job(key, company="Acme", url_suffix="1"):
    return {
        "job_key": key,
        "company_name": company,
        "title": "Engineer",
        "location": "Remote",
        "url": f"https://careers.example.com/job/{url_suffix}",
        "description": "Full job description",
        "first_seen_at": "2026-10-01T00:00:00+00:00",
        "last_seen_at": "2026-10-02T00:00:00+00:00",
        "is_active": True,
        "notification_status": "sent",
        "application_status": "applied",
        "application_notes": "Follow up",
    }


def test_google_sheet_sync_updates_existing_keys_and_appends_new_keys(tmp_path):
    key_file = tmp_path / "service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    session = FakeSession([list(JOB_COLUMNS), ["existing"]])
    mirror = GoogleSheetsMirror("spreadsheet-id-123", credentials_file=str(key_file), session=session)
    mirror._access_token = lambda: "fake-token"

    mirror.sync_jobs([_job("existing"), _job("new", url_suffix="2")])

    update = next(call for call in session.calls if call["url"].endswith("/values:batchUpdate"))
    append = next(call for call in session.calls if "/values/Jobs!A:L:append" in call["url"])
    assert update["json"]["data"][0]["range"] == "Jobs!A2:L2"
    assert update["json"]["data"][0]["values"][0][0] == "existing"
    assert append["json"]["values"][0][0] == "new"
    assert append["json"]["values"][0][-1] == "Follow up"
    assert append["params"]["valueInputOption"] == "RAW"


def test_google_sheet_sync_creates_jobs_tab_and_header_when_missing(tmp_path):
    class EmptySheetSession(FakeSession):
        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "params": params, "json": json})
            if method == "GET" and url.endswith("/values/Jobs!A1:L1"):
                return FakeResponse()
            if method == "GET" and url.endswith("/values/Jobs!A:A"):
                return FakeResponse()
            if method == "GET":
                return FakeResponse({"sheets": []})
            return FakeResponse()

    key_file = tmp_path / "service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    session = EmptySheetSession([])
    mirror = GoogleSheetsMirror("spreadsheet-id-123", credentials_file=str(key_file), session=session)
    mirror._access_token = lambda: "fake-token"

    mirror.sync_jobs([])

    assert any(call["url"].endswith(":batchUpdate") for call in session.calls)
    header_write = next(call for call in session.calls if call["method"] == "PUT")
    assert header_write["json"]["values"] == [list(JOB_COLUMNS)]


def test_google_sheet_sync_reports_missing_configuration():
    mirror = GoogleSheetsMirror("")
    try:
        mirror.sync_jobs([])
    except RuntimeError as exc:
        assert "spreadsheet ID" in str(exc)
    else:
        raise AssertionError("Expected missing spreadsheet ID to be reported")


def test_google_sheet_sync_reports_missing_service_account_file(tmp_path):
    mirror = GoogleSheetsMirror(
        "spreadsheet-id-123", credentials_file=str(tmp_path / "missing.json")
    )
    try:
        mirror.sync_jobs([])
    except RuntimeError as exc:
        assert "GOOGLE_SERVICE_ACCOUNT_FILE" in str(exc)
    else:
        raise AssertionError("Expected missing service-account credentials to be reported")


def test_google_sheet_sync_refuses_to_overwrite_an_unrelated_jobs_tab(tmp_path):
    class MismatchedHeaderSession(FakeSession):
        def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "params": params, "json": json})
            if method == "GET" and url.endswith("/values/Jobs!A1:L1"):
                return FakeResponse({"values": [["My existing data"]]})
            if method == "GET":
                return FakeResponse({"sheets": [{"properties": {"title": "Jobs"}}]})
            return FakeResponse()

    key_file = tmp_path / "service-account.json"
    key_file.write_text("{}", encoding="utf-8")
    mirror = GoogleSheetsMirror(
        "spreadsheet-id-123", credentials_file=str(key_file), session=MismatchedHeaderSession([])
    )
    mirror._access_token = lambda: "fake-token"

    try:
        mirror.sync_jobs([])
    except RuntimeError as exc:
        assert "different columns" in str(exc)
    else:
        raise AssertionError("Expected an incompatible Jobs header to fail safely")

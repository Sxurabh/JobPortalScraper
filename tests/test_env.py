import os

from job_scraper.env import load_project_env


def test_env_loader_reads_settings_and_resolves_service_key_relative_to_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    key_file = tmp_path / "service-key.json"
    key_file.write_text("{}", encoding="utf-8")
    env_file.write_text(
        "GOOGLE_SERVICE_ACCOUNT_FILE=service-key.json\nGOOGLE_SPREADSHEET_ID='sheet_1234567890'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_SPREADSHEET_ID", raising=False)

    loaded = load_project_env(env_file)

    assert loaded == ["GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SPREADSHEET_ID"]
    assert os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] == str(key_file.resolve())
    assert os.environ["GOOGLE_SPREADSHEET_ID"] == "sheet_1234567890"


def test_process_environment_takes_precedence_over_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("EXAMPLE_SETTING=from-file\n", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SETTING", "from-process")

    assert load_project_env(env_file) == []
    assert os.environ["EXAMPLE_SETTING"] == "from-process"

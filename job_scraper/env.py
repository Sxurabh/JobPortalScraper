"""Small .env loader for local, non-secret app configuration."""

import os
import re
from pathlib import Path


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_project_env(path: str | Path | None = None) -> list[str]:
    """Load project .env values without overriding the process environment.

    Relative service-account paths are resolved relative to the .env file so the
    dashboard works even when it is launched from another working directory.
    """
    env_path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return []

    loaded = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key) or key in os.environ:
            continue
        value = _parse_value(value.strip())
        if key == "GOOGLE_SERVICE_ACCOUNT_FILE" and value:
            credential_path = Path(value).expanduser()
            if not credential_path.is_absolute():
                credential_path = env_path.parent / credential_path
            value = str(credential_path.resolve())
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _parse_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # dotenv-compatible unquoted inline comment, preserving hashes within URLs.
    marker = value.find(" #")
    return value[:marker].rstrip() if marker >= 0 else value

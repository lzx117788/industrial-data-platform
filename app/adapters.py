import csv
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Settings


class AdapterError(ValueError):
    pass


def import_path(value: str, settings: Settings) -> Path:
    if not isinstance(value, str) or not value:
        raise AdapterError("config.path is required")
    root = settings.import_root.resolve()
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise AdapterError("Source files must stay within IMPORT_ROOT")
    return target


def validate_config(kind: str, config: dict, settings: Settings):
    if kind in {"csv", "sqlite"}:
        import_path(config.get("path"), settings)
    if kind == "sqlite" and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(config.get("table", ""))
    ):
        raise AdapterError("config.table must be a simple SQL table name")
    if kind == "http":
        try:
            url = urlparse(str(config.get("url", "")))
            _ = url.port  # Validate malformed or out-of-range port strings as well.
        except ValueError as exc:
            raise AdapterError("Invalid HTTP source URL") from exc
        if url.scheme not in {"http", "https"} or url.hostname not in settings.http_allowed_hosts:
            raise AdapterError("HTTP host must be listed in HTTP_ALLOWED_HOSTS")
        if url.username or url.password or url.fragment:
            raise AdapterError("Credentials and fragments are not allowed in source URLs")


def fetch_records(kind: str, config: dict, settings: Settings, transport=None) -> list[dict]:
    validate_config(kind, config, settings)
    if kind == "csv":
        with import_path(config["path"], settings).open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                rows.append(row)
                if len(rows) > settings.max_source_records:
                    raise AdapterError("Source exceeds max_source_records")
    elif kind == "sqlite":
        file = import_path(config["path"], settings)
        uri = file.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in connection.execute(
                    f'SELECT * FROM "{config["table"]}" LIMIT ?', (settings.max_source_records + 1,)
                )
            ]
    elif kind == "http":
        # Bounded, allowlisted snapshot API. Redirects cannot escape the host check.
        with httpx.Client(
            timeout=10, follow_redirects=False, transport=transport, trust_env=False
        ) as client:
            with client.stream("GET", config["url"]) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 5_000_000:
                        raise AdapterError("HTTP source exceeds 5 MB")
        payload = json.loads(content)
        rows = payload.get("items") if isinstance(payload, dict) else payload
    else:
        raise AdapterError("Unsupported source kind")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise AdapterError("Source must return a list of objects (or an object with items)")
    if len(rows) > settings.max_source_records:
        raise AdapterError("Source exceeds max_source_records")
    return rows

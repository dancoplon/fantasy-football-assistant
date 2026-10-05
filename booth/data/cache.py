"""Download-and-cache for public data files.

Files land in state/cache/. A fresh copy is reused; a failed download falls
back to the last good copy so one flaky source doesn't sink a report.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from booth.config import ROOT

CACHE_DIR = ROOT / "state" / "cache"
USER_AGENT = "booth-fantasy-assistant/0.1 (personal, non-commercial)"


class DataSourceError(RuntimeError):
    pass


def fetch(url: str, name: str, max_age_hours: float, cache_dir: Path | None = None) -> tuple[Path, bool]:
    """Return (path, stale). stale=True means the download failed and an older copy was used."""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return path, False
    try:
        resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException as exc:
        if path.exists():
            return path, True
        raise DataSourceError(f"Couldn't download {url}: {exc}") from exc
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path, False


def get_json(url: str, params: dict | None = None) -> dict | list:
    """Uncached JSON GET for small, fast-changing endpoints."""
    try:
        resp = requests.get(url, params=params, timeout=30, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise DataSourceError(f"Couldn't fetch {url}: {exc}") from exc

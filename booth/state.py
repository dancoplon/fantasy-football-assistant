"""Weekly state store: one JSON file per league week holding each run's snapshot.

state/2026-wk05.json = {"season": 2026, "week": 5, "runs": {"tue": {...}, "thu": {...}, ...}}
state/latest.json -> symlink to the most recently written week file.

Files are kept all season as a decision log.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from booth.config import ROOT

RUN_ORDER = ["tue", "thu", "sat", "sun"]
# Which earlier run each report diffs against (PRD: Sat vs Thu, Sun vs Sat).
DIFF_AGAINST = {"sat": "thu", "sun": "sat"}


def state_dir() -> Path:
    return ROOT / os.getenv("BOOTH_STATE_DIR", "state")


def week_path(season: int, week: int, base: Path | None = None) -> Path:
    return (base or state_dir()) / f"{season}-wk{week:02d}.json"


def load_week(season: int, week: int, base: Path | None = None) -> dict:
    path = week_path(season, week, base)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:  # bad JSON or bad bytes
            # A damaged file (e.g. a full disk mid-write) must not stop the rest of the week's
            # reports. Keep it for inspection and start the week fresh.
            path.replace(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
    return {"season": season, "week": week, "runs": {}}


def write_week(data: dict, base: Path | None = None) -> Path:
    """Write a week file atomically, so a crash or full disk never leaves it half-written."""
    path = week_path(data["season"], data["week"], base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    return path


def prior_snapshot(season: int, week: int, run: str, base: Path | None = None) -> dict | None:
    """The snapshot this run should diff against, falling back to the latest earlier run that week.

    Once the scheduler is tracking the week, only reports that reached Dan count: "No changes"
    must mean no changes from something he actually read.
    """
    data = load_week(season, week, base)
    runs = data["runs"]
    if "jobs" in data:
        runs = {r: s for r, s in runs.items() if data["jobs"].get(r, {}).get("delivered_at")}
    target = DIFF_AGAINST.get(run)
    if target is None:
        return None
    if target in runs:
        return runs[target]
    earlier = [r for r in RUN_ORDER[: RUN_ORDER.index(run)] if r in runs]
    return runs[earlier[-1]] if earlier else None


def save_snapshot(snapshot: dict, base: Path | None = None) -> Path:
    base = base or state_dir()
    base.mkdir(parents=True, exist_ok=True)
    data = load_week(snapshot["season"], snapshot["week"], base)
    data["runs"][snapshot["run"]] = snapshot
    path = write_week(data, base)
    latest = base / "latest.json"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(path.name)
    return path

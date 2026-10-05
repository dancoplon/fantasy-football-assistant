"""Manual roster fallback for when the Yahoo API isn't reachable or provisioned."""

from __future__ import annotations

import json

from booth.config import ROOT

MANUAL_ROSTER = ROOT / "config" / "manual_roster.json"


def manual_roster() -> dict:
    data = json.loads(MANUAL_ROSTER.read_text())
    return {"source": "manual", **{k: v for k, v in data.items() if not k.startswith("_")}}

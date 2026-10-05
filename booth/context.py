"""League config + strategy context loaded on every run."""

from __future__ import annotations

import json

from booth.config import ROOT

CONFIG_DIR = ROOT / "config"


def league_config() -> dict:
    data = json.loads((CONFIG_DIR / "league.json").read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def strategy_text() -> str:
    return (CONFIG_DIR / "strategy.md").read_text()

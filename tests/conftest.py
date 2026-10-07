import pytest


@pytest.fixture(autouse=True)
def _no_local_league_data(monkeypatch, tmp_path_factory):
    """Tests never read the Mac's real copies of Dan's Yahoo pages (gitignored local files)."""
    from booth import manual

    empty = tmp_path_factory.mktemp("no-local-data")
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", empty / "manual_roster.local.json")
    monkeypatch.setattr(manual, "MANUAL_FREE_AGENTS", empty / "manual_free_agents.json")
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", empty / "manual_matchup.json")
    monkeypatch.setattr(manual, "FAAB_MARKET", empty / "faab_market.json")


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, tmp_path_factory):
    """Tests never see the Mac's real Telegram token or saved chat, so nothing reaches Dan's phone."""
    from booth import deliver

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    empty = tmp_path_factory.mktemp("no-telegram")
    monkeypatch.setattr(deliver, "state_dir", lambda: empty)

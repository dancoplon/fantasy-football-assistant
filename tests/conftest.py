import pytest


@pytest.fixture(autouse=True)
def _no_local_league_data(monkeypatch, tmp_path_factory):
    """Tests never read the Mac's real copies of Dan's Yahoo pages (gitignored local files)."""
    from booth import manual

    empty = tmp_path_factory.mktemp("no-local-data")
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", empty / "manual_roster.local.json")
    monkeypatch.setattr(manual, "MANUAL_FREE_AGENTS", empty / "manual_free_agents.json")
    monkeypatch.setattr(manual, "MANUAL_MATCHUP", empty / "manual_matchup.json")

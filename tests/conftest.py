import pytest


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, tmp_path_factory):
    """Tests never see the Mac's real Telegram token or saved chat, so nothing reaches Dan's phone."""
    from booth import deliver

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    empty = tmp_path_factory.mktemp("no-telegram")
    monkeypatch.setattr(deliver, "state_dir", lambda: empty)

from unittest import mock

import pytest

from booth import deliver


def test_stdout_when_no_recipient(monkeypatch, capsys):
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("IMESSAGE_RECIPIENT", raising=False)
    monkeypatch.delenv("BOOTH_DELIVERY", raising=False)
    assert deliver.send("hello") == "stdout"
    assert "hello" in capsys.readouterr().out


def test_imessage_passes_text_as_argument(monkeypatch):
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.delenv("BOOTH_DELIVERY", raising=False)
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    tricky = 'He said "start him" \\ end tell'
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
        assert deliver.send(tricky) == "imessage"
    args, kwargs = run.call_args
    assert args[0] == ["osascript", "-", tricky, "+15555550123"]
    assert tricky not in kwargs["input"]  # text never spliced into the script


def test_imessage_failure_raises(monkeypatch):
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="not authorized")):
        with pytest.raises(deliver.DeliveryError, match="not authorized"):
            deliver.send("x")


def test_imessage_timeout_becomes_delivery_error(monkeypatch):
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.delenv("BOOTH_DELIVERY", raising=False)
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    with mock.patch.object(deliver.subprocess, "run", side_effect=deliver.subprocess.TimeoutExpired("osascript", 120)):
        with pytest.raises(deliver.DeliveryError, match="permission prompt"):
            deliver.send("x")


@pytest.fixture
def tg(monkeypatch, tmp_path):
    """Telegram configured: token in env, chat saved under a temp state dir; no real network."""
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(deliver, "state_dir", lambda: tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.delenv("BOOTH_DELIVERY", raising=False)
    monkeypatch.delenv("IMESSAGE_RECIPIENT", raising=False)
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        method = url.rsplit("/", 1)[1]
        result = {"getMe": {"username": "dan_booth_bot"},
                  "getUpdates": [{"message": {"chat": {"id": 42, "type": "private", "first_name": "Dan"}}}],
                  "sendMessage": {"message_id": 1}}[method]
        return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": result})

    monkeypatch.setattr(deliver.requests, "post", post)
    return calls


def test_telegram_setup_saves_chat_and_says_hello(tg, tmp_path):
    assert deliver.channel() == "imessage" or deliver.channel() == "stdout"  # not connected yet
    assert "dan_booth_bot" in deliver.telegram_setup()
    assert deliver.telegram_chat_id() == 42
    assert tg[-1][1]["chat_id"] == 42 and "connected" in tg[-1][1]["text"]
    assert deliver.channel() == "telegram"
    assert deliver.send("Booth: hi") == "telegram"
    assert tg[-1] == ("https://api.telegram.org/bot123:SECRET/sendMessage",
                      {"chat_id": 42, "text": "Booth: hi", "disable_web_page_preview": True})


def test_telegram_setup_needs_exactly_one_chat(tg, monkeypatch):
    def updates(chats):
        def post(url, json=None, timeout=None):
            method = url.rsplit("/", 1)[1]
            res = {"getMe": {"username": "b"}, "getUpdates": [
                {"message": {"chat": {"id": i, "type": "private", "first_name": n}}} for i, n in chats]}[method]
            return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": res})
        return post
    monkeypatch.setattr(deliver.requests, "post", updates([]))
    with pytest.raises(deliver.DeliveryError, match="send it \"hi\""):
        deliver.telegram_setup()
    monkeypatch.setattr(deliver.requests, "post", updates([(1, "Dan"), (2, "Stranger")]))
    with pytest.raises(deliver.DeliveryError, match="More than one"):
        deliver.telegram_setup()
    assert deliver.telegram_chat_id() is None


def test_long_messages_split_at_blank_lines():
    text = "\n\n".join(f"Section {i}\n" + "x" * 900 for i in range(10))
    parts = deliver._chunks(text)
    assert len(parts) == 3 and all(len(p) <= deliver.TELEGRAM_CHUNK for p in parts)
    assert "\n\n".join(parts) == text and all(p.startswith("Section") for p in parts)
    assert deliver._chunks("short") == ["short"]
    assert all(len(p) <= 4000 for p in deliver._chunks("y" * 9000))


def test_telegram_errors_never_show_the_token(tg, monkeypatch):
    deliver.telegram_setup()

    def down(url, json=None, timeout=None):
        raise deliver.requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(deliver.requests, "post", down)
    with pytest.raises(deliver.DeliveryError) as exc:
        deliver.send("report")
    assert "SECRET" not in str(exc.value) and "<token>" in str(exc.value)

    bad = mock.Mock(status_code=401, json=lambda: {"ok": False, "description": "Unauthorized"})
    monkeypatch.setattr(deliver.requests, "post", lambda *a, **k: bad)
    with pytest.raises(deliver.DeliveryError, match="check TELEGRAM_BOT_TOKEN"):
        deliver.send("report")


def test_telegram_failure_falls_back_to_imessage(tg, monkeypatch):
    deliver.telegram_setup()
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    monkeypatch.setattr(deliver.requests, "post", mock.Mock(side_effect=deliver.requests.Timeout("slow")))
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
        assert deliver.send("Booth: report") == "imessage (Telegram failed)"
    sent = run.call_args[0][0][2]
    assert sent.startswith("(Sent by text because Telegram failed") and sent.endswith("Booth: report")


def test_forced_channel_wins(tg, monkeypatch):
    deliver.telegram_setup()
    monkeypatch.setenv("BOOTH_DELIVERY", "stdout")
    assert deliver.channel() == "stdout"

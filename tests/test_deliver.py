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


TOKEN = "123456:AAFakeTokenForTests_x-y"


class FakeTelegram:
    """Stands in for api.telegram.org: records calls; getUpdates returns `messages`."""

    def __init__(self):
        self.calls = []
        self.messages = []  # (chat_id, first_name, text)

    def __call__(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        method = url.rsplit("/", 1)[1]
        result = {
            "getMe": {"username": "dan_booth_bot"},
            "getUpdates": [{"update_id": i, "message": {"chat": {"id": cid, "type": "private", "first_name": name},
                                                        "text": text}}
                           for i, (cid, name, text) in enumerate(self.messages)],
            "sendMessage": {"message_id": 1},
        }[method]
        return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": result})

    def code(self):
        """The setup code from the link Booth printed."""
        return deliver._setup_code()


@pytest.fixture
def tg(monkeypatch, tmp_path):
    """Telegram configured: token in env, state under a temp dir; no real network."""
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(deliver, "state_dir", lambda: tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("BOOTH_DELIVERY", raising=False)
    monkeypatch.delenv("IMESSAGE_RECIPIENT", raising=False)
    fake = FakeTelegram()
    monkeypatch.setattr(deliver.requests, "post", fake)
    return fake


def connect(tg):
    first = deliver.telegram_setup()
    tg.messages.append((42, "Dan", f"/start {tg.code()}"))
    return first, deliver.telegram_setup()


def test_telegram_setup_links_the_chat_that_sent_the_code(tg):
    (ok1, text1), (ok2, text2) = connect(tg)
    assert not ok1 and f"https://t.me/dan_booth_bot?start={tg.messages[0][2].split()[1]} " in text1
    assert ok2 and text2 == "Connected @dan_booth_bot to Dan. Sent a hello there."
    assert deliver.telegram_chat_id() == 42
    assert tg.calls[-1][1]["chat_id"] == 42 and "connected" in tg.calls[-1][1]["text"]
    assert deliver.send("Booth: hi") == "telegram"
    assert tg.calls[-1] == (f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            {"chat_id": 42, "text": "Booth: hi", "disable_web_page_preview": True})


def test_telegram_setup_ignores_strangers(tg):
    ok, _ = deliver.telegram_setup()
    tg.messages += [(7, "Dan", "hi"), (8, "Stranger", "/start"), (9, "Guesser", "/start 0000")]
    ok, text = deliver.telegram_setup()
    assert not ok and "start=" in text and deliver.telegram_chat_id() is None
    tg.messages.append((42, "Dan", f"/start {tg.code()}"))
    assert deliver.telegram_setup()[0] and deliver.telegram_chat_id() == 42


def test_setup_code_is_kept_until_used_then_renewed(tg, tmp_path):
    code = tg.code()
    assert tg.code() == code  # running setup twice gives the same link
    saved = deliver.json.loads((tmp_path / "telegram_setup.json").read_text())
    saved["created"] = "2026-01-01T00:00:00+00:00"
    (tmp_path / "telegram_setup.json").write_text(deliver.json.dumps(saved))
    assert tg.code() != code  # a day-old code is replaced


def test_telegram_setup_keeps_an_existing_link(tg):
    connect(tg)
    tg.messages = [(99, "Someone", "/start whatever")]
    ok, text = deliver.telegram_setup()
    assert ok and "already connected" in text and "--replace" in text
    assert deliver.telegram_chat_id() == 42
    ok, text = deliver.telegram_setup(replace=True)
    assert not ok and deliver.telegram_chat_id() == 42  # needs the new code first
    tg.messages.append((99, "Someone", f"/start {tg.code()}"))
    assert deliver.telegram_setup(replace=True)[0] and deliver.telegram_chat_id() == 99


def test_token_set_but_not_connected_is_never_silent(tg, capsys):
    assert deliver.channel() == "telegram"
    with pytest.raises(deliver.DeliveryError, match="uv run booth telegram-setup"):
        deliver.send("report")
    assert "report" not in capsys.readouterr().out  # not quietly printed to a log instead


def test_long_messages_split_at_blank_lines():
    text = "\n\n".join(f"Section {i}\n" + "x" * 900 for i in range(10))
    parts = deliver._chunks(text)
    assert len(parts) == 3 and all(len(p) <= deliver.TELEGRAM_CHUNK for p in parts)
    assert "\n\n".join(parts) == text and all(p.startswith("Section") for p in parts)
    assert deliver._chunks("short") == ["short"]
    assert all(len(p) <= 4000 for p in deliver._chunks("y" * 9000))


@pytest.mark.parametrize("shown", [
    lambda url: f"Max retries exceeded with url: {url}",
    lambda url: f"Max retries exceeded with url: {url.replace(':', '%3A')}",  # encoded
    lambda url: f"HTTPSConnectionPool(host='api.telegram.org'): url: '{url}'",
])
def test_telegram_errors_never_show_the_token(tg, monkeypatch, shown):
    connect(tg)

    def down(url, json=None, timeout=None):
        raise deliver.requests.ConnectionError(shown(url))
    monkeypatch.setattr(deliver.requests, "post", down)
    with pytest.raises(deliver.DeliveryError) as exc:
        deliver.send("report")
    assert "AAFake" not in str(exc.value) and "<token>" in str(exc.value)


def test_telegram_refusal_hints_at_the_token(tg, monkeypatch):
    connect(tg)
    bad = mock.Mock(status_code=401, json=lambda: {"ok": False, "description": "Unauthorized"})
    monkeypatch.setattr(deliver.requests, "post", lambda *a, **k: bad)
    with pytest.raises(deliver.DeliveryError, match="check TELEGRAM_BOT_TOKEN"):
        deliver.send("report")


@pytest.mark.parametrize("token", ["“123456:AAFake”", "123456:AAFake​", "123456 AAFake", "AAFake"])
def test_badly_pasted_token_is_caught_without_showing_it(tg, monkeypatch, token):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    with pytest.raises(deliver.DeliveryError, match="doesn't look like a bot token") as exc:
        deliver.telegram_setup()
    assert "AAFake" not in str(exc.value) and tg.calls == []


def test_telegram_failure_falls_back_to_imessage(tg, monkeypatch):
    connect(tg)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    monkeypatch.setattr(deliver.requests, "post", mock.Mock(side_effect=deliver.requests.Timeout("slow")))
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
        assert deliver.send("Booth: report") == "imessage (Telegram failed: Telegram unreachable: slow)"
    sent = run.call_args[0][0][2]
    assert sent.startswith("(Sent by text because Telegram failed: Telegram unreachable: slow)")
    assert sent.endswith("Booth: report")


def test_both_channels_failing_names_both(tg, monkeypatch):
    connect(tg)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    monkeypatch.setattr(deliver.requests, "post", mock.Mock(side_effect=deliver.requests.Timeout("slow")))
    with mock.patch.object(deliver.subprocess, "run", side_effect=deliver.subprocess.TimeoutExpired("osascript", 120)):
        with pytest.raises(deliver.DeliveryError) as exc:
            deliver.send("Booth: report")
    assert str(exc.value).startswith("Telegram failed (Telegram unreachable: slow); the iMessage backup also failed")
    assert "permission prompt" in str(exc.value)


def test_unset_telegram_with_imessage_backup_says_why(tg, monkeypatch):
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
        assert deliver.send("Booth: report").startswith("imessage (Telegram failed: Telegram isn't connected")
    assert "telegram-setup" in run.call_args[0][0][2]


def test_send_via_tests_one_channel_without_fallback(tg, monkeypatch):
    connect(tg)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+15555550123")
    monkeypatch.setattr(deliver.sys, "platform", "darwin")
    with mock.patch.object(deliver.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
        assert deliver.send("test", via="imessage") == "imessage"
    assert run.call_args[0][0][2] == "test"
    monkeypatch.setattr(deliver.requests, "post", mock.Mock(side_effect=deliver.requests.Timeout("slow")))
    with pytest.raises(deliver.DeliveryError, match="unreachable"):
        deliver.send("test", via="telegram")  # a test of Telegram must not pass by falling back


def test_forced_channel_wins(tg, monkeypatch):
    connect(tg)
    monkeypatch.setenv("BOOTH_DELIVERY", "stdout")
    assert deliver.channel() == "stdout"


def test_typing_the_code_works_too(tg):
    deliver.telegram_setup()
    tg.messages.append((42, "Dan", f"  {tg.code()} "))
    assert deliver.telegram_setup()[0] and deliver.telegram_chat_id() == 42

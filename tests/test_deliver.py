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

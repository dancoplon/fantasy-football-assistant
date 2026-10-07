import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from booth import chat, deliver, manual

TOKEN = "123456:AAFakeTokenForTests_x-y"
EXPECTED_API_SECRET = "737cd05f33237c58f337c1b85f2fcb06ebdbd89da88cbd5b6a8b0f1303260f07"
NOW = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)  # Sun 1:00 PM ET


@pytest.fixture
def roster(tmp_path, monkeypatch):
    """Dan's roster copy in a temp file."""
    path = tmp_path / "manual_roster.local.json"
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", path)
    path.write_text(json.dumps({
        "as_of": "2026-10-07", "faab_remaining": 95, "open_roster_spots": 1,
        "players": [
            {"name": "Joe Burrow", "position": "QB", "nfl_team": "CIN", "slot": "QB"},
            {"name": "Tank Bigsby", "position": "RB", "nfl_team": "PHI", "slot": "BN"},
            {"name": "Marvin Harrison Jr.", "position": "WR", "nfl_team": "ARI", "slot": "WR"},
            {"name": "Josh Downs", "position": "WR", "nfl_team": "IND", "slot": "BN"},
            {"name": "Josh Palmer", "position": "WR", "nfl_team": "BUF", "slot": "BN"},
        ],
    }))
    return path


def load(path):
    return json.loads(path.read_text())


# --- Roster moves ---

def test_add_records_the_player_and_spends_faab(roster):
    ok, detail = chat.apply_move({"action": "add", "player": "Tyson Bagent", "position": "qb", "nfl_team": "chi",
                                  "faab_spent": 12}, NOW)
    assert ok and detail == "added Tyson Bagent (QB, $12)"
    data = load(roster)
    assert data["players"][-1] == {"name": "Tyson Bagent", "position": "QB", "nfl_team": "CHI", "status": "", "slot": "BN"}
    assert data["faab_remaining"] == 83 and data["open_roster_spots"] == 0
    assert data["chat_updates"] == ["Oct 11 1:00 PM ET: added Tyson Bagent (QB, $12) (Dan, in Telegram)"]
    assert manual.check_roster(data) == ["only 6 players: looks like part of the roster (a full one is about 25 plus IR)"]


def test_add_refuses_duplicates_and_missing_positions(roster):
    assert chat.apply_move({"action": "add", "player": "Joe Burrow", "position": "QB"}, NOW) == (
        False, "Joe Burrow is already on the roster copy")
    assert not chat.apply_move({"action": "add", "player": "Somebody", "position": "FLEX"}, NOW)[0]
    assert len(load(roster)["players"]) == 5


def test_drop_and_slot_match_names_loosely(roster):
    assert chat.apply_move({"action": "drop", "player": "Bigsby"}, NOW) == (True, "dropped Tank Bigsby")
    assert load(roster)["open_roster_spots"] == 2
    assert chat.apply_move({"action": "slot", "player": "Marvin Harrison", "slot": "bn"}, NOW) == (
        True, "moved Marvin Harrison Jr. to BN")
    assert chat.apply_move({"action": "slot", "player": "marvin harrison jr", "slot": "W/R/T"}, NOW)[0]
    # "Josh" matches two players: never guess which one
    assert chat.apply_move({"action": "drop", "player": "Josh"}, NOW) == (False, "couldn't find Josh on the roster copy")
    assert chat.apply_move({"action": "slot", "player": "Burrow", "slot": "FLEX"}, NOW) == (False, '"FLEX" isn\'t a roster slot')


def test_faab_sets_dollars_left(roster):
    assert chat.apply_move({"action": "faab", "faab_remaining": 71}, NOW) == (True, "set FAAB left to $71")
    assert load(roster)["faab_remaining"] == 71
    assert not chat.apply_move({"action": "faab"}, NOW)[0]
    assert not chat.apply_move({"action": "trade", "player": "Burrow"}, NOW)[0]


def test_first_move_starts_from_the_seeded_roster(tmp_path, monkeypatch):
    path = tmp_path / "manual_roster.local.json"
    monkeypatch.setattr(manual, "MANUAL_ROSTER_LOCAL", path)
    seed_before = manual.MANUAL_ROSTER.read_text()
    assert chat.apply_move({"action": "drop", "player": "Kyler Murray"}, NOW)[0]
    data = load(path)
    assert "_note" not in data and not any(p["name"] == "Kyler Murray" for p in data["players"])
    assert manual.MANUAL_ROSTER.read_text() == seed_before  # the tracked seed is never edited


# --- Notes and history ---

def test_notes_get_ids_and_can_be_removed():
    assert chat.change_notes(["Keep $40 FAAB until week 10", "  "], [], NOW) == []
    assert chat.change_notes(["Start Bagent in week 6"], [], NOW) == []
    assert [(n["id"], n["text"]) for n in chat.dan_notes()] == [(1, "Keep $40 FAAB until week 10"), (2, "Start Bagent in week 6")]
    assert chat.change_notes([], [1, 9], NOW) == ["there was no note 9 to remove"]
    assert chat.change_notes(["New one"], [], NOW) == []
    assert [n["id"] for n in chat.dan_notes()] == [2, 3]  # ids are never reused
    assert chat.dan_notes()[0]["at"] == "2026-10-11"


def test_notes_are_capped(monkeypatch):
    monkeypatch.setattr(chat, "MAX_NOTES", 2)
    assert chat.change_notes(["a", "b", "c"], [], NOW) == ['Booth keeps at most 2 notes, so it didn\'t save "c"']


def test_reports_see_the_notes():
    from booth import mcp_server

    chat.change_notes(["Never start a kicker in the snow"], [], NOW)
    assert mcp_server.get_league_context()["dan_notes"][0]["text"] == "Never start a kicker in the snow"


# --- Answering ---

def reply_with(**out):
    base = {"reply": "On it.", "notes_add": [], "notes_remove": [], "roster_moves": [], "rerun": "none"}
    calls = []

    def claude(prompt):
        calls.append(prompt)
        return {"structured_output": {**base, **out}}
    return claude, calls


def msg(i, text, reply_to="", at="2026-10-11T16:59:00+00:00"):
    return {"id": i, "text": text, "reply_to": reply_to, "at": at}


def test_answer_carries_out_what_claude_decided(roster, monkeypatch):
    monkeypatch.setattr(chat, "_week", lambda now: 6)
    claude, prompts = reply_with(
        reply="Got it, Bagent is on your bench.\n\nI'll start him while Burrow is on bye.",
        notes_add=["Start Bagent in week 6"],
        roster_moves=[{"action": "add", "player": "Tyson Bagent", "position": "QB", "nfl_team": "CHI", "faab_spent": 0},
                      {"action": "drop", "player": "Nobody Here"}],
        rerun="sun",
    )
    reruns = []
    reply = chat.answer([msg(5, "I picked up Bagent, redo Sunday's check", reply_to="Claim Rodgers $8")], NOW,
                        claude=claude, rerun_fn=reruns.append)
    assert reply == ("Got it, Bagent is on your bench.\n\nI'll start him while Burrow is on bye.\n\n"
                     "(Booth couldn't do everything: couldn't find Nobody Here on the roster copy.)")
    assert reruns == ["sun"]
    assert [n["text"] for n in chat.dan_notes()] == ["Start Bagent in week 6"]
    assert load(roster)["players"][-1]["name"] == "Tyson Bagent"
    prompt = prompts[0]
    assert "[Replying to this earlier message:\nClaim Rodgers $8]\nI picked up Bagent" in prompt
    assert "NFL week 6. Now: Sunday Oct 11, 2026, 1:00 PM ET." in prompt
    assert "(this is the first message)" in prompt and "{" not in prompt.split("## Dan's notes")[1]
    turns = chat.history()
    assert [t["role"] for t in turns] == ["user", "assistant"] and turns[1]["text"] == reply

    claude, prompts = reply_with(reply="Sure.", notes_remove=[1])
    chat.answer([msg(6, "never mind the note")], NOW, claude=claude, rerun_fn=reruns.append)
    assert "[1] (2026-10-11) Start Bagent in week 6" in prompts[0]
    assert "Booth: Got it, Bagent is on your bench." in prompts[0]
    assert chat.dan_notes() == [] and reruns == ["sun"]


def test_messages_from_a_sleep_are_answered_together(monkeypatch):
    monkeypatch.setattr(chat, "_week", lambda now: 6)
    claude, prompts = reply_with()
    chat.answer([msg(1, "is Swift playing?", at="2026-10-11T13:05:00+00:00"),
                 msg(2, "also who's my flex", at="2026-10-11T13:40:00+00:00")], NOW, claude=claude)
    assert "(Sun 9:05 AM) is Swift playing?\n\n(Sun 9:40 AM) also who's my flex" in prompts[0]
    assert "sent this while the Mac was asleep" in prompts[0]
    claude, prompts = reply_with()
    chat.answer([msg(3, "thanks"), msg(4, "one more thing")], NOW, claude=claude)  # two quick texts: no wait
    assert "asleep" not in prompts[0]


def test_empty_answer_is_an_error():
    claude, _ = reply_with(reply="  ")
    with pytest.raises(chat.ChatError):
        chat.answer([msg(1, "hi")], NOW, claude=claude)
    assert chat.history() == []


class FakeSource:
    def __init__(self):
        self.acked, self.beats = [], 0

    def heartbeat(self):
        self.beats += 1

    def done(self, ids):
        self.acked += ids


def test_handle_always_answers_and_marks_done(monkeypatch):
    monkeypatch.setattr(chat, "_keep_typing", lambda stop, source: None)
    source, sent = FakeSource(), []
    assert chat.handle([msg(4, "hi")], source, send_fn=sent.append, answer_fn=lambda m: "Hello.")
    assert sent == ["Hello."] and source.acked == [4]

    def broken(messages):
        raise RuntimeError("Claude didn't finish within 10 minutes.")
    chat.handle([msg(5, "hi"), msg(6, "?")], source, send_fn=sent.append, answer_fn=broken)
    assert sent[-1] == "Booth hit a snag answering that (Claude didn't finish within 10 minutes.). Try again in a minute?"
    assert source.acked == [4, 5, 6]


def test_an_answer_that_cant_be_sent_waits_and_is_never_redone(monkeypatch):
    monkeypatch.setattr(chat, "_keep_typing", lambda stop, source: None)
    source, sent, answers = FakeSource(), [], []

    def answer(messages):
        answers.append([m["id"] for m in messages])
        return "Start Kraft."

    def offline(text):
        raise deliver.DeliveryError("Telegram unreachable")
    assert not chat.handle([msg(7, "flex?")], source, send_fn=offline, answer_fn=answer)
    assert source.acked == []  # not done until it's sent
    assert not chat.deliver_outbox(source, offline)
    # the mailbox hands the message over again: it's not answered a second time
    assert not chat.handle([msg(7, "flex?")], source, send_fn=offline, answer_fn=answer)
    assert answers == [[7]]
    assert chat.deliver_outbox(source, sent.append)
    assert sent == ["Start Kraft."] and source.acked == [7]
    assert chat.deliver_outbox(source, sent.append) and sent == ["Start Kraft."]


def test_a_lost_acknowledgement_never_means_a_second_answer(monkeypatch):
    monkeypatch.setattr(chat, "_keep_typing", lambda stop, source: None)
    answers, sent = [], []

    class Flaky(FakeSource):
        def done(self, ids):
            raise chat.ChatError("the mailbox returned HTTP 502")
    assert chat.handle([msg(8, "hi")], Flaky(), send_fn=sent.append, answer_fn=lambda m: answers.append(m) or "Hi.")
    source = FakeSource()
    assert chat.handle([msg(8, "hi"), msg(9, "and?")], source, send_fn=sent.append,
                       answer_fn=lambda m: answers.append(m) or "And.")
    assert [[m["id"] for m in a] for a in answers] == [[8], [9]]
    assert sent == ["Hi.", "And."] and source.acked == [8, 9]
    assert chat.handle([msg(8, "hi")], source, send_fn=sent.append, answer_fn=lambda m: answers.append(m) or "x")
    assert source.acked == [8, 9, 8] and len(answers) == 2


def test_typing_keeps_the_mailbox_told_the_mac_is_awake(monkeypatch):
    actions = []
    monkeypatch.setattr(deliver, "_telegram", lambda method, **kw: actions.append((method, kw["action"])))
    monkeypatch.setattr(deliver, "telegram_chat_id", lambda: 42)
    source = FakeSource()

    class Stop:
        def __init__(self):
            self.n = 0

        def wait(self, t):
            self.n += 1
            return self.n > 3
    chat._keep_typing(Stop(), source)
    assert actions == [("sendChatAction", "typing")] * 2 and source.beats == 2


# --- Where messages come from ---

@pytest.fixture
def tg(monkeypatch):
    monkeypatch.setattr(chat, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(deliver, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    calls = []
    replies = {}

    def fake_post(url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append((method, json))
        result = replies[method](json) if callable(replies.get(method)) else replies.get(method, True)
        return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": result})
    monkeypatch.setattr(deliver.requests, "post", fake_post)
    return calls, replies


def test_mailbox_found_from_the_webhook_and_rechecked(tg):
    calls, replies = tg
    replies["getWebhookInfo"] = {"url": "https://booth-mailbox.dan.workers.dev/telegram"}
    assert chat.mailbox_url(NOW) == "https://booth-mailbox.dan.workers.dev"
    replies["getWebhookInfo"] = {"url": ""}
    assert chat.mailbox_url(NOW) == "https://booth-mailbox.dan.workers.dev"  # remembered for a while
    assert len(calls) == 1
    assert chat.mailbox_url(NOW, refresh=True) == ""
    assert chat.mailbox_url(NOW.replace(hour=19)) == ""


def test_mailbox_url_override(monkeypatch):
    monkeypatch.setattr(chat, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOOTH_MAILBOX_URL", "http://127.0.0.1:8787/")
    assert chat.mailbox_url() == "http://127.0.0.1:8787"


def test_mailbox_source_polls_acks_and_signs(monkeypatch):
    monkeypatch.setattr(chat, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOOTH_MAILBOX_URL", "https://mb.example")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    seen = []

    def fake_request(method, url, json=None, params=None, timeout=None, headers=None):
        seen.append((method, url, json, params, headers["Authorization"]))
        body = {"messages": [{"updateId": 9, "chat": "42", "text": "hi", "replyTo": "", "receivedAt": "2026-10-11T16:00:00Z"}]}
        return mock.Mock(status_code=200, json=lambda: body if url.endswith("/poll") else {"ok": True})
    monkeypatch.setattr(chat.requests, "request", fake_request)
    source = chat.MailboxSource(42)
    assert source.fetch() == [{"id": 9, "text": "hi", "reply_to": "", "at": "2026-10-11T16:00:00Z"}]
    source.done([9])
    assert seen[0][:4] == ("GET", "https://mb.example/poll", None, {"chat": "42"})
    assert seen[1][:3] == ("POST", "https://mb.example/ack", {"ids": [9]})
    # the same secret the mailbox derives (worker/test/rules.test.ts pins the same value)
    assert seen[0][4] == "Bearer " + EXPECTED_API_SECRET

    monkeypatch.setattr(chat.requests, "request", lambda *a, **k: mock.Mock(status_code=403))
    with pytest.raises(chat.ChatError, match="HTTP 403"):
        source.fetch()


def test_telegram_source_answers_only_dans_chat(tg):
    calls, replies = tg
    replies["getUpdates"] = lambda params: [
        {"update_id": 10, "message": {"chat": {"id": 42, "type": "private"}, "text": "who's my flex?", "date": 1791738000,
                                      "reply_to_message": {"text": "Booth: Sunday final lineup pass"}}},
        {"update_id": 11, "message": {"chat": {"id": 7, "type": "private", "first_name": "Eve"}, "text": "/start abc"}},
        {"update_id": 12, "message": {"chat": {"id": 42, "type": "private"}, "sticker": {}}},
    ] if params["offset"] == 0 else []
    source = chat.TelegramSource(42)
    got = source.fetch()
    assert [(m["id"], m["text"], m["reply_to"]) for m in got] == [(10, "who's my flex?", "Booth: Sunday final lineup pass")]
    assert calls[0][1]["timeout"] == 25
    source.done([10])
    assert source.fetch() == [] and calls[-1][1]["offset"] == 11  # later updates are fetched again
    source.done([12])
    source.done([10])
    assert json.loads((chat.state_dir() / "telegram_offset.json").read_text()) == {"offset": 13}
    others = json.loads((chat.state_dir() / "telegram_others.json").read_text())
    assert others == [{"id": 7, "name": "Eve", "text": "/start abc"}]


def test_listen_switches_to_the_mailbox_once_the_webhook_is_set(tg, monkeypatch):
    calls, replies = tg
    monkeypatch.setattr(deliver, "telegram_chat_id", lambda: 42)
    monkeypatch.setattr(chat.time, "sleep", lambda s: None)
    replies["getWebhookInfo"] = {"url": ""}
    sources = []
    original = chat._source

    def spy(chat_id, refresh=False):
        s = original(chat_id, refresh)
        sources.append(type(s).__name__)
        return s
    monkeypatch.setattr(chat, "_source", spy)

    def updates(params):
        replies["getWebhookInfo"] = {"url": "https://booth-mailbox.dan.workers.dev/telegram"}
        raise_conflict()
    replies["getUpdates"] = updates

    def raise_conflict():
        raise deliver.DeliveryError("Telegram refused getUpdates: Conflict: can't use getUpdates method while webhook is active")
    monkeypatch.setattr(chat.requests, "request", lambda *a, **k: mock.Mock(status_code=200, json=lambda: {"messages": []}))
    chat.listen(max_rounds=3)
    assert sources == ["TelegramSource", "MailboxSource"]
    chats = iter([42, 77, 77])  # `telegram-setup --replace` picks a new chat while Booth listens
    monkeypatch.setattr(deliver, "telegram_chat_id", lambda: next(chats))
    polled = []
    monkeypatch.setattr(chat.requests, "request", lambda *a, **k: polled.append(k["params"]["chat"])
                        or mock.Mock(status_code=200, json=lambda: {"messages": []}))
    chat.listen(max_rounds=3)
    assert polled == ["42", "77", "77"]


def test_setup_finds_a_new_chat_through_the_mailbox(tg, monkeypatch, tmp_path):
    calls, replies = tg
    monkeypatch.setattr(deliver, "state_dir", lambda: tmp_path)
    replies["getMe"] = {"username": "dan_booth_bot"}

    def conflict(params):
        raise deliver.DeliveryError("Telegram refused getUpdates: Conflict: can't use getUpdates method while webhook is active")
    replies["getUpdates"] = conflict
    replies["getWebhookInfo"] = {"url": "https://booth-mailbox.dan.workers.dev/telegram"}
    heard = [{"id": "5", "name": "Eve", "text": "hi"}]
    monkeypatch.setattr(chat, "mailbox_chats", lambda: heard)
    ok, text = deliver.telegram_setup()
    assert not ok and "start=" in text
    heard.insert(0, {"id": "77", "name": "Dan", "text": f"/start {deliver._setup_code()}"})
    ok, text = deliver.telegram_setup()
    assert ok and deliver.telegram_chat_id() == 77


def test_run_claude_takes_chat_effort_and_needs_no_tools(monkeypatch):
    from booth import report

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        raise report.subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(report.subprocess, "run", fake_run)
    monkeypatch.delenv("BOOTH_EFFORT", raising=False)
    with pytest.raises(report.ReportError):
        report.run_claude("hi", effort="medium", require_tools=False)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "medium"
    init = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "yahoo-fantasy", "status": "connected"}]}
    result = {"type": "result", "structured_output": {"reply": "Thanks!"}}
    stream = "\n".join(json.dumps(e) for e in (init, result))
    assert report.parse_stream(stream, require_tools=False)["structured_output"] == {"reply": "Thanks!"}
    with pytest.raises(report.ReportError, match="without using any of Booth's data tools"):
        report.parse_stream(stream)


def test_pictures_and_files_are_described_not_dropped(tg):
    calls, replies = tg
    replies["getUpdates"] = lambda params: [
        {"update_id": 20, "message": {"chat": {"id": 42, "type": "private"}, "photo": [{}], "caption": "my roster"}},
        {"update_id": 21, "message": {"chat": {"id": 42, "type": "private"}, "document": {}}},
    ]
    got = chat.TelegramSource(42).fetch()
    assert [m["text"] for m in got] == ["[sent a photo with the caption: my roster]", "[sent a file]"]

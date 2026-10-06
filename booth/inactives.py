"""Inactives alert: a short message right after inactives come out, only when one of Dan's
recommended starters is ruled out.

Teams announce inactive players 90 minutes before kickoff. Every `booth run due` (each
30 minutes while the Mac is awake) checks whether a kickoff is 80 to 5 minutes away. If
any starter from the week's latest lineup report plays then and carries an injury
designation (in that report, or on the official injury report), Booth asks Claude to
check the inactives and pick a bench swap. It messages Dan only if someone is out, or if
by its last check before kickoff it still couldn't confirm a starter's status (silence
would read as all clear). Healthy starters aren't checked: surprise inactives are rare, and each check is a
Claude run. With the Mac asleep at that time, there's no check.
"""

from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv

from booth.config import ROOT
from booth.data import nflverse
from booth.data.names import normalize
from booth.deliver import notify, send
from booth.jobs import (
    EASTERN,
    LockBusy,
    _alert_once,
    _bookkeeping,
    _kickoff,
    _Lock,
    _log,
    _save_bookkeeping,
    _stay_awake,
    installed_at,
)
from booth.report import PROMPTS, locked_games_at, run_claude, space_sections
from booth.state import load_week

WINDOW_OPEN = timedelta(minutes=80)  # lists are out by kickoff minus 90 minutes
WINDOW_CLOSE = timedelta(minutes=5)  # after this a swap is unlikely to make it in
RUN_INTERVAL = timedelta(minutes=30)  # how often launchd runs `booth run due`
MAX_CHECKS = 2  # Claude runs per kickoff: one retry if the lists weren't out yet or the run failed
LINEUP_SOURCES = ("sun", "sat", "thu", "tue")  # newest lineup recommendation first
HEALTHY = {"", "healthy", "active", "none", "probable", "p", "ok", "-", "n/a", "full"}
NL = "\n\n"
PROMISE = "Booth will check again before kickoff."
NO_RECHECK = "Booth couldn't check again, so check the inactives yourself."
# Yahoo/other spellings -> nflverse team codes
TEAM_ALIASES = {"LAR": "LA", "STL": "LA", "JAC": "JAX", "WSH": "WAS", "GNB": "GB", "KAN": "KC", "NOR": "NO",
                "NWE": "NE", "SFO": "SF", "TAM": "TB", "LVR": "LV", "OAK": "LV", "SD": "LAC", "SDG": "LAC"}

SCHEMA = {
    "type": "object",
    "properties": {
        "players": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "slot": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "inactive", "unknown"]},
                    "source": {"type": "string"},
                },
                "required": ["name", "slot", "status"],
            },
        },
        "message": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["players", "message"],
}


def _team(code: str | None) -> str:
    code = (code or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def _risky(status: str | None) -> bool:
    s = (status or "").strip().lower()
    return s not in HEALTHY and s != "bye"


def kickoffs(week: int, games: list[dict]) -> list[datetime]:
    return sorted({_kickoff(g) for g in games if int(g["week"]) == week})


def due_kickoffs(now: datetime, week: int, games: list[dict]) -> list[datetime]:
    """Kickoffs whose inactives window (80 to 5 minutes before) contains `now`. Windows
    overlap (4:05 and 4:25 PM), so there can be two."""
    return [k for k in kickoffs(week, games) if k - WINDOW_OPEN <= now < k - WINDOW_CLOSE]


def latest_lineup(season: int, week: int) -> tuple[str, dict] | None:
    runs = load_week(season, week)["runs"]
    for run in LINEUP_SOURCES:
        lineup = (runs.get(run) or {}).get("lineup")
        if lineup:
            return run, lineup
    return None


def _official(week: int, names: list[str]) -> dict[str, dict]:
    """name -> {"status", "team"} from the official injury report; empty if it can't be read."""
    try:
        report = nflverse.injury_report(week=week, names=names)
    except Exception as exc:  # the report's own designations still count
        _log(f"inactives: couldn't read the injury report: {exc}")
        return {}
    return {normalize(e["name"]): {"status": e.get("game_status") or "", "team": e.get("team") or ""}
            for e in report.get("entries", [])}


def _roster_teams() -> dict[str, str]:
    try:
        from booth.manual import manual_roster

        return {normalize(p["name"]): p.get("nfl_team", "") for p in manual_roster()["players"]}
    except Exception:
        return {}


def at_risk(lineup: dict, teams: set[str], official: dict[str, dict], roster_teams: dict[str, str]) -> list[dict]:
    """Starters playing for one of `teams` with an injury designation (in the lineup report or
    on the official injury report)."""
    out = []
    for slot, p in lineup.items():
        if not p or not p.get("name") or slot.upper().startswith("DEF"):
            continue
        key = normalize(p["name"])
        listed = official.get(key) or {}
        team = _team(p.get("nfl_team") or roster_teams.get(key) or listed.get("team"))
        if team not in teams:
            continue
        status = p.get("status") or ""
        if _risky(status) or _risky(listed.get("status")):
            out.append({"slot": slot, "name": p["name"], "team": team, "status": listed.get("status") or status})
    return out


def build_prompt(week: int, now: datetime, kickoff: datetime, players: list[dict], games: list[dict]) -> str:
    fields = {
        "week": week,
        "time": now.astimezone(EASTERN).strftime("%-I:%M %p"),
        "weekday": now.astimezone(EASTERN).strftime("%A"),
        "kickoff": kickoff.strftime("%-I:%M %p"),
        "players": "\n".join(f"- {p['name']} ({p['slot']}, {p['team']}): {p['status']}" for p in players),
        "locked_games": locked_games_at(week, games, now),
    }
    text = (PROMPTS / "inactives.md").read_text()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def check(week: int, now: datetime, kickoff: datetime, players: list[dict], games: list[dict]) -> dict:
    prompt = build_prompt(week, now, kickoff, players, games)
    return run_claude(prompt, timeout=600, schema=SCHEMA)["structured_output"]


def _verdicts(players: list[dict], said: list[dict]) -> dict[str, str | None]:
    """Each starter's status as Claude reported it (None if no entry matched): by name, then a
    looser name match, then slot. A slot counts only when exactly one entry carries it and that
    entry isn't another starter by name, so a bench swap listed in the same slot can't stand in
    for the starter. "Hollywood Brown" for "Marquise Brown" won't match."""
    by_name = {normalize(p.get("name") or ""): p.get("status") for p in said}
    starters = {normalize(r["name"]) for r in players}
    by_slot: dict[str, list] = {}
    for p in said:
        if p.get("slot") and normalize(p.get("name") or "") not in starters:
            by_slot.setdefault(p["slot"].upper(), []).append(p.get("status"))
    out = {}
    for r in players:
        key = normalize(r["name"])
        status = by_name.get(key)
        if status is None:
            status = next((s for n, s in by_name.items() if n and key and (key in n or n in key)), None)
        if status is None:
            hits = by_slot.get(r["slot"].upper(), [])
            status = hits[0] if len(hits) == 1 else None
        out[r["name"]] = status
    return out


def _names(players: list[dict]) -> str:
    return ", ".join(f"{p['name']} ({p['status']})" for p in players)


def run_inactives(now: datetime | None = None, games: list[dict] | None = None, check_fn=check, send_fn=send,
                  notify_fn=notify, clock=None) -> str:
    """Check inactives for each kickoff coming up, if any. Returns a one-line outcome."""
    load_dotenv(ROOT / ".env")
    if clock is None:
        clock = (lambda: now) if now else (lambda: datetime.now(EASTERN))
    now = now or clock()
    try:
        games = games or nflverse.games()
    except Exception as exc:
        return f"inactives: couldn't load the NFL schedule: {exc}"
    week = nflverse.current_week(games, now.date())
    due = due_kickoffs(now, week, games)
    if not due:
        return "inactives: nothing due"
    since = installed_at()
    outcomes = []
    for i, kickoff in enumerate(due):
        if since is None or kickoff - WINDOW_OPEN < since:
            outcomes.append(f"inactives {kickoff:%a %-I:%M %p}: before the schedule was installed")
            continue
        at = now if i == 0 else clock()  # an earlier kickoff's check takes minutes
        outcomes.append(_run_kickoff(week, kickoff, at, games, check_fn, send_fn, notify_fn, clock))
    return "; ".join(outcomes)


def _run_kickoff(week, kickoff, now, games, check_fn, send_fn, notify_fn, clock) -> str:
    """One kickoff's check. Bookkeeping in the week file under jobs["inactives"][kickoff]:
    checks so far, the starters at risk, "pending" (still unconfirmed, to check again),
    and "message" (built but not sent yet)."""
    label = f"{kickoff:%a %-I:%M %p}"
    season = nflverse.SEASON
    jobs: dict = {}
    job: dict = {}
    risky: list[dict] = []

    def deliver() -> None:
        if clock() >= kickoff:
            job.pop("message", None)
            job.update(done=True, result="kicked off before it could be sent")
        else:
            send_fn(job["message"])
            job.setdefault("sent", []).append(clock().isoformat())
            job["result"] = job.pop("sent_result", "sent")
            job.pop("message")
            job["done"] = not job.get("pending")
        _save_bookkeeping(season, week, jobs)

    try:
        with _Lock():
            jobs = _bookkeeping(season, week)
            job = jobs.setdefault("inactives", {}).setdefault(kickoff.isoformat(), {})
            if job.get("done"):
                return f"inactives {label}: {job.get('result', 'done')}"
            check_now = True
            if job.get("message"):  # an earlier run built it but couldn't send it
                deliver()
                # that send took the run a promised recheck was counting on: if no later run
                # falls before the window closes, recheck now
                check_now = not job["done"] and now + RUN_INTERVAL >= kickoff - WINDOW_CLOSE
                now = clock()
            if check_now:
                if job.get("checks", 0) >= MAX_CHECKS:
                    job.update(done=True, result=f"gave up after {MAX_CHECKS} checks")
                    _save_bookkeeping(season, week, jobs)
                    return f"inactives {label}: {job['result']}"
                if job.get("pending"):
                    to_check = job["pending"]
                else:
                    found = latest_lineup(season, week)
                    if not found:
                        job.update(done=True, result="no lineup report this week")
                        _save_bookkeeping(season, week, jobs)
                        return f"inactives {label}: {job['result']}"
                    _, lineup = found
                    teams = {t for g in games if int(g["week"]) == week and _kickoff(g) == kickoff
                             for t in (g["home_team"], g["away_team"])}
                    names = [p["name"] for p in lineup.values() if p and p.get("name")]
                    risky = at_risk(lineup, teams, _official(week, names), _roster_teams())
                    if not risky:
                        job.update(done=True, result="no starters with an injury designation")
                        _save_bookkeeping(season, week, jobs)
                        return f"inactives {label}: {job['result']}"
                    job["players"] = to_check = risky
                _stay_awake()
                job["checks"] = job.get("checks", 0) + 1
                _save_bookkeeping(season, week, jobs)
                result = check_fn(week, now, kickoff, to_check, games)
                said = result.get("players") or []
                job.setdefault("results", []).append(said)
                verdict = _verdicts(to_check, said)
                out = [p for p in to_check if verdict[p["name"]] == "inactive"]
                active = [p for p in to_check if verdict[p["name"]] == "active"]
                unknown = [p for p in to_check if verdict[p["name"]] not in ("active", "inactive")]
                unmatched = [p for p in to_check if verdict[p["name"]] is None]
                message = (result.get("message") or "").strip()
                if message and not out and unmatched:  # a swap was written but its names didn't line up: trust it
                    out = unmatched
                    unknown = [p for p in unknown if p not in out]
                if out and not message:  # never sit on an inactive starter for want of wording
                    message = (f"Lineup change before {kickoff:%-I:%M %p} ET:\n"
                               f"{', '.join(p['name'] for p in out)} inactive. Swap in a healthy bench player.")
                retry = bool(unknown) and job["checks"] < MAX_CHECKS and now + RUN_INTERVAL < kickoff - WINDOW_CLOSE
                parts = [message] if out else []  # a note with nobody out isn't an alert
                if job.get("promised") and active:  # Dan was told these were still being checked
                    parts.append(f"Now confirmed active: {_names(active)}. No change needed.")
                if unknown and not retry:  # silence would read as all clear
                    parts.append(f"Couldn't confirm before the {kickoff:%-I:%M %p} ET kickoff: {_names(unknown)}. "
                                 "Check the inactives yourself.")
                elif unknown and parts:
                    parts.append(f"Not announced yet: {_names(unknown)}. {PROMISE}")
                    job["promised"] = True
                job["pending"] = unknown if retry else []
                if not parts:
                    job["done"] = not retry
                    job["result"] = "not announced yet, will check again" if retry else "all active"
                    _save_bookkeeping(season, week, jobs)
                    return f"inactives {label}: {job['result']}"
                text = f"Booth: inactives alert, week {week}\n\n{space_sections(NL.join(parts))}"
                if os.getenv("BOOTH_DRY_RUN") == "1":
                    text = f"[DRY RUN] {text}"
                job["message"] = text
                bits = []
                if out:
                    bits.append(f"{', '.join(p['name'] for p in out)} inactive")
                if unknown:
                    bits.append("rest pending" if retry else f"couldn't confirm {', '.join(p['name'] for p in unknown)}")
                job["sent_result"] = "sent: " + ("; ".join(bits) or "follow-up")
                _save_bookkeeping(season, week, jobs)
                deliver()
    except LockBusy:
        return "inactives: skipped, another run in progress"
    except Exception as exc:
        _log(f"FAILED inactives {label}: {exc}\n{traceback.format_exc()}")
        job["last_error"] = str(exc)[:500]
        if clock() >= kickoff:  # too late to help, and nothing goes out after kickoff
            job.pop("message", None)
            job.update(done=True, result="kicked off before the check finished")
        elif now + RUN_INTERVAL >= kickoff - WINDOW_CLOSE or (
                not job.get("message") and job.get("checks", 0) >= MAX_CHECKS):  # last chance
            who = _names(job.get("pending") or job.get("players") or risky) or "your starters"
            text = (job.get("message") or "").replace(PROMISE, NO_RECHECK) or (
                f"Booth couldn't check inactives for the {kickoff:%-I:%M %p} ET games: {who}. "
                "Check them yourself before kickoff.")
            _alert_once(f"inactives-{kickoff.isoformat()}", text, send_fn, notify_fn)
        if jobs:
            try:
                _save_bookkeeping(season, week, jobs)
            except Exception as save_exc:
                _log(f"couldn't save bookkeeping: {save_exc}")
        return f"failed inactives {label}: {exc}"
    _log(f"inactives {label}: {job['result']}")
    return f"inactives {label}: {job['result']}"

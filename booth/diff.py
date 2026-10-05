"""Structured diff between two report snapshots.

Only material fields count: who starts, injury status, waiver recs and bids,
weather flags, and projection swings past a threshold. Reworded reasoning with
the same recommendations is not a change.
"""

from __future__ import annotations

from booth.data.names import normalize

PROJECTION_SWING = 3.0


def _starters(snap: dict) -> dict[str, dict]:
    return {normalize(p["name"]): p for p in (snap.get("lineup") or {}).values() if p and p.get("name")}


def _statuses(snap: dict) -> dict[str, tuple[str, str]]:
    out = {k: (p["name"], (p.get("status") or "healthy")) for k, p in _starters(snap).items()}
    for f in snap.get("bench_flags") or []:
        if f.get("name") and f.get("status"):
            out.setdefault(normalize(f["name"]), (f["name"], f["status"]))
    return out


def _norm_status(s: str) -> str:
    s = (s or "").strip().lower()
    return "healthy" if s in ("", "active", "healthy", "none") else s


def diff_snapshots(prev: dict | None, cur: dict) -> list[str]:
    if not prev:
        return []
    changes: list[str] = []

    old_s, new_s = _starters(prev), _starters(cur)
    ins = [new_s[k]["name"] for k in new_s if k not in old_s]
    outs = [old_s[k]["name"] for k in old_s if k not in new_s]
    if ins or outs:
        changes.append("Lineup: " + "; ".join(
            x for x in (f"start {', '.join(ins)}" if ins else "", f"bench {', '.join(outs)}" if outs else "") if x
        ))

    old_st, new_st = _statuses(prev), _statuses(cur)
    for k, (name, status) in new_st.items():
        if k in old_st and _norm_status(old_st[k][1]) != _norm_status(status):
            changes.append(f"Injury: {name} {old_st[k][1]} → {status}")

    for k in new_s.keys() & old_s.keys():
        a, b = old_s[k].get("projection"), new_s[k].get("projection")
        if a is not None and b is not None and abs(b - a) >= PROJECTION_SWING:
            changes.append(f"Projection: {new_s[k]['name']} {a:g} → {b:g}")

    def recs(s):
        return {(normalize(r.get("add", "")), normalize(r.get("drop") or "")): r for r in s.get("waiver_recs") or []}

    old_r, new_r = recs(prev), recs(cur)
    for k, r in new_r.items():
        if k not in old_r:
            changes.append(f"New waiver rec: add {r['add']}" + (f", drop {r['drop']}" if r.get("drop") else ""))
        elif old_r[k].get("faab_bid") != r.get("faab_bid"):
            changes.append(f"FAAB bid on {r['add']}: ${old_r[k].get('faab_bid')} → ${r.get('faab_bid')}")
    for k, r in old_r.items():
        if k not in new_r:
            changes.append(f"Dropped waiver rec: {r['add']}")

    def wx(s):
        return {(f.get("game") or "").upper(): (f.get("note") or "") for f in s.get("weather_flags") or []}

    old_w, new_w = wx(prev), wx(cur)
    for g in new_w.keys() - old_w.keys():
        changes.append(f"Weather: {g} {new_w[g]}")
    for g in old_w.keys() - new_w.keys():
        changes.append(f"Weather cleared: {g}")

    return changes


def change_header(changes: list[str]) -> str:
    if not changes:
        return "No changes. Lineup stands."
    return "Changes since last report:\n" + "\n".join(f"- {c}" for c in changes)

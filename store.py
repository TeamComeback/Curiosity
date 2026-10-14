"""All persistent state lives in data/*.json, committed back to the repo after each run."""
import copy
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")


def now_et():
    return datetime.now(ET)


def today():
    return now_et().strftime("%Y-%m-%d")


def _path(name):
    return os.path.join(config.DATA_DIR, name)


def load(name, default):
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(default)


def save(name, obj):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, _path(name))


def load_state():
    return load("state.json", {})


def init_state():
    bal = config.RACE["start_balance"]
    return {
        "created": now_et().isoformat(timespec="seconds"),
        "race": {"status": "running", "goal": config.RACE["goal"], "winner": None,
                 "finished_at": None, "final_standings": None},
        "entries": {"date": today(), "counts": {}},
        "agents": {aid: {"cash": bal, "reserved": 0.0, "positions": {}, "status": "active",
                         "status_reason": "", "status_at": None, "closed": 0, "wins": 0,
                         "peak": bal, "max_dd": 0.0, "notes": [], "last_exits": {}}
                   for aid in config.AGENTS},
    }


def add_missing_agents(state):
    """Agents added mid-race (like the monkey) join with the standard starting balance."""
    bal = config.RACE["start_balance"]
    for aid in config.AGENTS:
        if aid not in state["agents"]:
            state["agents"][aid] = {"cash": bal, "reserved": 0.0, "positions": {}, "status": "active",
                                    "status_reason": "", "status_at": None, "closed": 0, "wins": 0,
                                    "peak": bal, "max_dd": 0.0, "notes": [], "last_exits": {},
                                    "joined": now_et().isoformat(timespec="seconds")}


def load_params():
    p = load("params.json", {})
    merged = copy.deepcopy(config.DEFAULT_PARAMS)
    for sid, vals in p.items():
        for k, v in vals.items():
            if sid in merged and k in merged[sid]:
                lo, hi = config.PARAM_BOUNDS[sid][k]
                merged[sid][k] = type(merged[sid][k])(min(max(v, lo), hi))
    return merged


def ledger(event):
    """Permanent record of every decision (trades, plans, focus changes). Never trimmed below 20,000 entries."""
    lg = load("ledger.json", [])
    lg.append({"time": now_et().isoformat(timespec="seconds"), **event})
    save("ledger.json", lg[-20000:])


RUN_EVENTS = []          # events from this run, for the phone alert summary


def journal(event):
    j = load("journal.json", [])
    event = {"time": now_et().isoformat(timespec="seconds"), **event}
    j.append(event)
    RUN_EVENTS.append(event)
    save("journal.json", j[-800:])
    print(f"[journal] {event.get('type')} {event.get('sleeve', '')} {event.get('symbol', '')} {event.get('note', '')}")


# ---------------- settlement (cash account, T+1) ----------------
def next_trading_day(day):
    """The next market day after `day` (YYYY-MM-DD), from the saved Alpaca calendar, else next weekday."""
    from datetime import date, timedelta
    later = sorted(c["date"] for c in load("calendar.json", []) if c.get("date", "") > day)
    if later:
        return later[0]
    d = date.fromisoformat(day) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def add_unsettled(agent, amount, note=""):
    """Sale proceeds (and credits received) settle on the next trading day."""
    if amount <= 0.004:
        return
    agent.setdefault("unsettled", []).append({"amount": round(amount, 2), "settles": next_trading_day(today()), "note": note[:40]})


def unsettled(agent):
    """Cash that hasn't settled yet. Settled entries are dropped as their day arrives."""
    day = today()
    rows = [r for r in agent.get("unsettled", []) if r["settles"] > day]
    agent["unsettled"] = rows
    return round(sum(r["amount"] for r in rows), 2)

"""Weekly self-review (Fridays after the close). Each competing agent reviews its own
week and may retune up to 2 of its own strategy parameters, each by at most 25% of the
allowed range. Usage: python -m agent.review"""
from datetime import timedelta

from . import brain, config, store

MAX_STEP = 0.25
MAX_CHANGES = 2


def agent_stats(aid, agent, journal, curve):
    closes = [j for j in journal if j.get("type") == "close" and j.get("agent") == aid]
    week_ago = (store.now_et().date() - timedelta(days=7)).isoformat()
    week = [j for j in closes if j["time"][:10] >= week_ago]
    wins = [j["pnl"] for j in closes if j["pnl"] > 0]
    losses = [j["pnl"] for j in closes if j["pnl"] <= 0]
    series = [r["agents"].get(aid) for r in curve if r.get("agents", {}).get(aid) is not None]
    by_conf = {}
    for j in closes:
        by_conf.setdefault(j.get("confidence") or 0, []).append(j["pnl"])
    return {
        "equity_now": series[-1] if series else agent["cash"],
        "equity_week_ago": next((r["agents"].get(aid) for r in curve if r["date"] >= week_ago), None),
        "closed_trades": len(closes), "closed_this_week": len(week),
        "week_pnl": round(sum(j["pnl"] for j in week), 2),
        "win_rate": round(100 * len(wins) / len(closes), 1) if closes else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown_pct": round(agent.get("max_dd", 0) * 100, 1),
        "avg_pnl_by_confidence": {k: round(sum(v) / len(v), 2) for k, v in sorted(by_conf.items())},
        "this_week_trades": [{k: j.get(k) for k in ("symbol", "pnl", "note", "thesis", "confidence")} for j in week],
    }


def apply_changes(aid, changes):
    current = store.load("params.json", {})
    params = store.load_params()[aid]
    applied = []
    for ch in (changes or [])[:MAX_CHANGES]:
        key = ch.get("param")
        if key not in config.PARAM_BOUNDS[aid] or any(a["param"] == key for a in applied):
            continue
        lo, hi = config.PARAM_BOUNDS[aid][key]
        old = params[key]
        try:
            new = float(ch["new"])
        except (TypeError, ValueError, KeyError):
            continue
        step = MAX_STEP * (hi - lo)
        new = min(max(new, old - step, lo), old + step, hi)
        new = type(config.DEFAULT_PARAMS[aid][key])(round(new, 3))
        if new == old:
            continue
        current.setdefault(aid, {})[key] = new
        params[key] = new
        applied.append({"agent": aid, "param": key, "old": old, "new": new, "reason": str(ch.get("reason", ""))[:200]})
    store.save("params.json", current)
    return applied


def main():
    state = store.load_state()
    if not state or state["race"]["status"] != "running":
        print("No running race.")
        return
    journal = store.load("journal.json", [])
    curve = store.load("equity.json", [])
    entry = {"date": store.today(), "agents": {}}
    for aid, agent in state["agents"].items():
        if not config.AGENTS[aid]["competing"] or agent["status"] != "active":
            continue
        stats = agent_stats(aid, agent, journal, curve)
        res = brain.weekly_review(aid, stats, store.load_params()[aid], config.PARAM_BOUNDS[aid], agent["notes"])
        applied = apply_changes(aid, res.get("changes"))
        if res.get("note"):
            agent["notes"].append({"date": store.today(), "note": "[weekly] " + str(res["note"])[:300]})
        for a in applied:
            store.journal({"type": "param_change", "agent": aid,
                           "note": f"{a['param']}: {a['old']} -> {a['new']} ({a['reason']})"})
        entry["agents"][aid] = {"summary": res.get("summary", ""), "applied": applied, "stats": stats}
    reviews = store.load("reviews.json", [])
    reviews.append(entry)
    store.save("reviews.json", reviews[-52:])
    store.save("state.json", state)
    dash = store.load("dashboard.json", {})
    if dash:
        dash["reviews"] = reviews[-6:][::-1]
        dash["params"] = store.load_params()
        store.save("dashboard.json", dash)
    print("Weekly review saved.")


if __name__ == "__main__":
    main()

"""Daily recap, written after the close (and on demand): what each firm did today, the day's best
and worst trades, the smartest and most efficient calls, and a pace projection to the goal.

Numbers come straight from the books (ledger, equity history, open positions). The AI writes the
headline and one line per firm, and judges the "smartest" trade; without the AI, simple rules fill in.
Saved to data/recaps.json (last 60 days). Usage: python recap.py [YYYY-MM-DD]"""
import json
import math
import sys
from datetime import datetime

import brain
import config
import store

MIN_EFFICIENCY_MINUTES = 5


def _minutes(a, b):
    try:
        return max((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60, 0)
    except (TypeError, ValueError):
        return None


def _trade(e, opens):
    o = opens.get(e.get("position_id"), {})
    mins = _minutes(e.get("opened"), e.get("time"))
    return {"agent": e["agent"], "symbol": e.get("symbol"), "underlying": e.get("underlying"),
            "instrument": e.get("instrument"), "pnl": e.get("pnl") or 0, "return_pct": e.get("return_pct"),
            "opened": e.get("opened"), "closed": e.get("time"), "minutes_held": round(mins) if mins is not None else None,
            "why_entered": (e.get("why_entered") or "")[:220], "why_exited": (e.get("why_exited") or "")[:160],
            "from_plan": bool(o.get("from_plan")), "confidence": o.get("confidence"), "position_id": e.get("position_id")}


def build(day=None):
    day = day or store.today()
    state = store.load_state()
    ledger = store.load("ledger.json", [])
    curve = store.load("equity.json", [])
    opens = {e.get("position_id"): e for e in ledger if e.get("type") == "trade_opened"}
    closed = [_trade(e, opens) for e in ledger if e.get("type") == "trade_closed" and not e.get("partial")
              and e.get("time", "")[:10] == day]
    opened = [e for e in ledger if e.get("type") == "trade_opened" and e.get("time", "")[:10] == day]

    rows = [r for r in curve if r["date"] <= day]
    today_eq = rows[-1]["agents"] if rows and rows[-1]["date"] == day else {}
    prev_eq = rows[-2]["agents"] if len(rows) >= 2 and rows[-1]["date"] == day else (rows[-1]["agents"] if rows else {})
    start = config.RACE["start_balance"]
    goal = config.RACE["goal"]
    trading_days = len([r for r in curve if r["date"] <= day])

    firms = {}
    for aid, a in (state.get("agents") or {}).items():
        eq = today_eq.get(aid)
        prev = prev_eq.get(aid, start)
        mine = [t for t in closed if t["agent"] == aid]
        firm = {"name": config.AGENTS[aid]["name"], "racing": config.AGENTS[aid]["competing"],
                "exhibition": bool(config.AGENTS[aid].get("exhibition")),
                "equity": eq, "day_pnl": round(eq - prev, 2) if eq is not None else None,
                "day_pct": round((eq / prev - 1) * 100, 2) if eq and prev else None,
                "total_pct": round((eq / start - 1) * 100, 2) if eq else None,
                "opened": sum(1 for e in opened if e.get("agent") == aid), "closed": len(mine),
                "wins": sum(1 for t in mine if t["pnl"] > 0), "realized": round(sum(t["pnl"] for t in mine), 2),
                "open_positions": len(a.get("positions", {}))}
        # pace projection: average daily growth so far, compounded to the goal (naive, for perspective only)
        days = max(trading_days, 1)
        if eq and eq > 0:
            g = (eq / start) ** (1 / days) - 1
            firm["avg_daily_pct"] = round(g * 100, 3)
            firm["days_to_goal"] = math.ceil(math.log(goal / eq) / math.log(1 + g)) if g > 0.0001 and eq < goal else None
            firm["projection_30d"] = round(eq * (1 + g) ** 21, 2)       # about one month of trading days
        firms[aid] = firm

    winners = sorted([t for t in closed if t["pnl"] > 0], key=lambda t: -t["pnl"])
    losers = sorted([t for t in closed if t["pnl"] < 0], key=lambda t: t["pnl"])
    eff = [dict(t, pct_per_hour=round((t["return_pct"] or 0) / (t["minutes_held"] / 60), 2))
           for t in closed if t["pnl"] > 0 and t["return_pct"] is not None and (t["minutes_held"] or 0) >= MIN_EFFICIENCY_MINUTES]
    eff.sort(key=lambda t: -t["pct_per_hour"])
    smart_pool = [t for t in winners if t["from_plan"] or (t["confidence"] or 0) >= 4] or winners
    smart = max(smart_pool, key=lambda t: t["return_pct"] or 0) if smart_pool else None

    recap = {"date": day, "made_at": store.now_et().isoformat(timespec="minutes"),
             "totals": {"opened": len(opened), "closed": len(closed), "wins": len(winners), "losses": len(losers),
                        "realized": round(sum(t["pnl"] for t in closed), 2)},
             "firms": firms, "biggest_wins": winners[:3], "biggest_losses": losers[:3],
             "most_efficient": eff[:3], "smartest": smart, "smartest_why": None,
             "radar": {k: (store.load("radar.json", {}).get("now") or {}).get(k) for k in ("score", "tier")}}
    narrate(recap, closed)
    return recap


SYSTEM = """You write the end-of-day recap for a paper-trading race between AI trading firms (each started with
${start:,.0f}; first to ${goal:,.0f} wins). Be concrete and brief, like a trading-desk wrap: plain English, numbers
where they matter, no hype. Reply with ONLY JSON:
{{"headline":"one or two sentences on the day",
 "firms":{{"<firm id>":"one sentence on what that firm did and how it went"}},
 "smartest":{{"position_id":"<id of the closed trade you judge the smartest call today, or empty>","why":"one sentence"}},
 "tomorrow":"one sentence on what to watch tomorrow"}}"""


def narrate(recap, closed):
    if not config.ANTHROPIC_API_KEY:
        recap["headline"] = _rules_headline(recap)
        return
    view = {"date": recap["date"], "totals": recap["totals"], "firms": recap["firms"], "radar": recap["radar"],
            "closed_trades": closed[:60]}
    try:
        d = brain._call(config.REVIEW_MODEL, SYSTEM.format(start=config.RACE["start_balance"], goal=config.RACE["goal"]),
                        json.dumps(view, default=str), 1500, 120)
    except Exception as e:  # noqa: BLE001 - the numbers still stand without the write-up
        print(f"[recap] write-up failed: {type(e).__name__}")
        recap["headline"] = _rules_headline(recap)
        return
    recap["headline"] = str(d.get("headline", ""))[:400] or _rules_headline(recap)
    recap["tomorrow"] = str(d.get("tomorrow", ""))[:300]
    for aid, line in (d.get("firms") or {}).items():
        if aid in recap["firms"]:
            recap["firms"][aid]["summary"] = str(line)[:240]
    pick = (d.get("smartest") or {})
    chosen = next((t for t in closed if t.get("position_id") and t["position_id"] == pick.get("position_id")), None)
    if chosen:
        recap["smartest"], recap["smartest_why"] = chosen, str(pick.get("why", ""))[:240]


def _rules_headline(recap):
    t = recap["totals"]
    racing = {k: v for k, v in recap["firms"].items() if v["racing"] and v["day_pnl"] is not None}
    best = max(racing.items(), key=lambda kv: kv[1]["day_pnl"], default=(None, None))
    return (f"{t['opened']} trades opened and {t['closed']} closed ({t['wins']} winners), realized "
            f"{'+' if t['realized'] >= 0 else '-'}${abs(t['realized']):.2f}."
            + (f" Best day among the racers: {best[1]['name']} ({best[1]['day_pnl']:+.2f})." if best[0] else ""))


def main(day=None):
    recap = build(day)
    allr = [r for r in store.load("recaps.json", []) if r["date"] != recap["date"]] + [recap]
    store.save("recaps.json", sorted(allr, key=lambda r: r["date"])[-60:])
    dash = store.load("dashboard.json", {})
    if dash:
        dash["recap_updated"] = recap["made_at"]
        store.save("dashboard.json", dash)
    store.journal({"type": "recap", "note": recap.get("headline", "")})
    print("Recap saved:", recap.get("headline"))


if __name__ == "__main__":
    import alerts
    main(sys.argv[1] if len(sys.argv) > 1 else None)
    alerts.flush("Agent race: daily recap")

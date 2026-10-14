"""Maintenance commands that change the race's books outside normal trading.

  python admin.py topup 400     add $400 cash to every agent (open positions untouched)
  python admin.py universe      rebuild the stock universe now (needs Alpaca keys)
  python admin.py radar         compute and save the Crash Radar now (no trading)
  python admin.py alert         send a test phone alert (checks the NTFY_TOPIC secret)
  python admin.py backfill      seed the permanent ledger from the journal (one time)

A top-up is recorded as if every agent had started with the bigger balance: past equity
points, peaks and drawdowns are shifted by the same amount, so charts stay continuous and
returns are measured against the new starting balance."""
import sys

import config
import store
import run


def topup(amount):
    state = store.load_state()
    if not state:
        raise SystemExit("No race state yet.")
    store.add_missing_agents(state)
    for aid, a in state["agents"].items():
        if a.get("joined") and a["closed"] == 0 and not a["positions"]:
            a["cash"] = config.RACE["start_balance"]      # brand-new agents simply start at the new balance
            a["peak"] = a["cash"]
            continue
        old_peak = a.get("peak") or config.RACE["start_balance"] - amount
        dd_dollars = a.get("max_dd", 0) * old_peak
        a["cash"] = round(a["cash"] + amount, 2)
        a["peak"] = round(old_peak + amount, 2)
        a["max_dd"] = round(dd_dollars / a["peak"], 4) if a["peak"] else 0
    curve = store.load("equity.json", [])
    for row in curve:
        for aid in list(row.get("agents", {})):
            row["agents"][aid] = round(row["agents"][aid] + amount, 2)
    store.save("equity.json", curve)
    store.journal({"type": "topup", "note": f"Every firm's starting balance raised to ${config.RACE['start_balance']:,.0f} "
                                            f"(+${amount:,.0f} cash each; open positions unchanged)."})

    # rebuild the dashboard from the books, valuing positions at their last known prices
    prices = {}
    for a in state["agents"].values():
        for p in a["positions"].values():
            if p["kind"] == "stock" and p.get("last"):
                prices[p["symbol"]] = p["last"]
    equities = {aid: run.agent_equity(a, prices) for aid, a in state["agents"].items()}
    regime = store.load("dashboard.json", {}).get("regime", {})
    run.write_dashboard(state, regime, equities)
    store.save("state.json", state)
    print("Top-up done:", {aid: round(e, 2) for aid, e in equities.items()})


def backfill():
    if store.load("ledger.json", []):
        print("Ledger already has entries; skipping.")
        return
    import universe
    rows = []
    for j in store.load("journal.json", []):
        t, aid = j.get("type"), j.get("agent")
        if t == "open":
            rows.append({"time": j["time"], "type": "trade_opened", "agent": aid, "symbol": j.get("symbol"),
                         "sector": universe.sector_of(j.get("symbol")), "instrument": j.get("instrument"),
                         "qty": j.get("qty"), "price": j.get("price"), "cost": j.get("cost"), "stop": j.get("stop"),
                         "why": j.get("thesis"), "confidence": j.get("confidence")})
        elif t == "close":
            rows.append({"time": j["time"], "type": "trade_closed", "agent": aid, "symbol": j.get("symbol"),
                         "sector": universe.sector_of(j.get("symbol")), "entry": j.get("entry"), "exit": j.get("exit"),
                         "pnl": j.get("pnl"), "opened": j.get("opened"), "why_entered": j.get("thesis"),
                         "why_exited": j.get("note")})
        elif t == "plan":
            note = j.get("note", "")
            sess, _, rest = note.partition(": ")
            rows.append({"time": j["time"], "type": "plan", "agent": aid, "session": sess,
                         "decision": rest.split(".")[0], "why": rest.partition(". ")[2],
                         "watchlist": [{"symbol": s.strip()} for s in (j.get("symbol") or "").split(",") if s.strip()]})
        elif t == "topup":
            rows.append({"time": j["time"], "type": "topup", "note": j.get("note")})
    store.save("ledger.json", rows)
    print(f"Ledger seeded with {len(rows)} entries.")


def test_alert():
    import os
    import requests
    import alerts
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise SystemExit("NTFY_TOPIC secret is missing or empty - alerts are off.")
    state = store.load_state()
    lines = []
    for aid, a in state.get("agents", {}).items():
        lines.append(f"{alerts.NAMES.get(aid, aid)}: ${a['cash']:.2f} cash, {len(a['positions'])} open")
    body = "Test alert - your phone is connected to the race.\n" + "\n".join(lines)
    r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=15,
                      headers={"Title": "Agent race: alerts are on", "Tags": "white_check_mark",
                               "Click": os.getenv("DASHBOARD_URL", "")})
    print(f"ntfy answered {r.status_code}; topic length {len(topic)} characters")
    if r.status_code != 200:
        raise SystemExit("ntfy did not accept the alert.")


def crash_radar():
    import blackswan
    import radar
    import universe
    from broker import Broker
    b = Broker()
    state = store.load_state() or {"agents": {}}
    syms = list(dict.fromkeys(["SPY"] + config.VOL_UNDERLYINGS + config.BEAR_SYMBOLS + config.SAFE_HAVENS
                              + blackswan.SECTOR_ETFS + universe.core_symbols()))
    bars = {}
    for i in range(0, len(syms), 50):
        bars.update(b.daily_bars(syms[i:i + 50]))
    prices = b.latest_prices(syms)
    r = radar.compute(bars, prices)
    print(f"Crash Radar {r['score']}/100 ({r['tier']}); parts {r['parts']}; facts {r['facts']}; economy {r['economy'].get('status')}")
    dash = store.load("dashboard.json", {})
    if dash:
        dash["radar"] = store.load("radar.json", {})
        store.save("dashboard.json", dash)


def build_universe():
    import universe
    from broker import Broker
    universe.build_weekly(Broker())


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "topup":
        topup(float(sys.argv[2]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        backfill()
    elif len(sys.argv) >= 2 and sys.argv[1] == "radar":
        crash_radar()
    elif len(sys.argv) >= 2 and sys.argv[1] == "alert":
        test_alert()
    elif len(sys.argv) >= 2 and sys.argv[1] == "universe":
        build_universe()
    else:
        print(__doc__)

"""Maintenance commands that change the race's books outside normal trading.

  python admin.py topup 400     add $400 cash to every agent (open positions untouched)

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


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "topup":
        topup(float(sys.argv[2]))
    else:
        print(__doc__)

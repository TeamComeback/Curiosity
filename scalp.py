"""One pass of the scalper's 2-minute loop (called by daylong.py between trading rounds):
manage open scalps, take at most one new one, and refresh the dashboard if anything traded."""
import sys

import alerts
import config
import run
import scalper
import store
from broker import Broker


def main():
    b = Broker()
    if not b.clock().get("is_open"):
        return
    state = store.load_state()
    if not state:
        return
    traded = False
    for aid in scalper.PROFILES:
        try:
            traded = scalper.tick(b, state, aid) or traded
        except Exception as e:  # noqa: BLE001 - one firm's problem must not stop the other
            print(f"[scalp] {aid} tick failed: {type(e).__name__}")
    if not traded:
        store.save("state.json", state)
        return
    held = sorted({p["symbol"] for a in state["agents"].values() for p in a["positions"].values() if p["kind"] == "stock"})
    prices = b.latest_prices(held) if held else {}
    equities = {aid: run.agent_equity(a, prices) for aid, a in state["agents"].items()}
    run.write_dashboard(state, store.load("dashboard.json", {}).get("regime", {}), equities)
    store.save("state.json", state)


if __name__ == "__main__":
    try:
        main()
        alerts.flush("Agent race: Rapid Fire")
    except Exception as e:  # noqa: BLE001 - a failed tick must never stop the trading day
        print(f"[scalp] tick failed: {type(e).__name__}")
        sys.exit(0)

"""Market-day loop. GitHub's scheduler often starts jobs late or skips them, so instead of
relying on one scheduled job per half hour, one job stays up through the trading day: it runs a
trading cycle at :05 and :35 past every hour while the market is open, runs the scalper's quick
check every 2 minutes in between, saves the results to the repo (so the dashboard updates), and
exits after the close. Several start times
are scheduled as backups; whichever starts first covers the day, the rest exit right away.
Usage: python daylong.py [max_minutes]"""
import subprocess
import sys
import time
from datetime import datetime, timedelta

import store
from broker import Broker

MAX_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 340

SAVE = ('git add data/ && (git diff --cached --quiet || (git commit -qm "agent: $(date -u +%Y-%m-%dT%H:%MZ)" && '
        'for i in 1 2 3; do git pull -q --rebase && git push -q && break; sleep 5; done))')


def next_slot(now):
    base = now.replace(second=0, microsecond=0)
    if now.minute < 5:
        return base.replace(minute=5)
    if now.minute < 35:
        return base.replace(minute=35)
    return (base + timedelta(hours=1)).replace(minute=5)


SCALP_EVERY = 120          # seconds between the scalper's checks
SAVE_EVERY = 600           # GitHub Pages rebuilds on each save; keep it to a few an hour


def main():
    start, b, cycles, last_save = time.time(), Broker(), 0, time.time()
    while time.time() - start < MAX_MIN * 60:
        try:
            clk = b.clock()
        except Exception as e:  # noqa: BLE001 - try again next slot
            print(f"[day] clock unavailable: {type(e).__name__}")
            clk = {}
        now = store.now_et()
        if clk.get("is_open"):
            subprocess.run([sys.executable, "run.py"])
            subprocess.run(SAVE, shell=True)
            last_save = time.time()
            cycles += 1
        elif clk.get("next_open"):
            nxt = datetime.fromisoformat(clk["next_open"])
            if nxt.date() != now.date():          # done for today (after the close, weekend or holiday)
                print(f"[day] market closed until {clk['next_open']}; {cycles} cycles run.")
                return
        now = store.now_et()
        target = next_slot(now)
        if time.time() - start + (target - now).total_seconds() > MAX_MIN * 60:
            break
        while True:                                   # between rounds: the scalper's 2-minute checks
            left = (target - store.now_et()).total_seconds()
            if left <= 0:
                break
            time.sleep(min(SCALP_EVERY, max(left, 1)))
            if clk.get("is_open") and (target - store.now_et()).total_seconds() > 45:
                subprocess.run([sys.executable, "scalp.py"])
                if time.time() - last_save >= SAVE_EVERY:
                    subprocess.run(SAVE, shell=True)
                    last_save = time.time()
    print(f"[day] time limit reached after {cycles} cycles; the next scheduled job takes over.")


if __name__ == "__main__":
    main()

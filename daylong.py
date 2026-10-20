"""The firms' working day, run as one chain of GitHub Actions jobs.

GitHub's scheduler regularly starts jobs hours late or skips them, so nothing here depends on it
firing on time. Once a job starts, it keeps the whole day going by itself:
  pre-market call ~7:40 AM ET  ->  trading rounds at :05/:35 with the scalper's check every 2 minutes
  ->  after-close review ~4:40 PM  ->  Friday weekly review ~5:30 PM  ->  evening check-in ~8:10 PM
GitHub jobs can last at most 6 hours, so before that limit a job starts its own successor and exits;
the chain ends after the evening check-in. Any job that starts with nothing to do soon simply exits.
The planning, review and trading scripts skip anything already done, so overlaps are harmless.
Usage: python daylong.py [max_minutes]"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import requests

import store
from broker import Broker

MAX_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 330
SCALP_EVERY = 120          # seconds between the scalper's checks
SAVE_EVERY = 600           # GitHub Pages rebuilds on each save; keep it to a few an hour
IDLE_EXIT_HOURS = 4        # nothing scheduled this soon: exit and let tomorrow's start take over

SAVE = ('git add data/ && (git diff --cached --quiet || (git commit -qm "agent: $(date -u +%Y-%m-%dT%H:%MZ)" && '
        'for i in 1 2 3; do git pull -q --rebase && git push -q && break; sleep 5; done))')


def sh(args):
    subprocess.run(args if isinstance(args, str) else [sys.executable] + args, shell=isinstance(args, str))


def save():
    sh(SAVE)


def next_slot(now):
    base = now.replace(second=0, microsecond=0)
    if now.minute < 5:
        return base.replace(minute=5)
    if now.minute < 35:
        return base.replace(minute=35)
    return (base + timedelta(hours=1)).replace(minute=5)


def at(now, hhmm):
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def planning_events(now):
    """Today's out-of-market jobs (ET), in order: (time, label, command)."""
    wd = now.weekday()              # Mon=0 .. Sun=6
    ev = []
    if wd <= 4:
        ev.append((at(now, "07:40"), "pre-market call", ["plan.py"]))
        ev.append((at(now, "16:40"), "after-close review", ["plan.py"]))
        if wd == 4:
            ev.append((at(now, "17:30"), "weekly self-review", ["review.py"]))
    if wd in (6, 0, 1, 2, 3):
        ev.append((at(now, "20:10"), "evening check-in", ["plan.py"]))
    return ev


def start_successor():
    """Start the next job in the chain (allowed with the job's own token for workflow_dispatch)."""
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        print("[day] no token to start a successor; the backup schedule will pick up")
        return
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/agent.yml/dispatches", timeout=20,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"ref": os.getenv("GITHUB_REF_NAME", "main"), "inputs": {"task": "day"}})
    print(f"[day] successor requested: HTTP {r.status_code}")


def main():
    start, b = time.time(), Broker()
    last_save, done = time.time(), set()
    elapsed = lambda: (time.time() - start) / 60

    while True:
        now = store.now_et()
        try:
            clk = b.clock()
        except Exception as e:  # noqa: BLE001
            print(f"[day] clock unavailable: {type(e).__name__}")
            clk = {}
        is_open = bool(clk.get("is_open"))

        # out-of-market jobs whose time has come (each script skips work already done)
        for when, label, cmd in planning_events(now):
            if now >= when and label not in done and (now - when) < timedelta(hours=3) and not is_open:
                print(f"[day] {label}")
                sh("git pull -q --rebase || true")        # pick up any code changes made during the day
                sh(cmd)
                save()
                last_save = time.time()
                done.add(label)

        if is_open:                                  # a trading round
            sh(["run.py"])
            save()
            last_save = time.time()

        # what's next?
        now = store.now_et()
        upcoming = [w for w, label, _ in planning_events(now) if w > now and label not in done]
        if is_open:
            upcoming.append(next_slot(now))
        elif clk.get("next_open"):
            nxt_open = datetime.fromisoformat(clk["next_open"]).astimezone(now.tzinfo)
            if nxt_open.date() == now.date():
                upcoming.append(nxt_open + timedelta(minutes=5))
        if not upcoming:
            print("[day] nothing left today; the chain ends here.")
            return
        target = min(upcoming)
        if (target - now) > timedelta(hours=IDLE_EXIT_HOURS):
            print(f"[day] next job at {target:%H:%M} ET is far off; exiting.")
            return
        if elapsed() + (target - now).total_seconds() / 60 > MAX_MIN:
            start_successor()                         # near GitHub's 6-hour limit: hand over
            return

        while True:                                   # wait; the scalper checks every 2 minutes while open
            left = (target - store.now_et()).total_seconds()
            if left <= 0:
                break
            time.sleep(min(SCALP_EVERY, max(left, 1)))
            if is_open and (target - store.now_et()).total_seconds() > 45:
                sh(["scalp.py"])
                if time.time() - last_save >= SAVE_EVERY:
                    save()
                    last_save = time.time()


if __name__ == "__main__":
    main()

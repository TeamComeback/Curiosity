"""Phone alerts through ntfy (free app: ntfy.sh). One short summary per run, only when
something worth knowing happened. Set the NTFY_TOPIC secret to turn it on; leave it empty
to turn alerts off. Anyone who knows the topic name can read it, so keep it long and random."""
import os

import requests

import config
import store

import re

MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def nice(sym):
    m = re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", str(sym or ""))
    return f"{m[1]} ${int(m[6]) / 1000:g} {'call' if m[5] == 'C' else 'put'} {MON[int(m[3]) - 1]} {int(m[4])}" if m else str(sym or "")


NAMES = {"trend": "Trend Co.", "meanrev": "Snapback", "volsell": "Premium House", "baseline": "SPY", "monkey": "Monkey"}


def _line(e):
    who = NAMES.get(e.get("agent"), e.get("agent") or "")
    t = e.get("type")
    if t == "open":
        return f"🟢 {who} bought {nice(e.get('symbol'))}{' shares' if e.get('instrument') == 'stock' else ''} for ${abs(e.get('cost') or 0):.2f}"
    if t == "close":
        pnl = e.get("pnl") or 0
        return f"{'✅' if pnl >= 0 else '🔻'} {who} closed {nice(e.get('symbol'))}: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} ({e.get('note')})"
    if t in ("eliminated", "failed"):
        return f"⛔ {who} {t}: {e.get('note')}"
    if t == "race_end":
        return f"🏆 Race over: {e.get('note')}"
    if t == "plan" and not str(e.get("note", "")).split(":", 1)[-1].strip().startswith("keep"):
        return f"🧠 {who} changed its plan. {str(e.get('note', ''))[:160]}"
    if t == "scorecard":
        return f"📋 {who}: {e.get('note')}"
    if t == "param_change":
        return f"🔧 {who} retuned itself: {str(e.get('note', ''))[:120]}"
    if t == "plan_error":
        return f"⚠️ {who}: {e.get('note')}"
    return None


def send(title, body, priority="default", tags=""):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic or not body:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if os.getenv("DASHBOARD_URL"):
        headers["Click"] = os.getenv("DASHBOARD_URL")
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
    except Exception as e:  # noqa: BLE001 - alerts must never break a run
        print(f"[alerts] not sent: {type(e).__name__}")


def flush(title="Agent race"):
    lines = [l for l in (_line(e) for e in store.RUN_EVENTS) if l]
    store.RUN_EVENTS.clear()
    if lines:
        urgent = any(l.startswith(("⛔", "🏆")) for l in lines)
        send(title, "\n".join(lines[:12]), priority="high" if urgent else "default")


def setup_problem(detail):
    send("Agent race: setup problem", str(detail)[:300], priority="high", tags="warning")

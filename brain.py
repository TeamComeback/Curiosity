"""Each competing agent's mind. Every run, an agent sees its balance, its open positions,
its scanner's setups and its own notes, then decides: what to close, where to move stops,
what to open, with how many dollars, and in which instrument.

With ANTHROPIC_API_KEY set, decisions come from Claude (BRAIN_MODEL). Without it, each
agent falls back to simple default rules so the system still runs (useful for testing)."""
import json
import re

import requests

import config

DECIDE_SYSTEM = """You are {name}, one of {n_firms} AI trading firms in a paper-trading race.
Each agent started with ${start:,.0f}. The first to reach ${goal:,.0f} wins. An agent that drops below
${elim:.0f}, or can no longer afford any trade, fails. You may only trade your specialty:

{style}

You have full control of your own balance: which setups to take, how many dollars to put on
each, which instrument (only these: {instruments}), where your stop is, and when to exit.
Hard rules you cannot break: cash only (no margin), no naked options, at most {max_trades} new
trades per day, at most {max_pos} open positions. Default exits (stops, targets, time exits)
are enforced automatically; you may close earlier or tighten stops, never loosen a stop past
the default.

"crash_radar" is a shared 0-100 market-stress score (calm under 40, warning 40-70, stress 70+).
Every firm sees it; weigh it against your own setups. Candidates may carry "max_dollars" (your
firm's own per-trade cap) and "suggested_instrument".

Think like a fund manager trying to compound a tiny account fast WITHOUT going bust: a
blown-up agent cannot win. Passing on weak setups is normal.

If "game_plan" is present, it's what your planning team prepared overnight. Use it as your
starting point, but live prices win: drop a planned idea if its trigger hasn't happened, and
take an unplanned setup if it's clearly better.

Reply with ONLY a JSON object:
{{"close":[{{"position_id":"...","reason":"..."}}],
 "adjust_stops":[{{"position_id":"...","stop":<number>}}],
 "open":[{{"candidate_id":<int>,"instrument":"{inst_choices}","dollars":<number>,
          "contracts":<int, credit spreads only>,"stop":<number, stock/options: underlying stop>,
          "thesis":"one sentence","confidence":1-5}}],
 "note":"optional one-line lesson or plan to remember"}}"""


def _call(model, system, user, max_tokens=1500, timeout=90):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=timeout,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def _rules_default(aid, view):
    """Fallback when no API key: take the top setup with ~40% of available cash."""
    out = {"close": [], "adjust_stops": [], "open": [], "note": "", "source": "rules"}
    cands = view["candidates"]
    if not cands or view["can_open"] is not True:
        return out
    c = cands[0]
    avail = view["balance"]["available"]
    if aid == "baseline":
        out["open"].append({"candidate_id": 0, "instrument": "stock", "dollars": round(avail * 0.99, 2),
                            "thesis": c["rule_thesis"], "confidence": 3})
    elif aid == "monkey":
        out["open"].append({"candidate_id": 0, "instrument": "stock",
                            "dollars": round(avail * c.get("size_pct", 0.4), 2), "thesis": c["rule_thesis"], "confidence": 0})
    elif aid == "blackswan":
        out["open"].append({"candidate_id": 0, "instrument": c.get("suggested_instrument", "debit_spread"),
                            "dollars": round(min(avail * 0.3, c.get("max_dollars") or avail), 2),
                            "stop": c.get("stop"), "thesis": c["rule_thesis"], "confidence": 3})
    elif aid == "volsell":
        out["open"].append({"candidate_id": 0, "instrument": "spread", "contracts": 1,
                            "thesis": c["rule_thesis"], "confidence": 3})
    else:
        inst = "put" if c.get("direction") == "short" else "stock"
        out["open"].append({"candidate_id": 0, "instrument": inst, "dollars": round(avail * 0.4, 2),
                            "stop": c.get("stop"), "thesis": c["rule_thesis"], "confidence": 3})
    return out


def decide(aid, view):
    if aid in ("baseline", "monkey") or not config.ANTHROPIC_API_KEY:
        return _rules_default(aid, view)
    a = config.AGENTS[aid]
    system = DECIDE_SYSTEM.format(name=a["name"], goal=config.RACE["goal"], start=config.RACE["start_balance"], elim=config.RACE["eliminate_below"],
                                  style=a["style"], instruments=", ".join(a["instruments"]),
                                  inst_choices="|".join(a["instruments"]),
                                  n_firms=sum(1 for x in config.AGENTS.values() if x["competing"]),
                                  max_trades=config.RULES["max_new_trades_per_day"],
                                  max_pos=config.RULES["max_open_positions"])
    try:
        d = _call(config.BRAIN_MODEL, system, json.dumps(view, default=str))
        d.setdefault("close", []); d.setdefault("adjust_stops", []); d.setdefault("open", [])
        d["source"] = "llm"
        return d
    except Exception as e:  # fail closed: no new trades this run, default exits still run
        print(f"[brain:{aid}] error: {type(e).__name__}")  # never print details: they can contain the key
        return {"close": [], "adjust_stops": [], "open": [], "note": "", "source": f"error: {type(e).__name__}"}


def weekly_review(aid, stats, params, bounds, notes):
    """The agent reviews its own week and may retune its own strategy (bounded)."""
    if not config.ANTHROPIC_API_KEY:
        return {"summary": "Rules-only mode: no AI review.", "changes": []}
    a = config.AGENTS[aid]
    prompt = {
        "you_are": a["name"], "specialty": a["style"],
        "task": "Review your own performance this week in the race to $%.0f. Decide whether to retune "
                "your strategy parameters (at most 2 changes, within bounds) to compound faster without "
                "busting. Reply ONLY JSON: {\"summary\":\"2-3 sentences\",\"changes\":[{\"param\":\"...\","
                "\"new\":<number>,\"reason\":\"...\"}],\"note\":\"lesson to remember\"}" % config.RACE["goal"],
        "stats": stats, "current_params": params, "bounds": bounds, "your_recent_notes": notes[-10:],
    }
    try:
        return _call(config.REVIEW_MODEL, "You are a self-improving trading agent.", json.dumps(prompt, default=str), 1200, 120)
    except Exception as e:
        return {"summary": f"Review failed ({type(e).__name__}).", "changes": []}


def final_assessment(payload):
    """After a winner is crowned: assess every agent, including failed ones."""
    if not config.ANTHROPIC_API_KEY:
        return {"summary": "Rules-only mode: see the standings and stats for the assessment.", "agents": {}}
    task = ("The race is over. Assess each agent: why it won, lagged, or failed; what its trade record "
            "shows about its edge (or lack of one); its risk-taking vs results; and whether its result "
            "looks like skill or luck. Compare all against the SPY benchmark. Reply ONLY JSON: "
            "{\"summary\":\"...\",\"agents\":{\"trend\":\"...\",\"meanrev\":\"...\",\"volsell\":\"...\",\"blackswan\":\"...\"},"
            "\"lessons\":[\"...\"]}")
    try:
        return _call(config.REVIEW_MODEL, "You are a hedge fund risk officer writing a post-mortem.",
                     json.dumps({"task": task, **payload}, default=str), 2500, 180)
    except Exception as e:
        return {"summary": f"Assessment failed ({type(e).__name__}).", "agents": {}}

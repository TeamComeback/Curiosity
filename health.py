"""Pre-flight checks. Each run first confirms the keys and data feeds work, and saves
plain-English results to data/health.json so problems show on the dashboard and in
the repo, not just in hidden Actions logs. Keys are never written anywhere."""
import os
import re
import traceback

import requests

import config
import store


SECRET_NAMES = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ANTHROPIC_API_KEY")


def redact(text):
    """Remove any secret value (raw or cleaned) and anything key-shaped from text."""
    text = str(text)
    for n in SECRET_NAMES + ("FRED_API_KEY", "NTFY_TOPIC"):
        raw = os.getenv(n, "")
        for v in {raw, raw.strip(), "".join(raw.split())}:
            if len(v) >= 6:
                text = text.replace(v, "[hidden]")
        for part in raw.split():
            if len(part) >= 6:
                text = text.replace(part, "[hidden]")
    text = re.sub(r"api_key=[^&\s]+", "api_key=[hidden]", text)
    text = re.sub(r"sk-ant-[\w\-\u0080-\uffff\\n]+", "sk-ant-[hidden]", text)
    text = re.sub(r"\bPK[A-Z0-9]{8,}\b", "PK[hidden]", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001 - we want every failure reported
        ok, detail = False, type(e).__name__ + ": " + str(e)[:300]
    return {"name": name, "ok": bool(ok), "detail": redact(detail)[:300]}


def _secrets():
    missing = [n for n in SECRET_NAMES if not os.getenv(n, "").strip()]
    if missing:
        return False, "Missing GitHub secret(s): " + ", ".join(missing) + ". Names must match exactly."
    bad = [n for n in SECRET_NAMES if not "".join(os.getenv(n, "").split()).isascii()]
    if bad:
        return False, ("These secrets contain unusual characters (often from copying via a screenshot, notes app "
                       "or text recognition): " + ", ".join(bad) + ". Copy the key with the provider's Copy button "
                       "and paste it directly into the GitHub secret.")
    spaced = [n for n in SECRET_NAMES if os.getenv(n, "") != "".join(os.getenv(n, "").split())]
    key = config.API_KEY
    hint = "" if key.startswith("PK") else " Note: paper keys usually start with 'PK'; live keys won't work here."
    if spaced:
        hint += " Removed stray spaces/line breaks from: " + ", ".join(spaced) + "."
    return True, "All three secrets are present." + hint


def _alpaca_account():
    r = requests.get(config.PAPER_BASE + "/v2/account", timeout=20,
                     headers={"APCA-API-KEY-ID": config.API_KEY, "APCA-API-SECRET-KEY": config.API_SECRET})
    if r.status_code in (401, 403):
        return False, (f"Alpaca rejected the keys (HTTP {r.status_code}). Regenerate keys on the PAPER account and "
                       "update ALPACA_API_KEY (the PK... ID) and ALPACA_API_SECRET.")
    if r.status_code >= 400:
        return False, f"Alpaca account error HTTP {r.status_code}: {r.text[:200]}"
    a = r.json()
    return True, f"Paper account OK (status {a.get('status')}, options level {a.get('options_trading_level', 'n/a')})."


def _alpaca_data():
    r = requests.get(config.DATA_BASE + "/v2/stocks/bars", timeout=20,
                     headers={"APCA-API-KEY-ID": config.API_KEY, "APCA-API-SECRET-KEY": config.API_SECRET},
                     params={"symbols": "SPY", "timeframe": "1Day", "limit": 5, "feed": config.STOCK_FEED})
    if r.status_code >= 400:
        return False, f"Market data error HTTP {r.status_code}: {r.text[:200]}"
    n = len((r.json().get("bars") or {}).get("SPY", []))
    return n > 0, f"Stock data OK ({n} SPY bars)." if n else "Stock data returned no bars."


def _alpaca_options():
    r = requests.get(config.DATA_BASE + "/v1beta1/options/snapshots/SPY", timeout=20,
                     headers={"APCA-API-KEY-ID": config.API_KEY, "APCA-API-SECRET-KEY": config.API_SECRET},
                     params={"feed": config.OPTION_FEED, "limit": 5, "type": "put"})
    if r.status_code >= 400:
        return False, f"Options data error HTTP {r.status_code}: {r.text[:200]}"
    return True, f"Options data OK ({len(r.json().get('snapshots') or {})} contracts sampled)."


def _anthropic():
    if not config.ANTHROPIC_API_KEY:
        return False, "No ANTHROPIC_API_KEY, agents will run on fallback rules only."
    r = requests.get("https://api.anthropic.com/v1/models", timeout=20,
                     headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
    if r.status_code == 401:
        return False, "Anthropic rejected the API key (401). Re-copy it into the ANTHROPIC_API_KEY secret."
    if r.status_code == 400 and "workspace" in r.text.lower():
        return False, ("This Anthropic key is organization-scoped and needs a workspace. Create a new key with "
                       "Scope set to a workspace (e.g. Default) and replace the secret.")
    if r.status_code >= 400:
        return False, f"Anthropic error HTTP {r.status_code}: {r.text[:200]}"
    missing = []
    for m in dict.fromkeys((config.BRAIN_MODEL, config.REVIEW_MODEL)):
        mr = requests.get(f"https://api.anthropic.com/v1/models/{m}", timeout=20,
                          headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
        if mr.status_code == 404:
            missing.append(m)
    if missing:
        return False, f"Key works, but model(s) not available to it: {', '.join(missing)}."
    return True, "Anthropic key OK."


def _fred():
    r = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=20,
                     params={"series_id": "T10Y2Y", "api_key": config.FRED_API_KEY, "file_type": "json",
                             "sort_order": "desc", "limit": 1})
    if r.status_code == 200:
        return True, "Connected: the Crash Radar includes economy data."
    return False, (f"FRED answered {r.status_code}. Check the FRED_API_KEY secret (32 letters and numbers). "
                   "The Crash Radar keeps running on market data meanwhile.")


def run_checks():
    checks = [_check("GitHub secrets", _secrets)]
    if checks[0]["ok"] or (config.API_KEY and config.API_SECRET):
        checks += [_check("Alpaca paper account", _alpaca_account),
                   _check("Alpaca stock data", _alpaca_data),
                   _check("Alpaca options data", _alpaca_options)]
    checks.append(_check("Anthropic API", _anthropic))
    if config.FRED_API_KEY:
        checks.append(_check("FRED economy data", _fred))
    save(checks)
    return checks


def save(checks, crash=None):
    store.save("health.json", {
        "time": store.now_et().isoformat(timespec="minutes"),
        "ok": all(c["ok"] for c in checks) and not crash,
        "checks": checks,
        "crash": crash,
    })


def record_crash(exc):  # noqa: D401
    h = store.load("health.json", {"checks": []})
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    where = next((l.strip() for l in reversed(tb) if l.strip().startswith('File "')), "")
    save(h.get("checks", []), crash={"error": redact(f"{type(exc).__name__}: {str(exc)[:400]}"), "where": redact(where)[:300],
                                     "time": store.now_et().isoformat(timespec="minutes")})

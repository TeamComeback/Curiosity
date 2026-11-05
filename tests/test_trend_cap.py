"""Trend Co. refuses a 7th entry once it holds 6 positions (HQ item p20261029curiosity)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, governor


def _state(n):
    return {"race": {"status": "running"}, "entries": {"date": None, "counts": {}},
            "agents": {"trend": {"status": "active", "positions": {f"p{i}": {} for i in range(n)}}}}


def test_cap_is_six():
    assert config.AGENTS["trend"]["max_open_positions"] == 6


def test_seventh_entry_refused():
    ok, why = governor._check_order(_state(6), "trend", {"symbol": "XYZ", "direction": "long"}, "stock", 10)
    assert not ok and "cap" in why


def test_can_trade_blocked_at_cap_not_below(monkeypatch):
    monkeypatch.setattr(governor, "entries_today", lambda s, a: 0)
    assert governor.can_trade_today(_state(6), "trend")[0] is False
    assert governor.can_trade_today(_state(5), "trend")[0] is True

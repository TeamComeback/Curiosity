"""Trend Co. half-size rule and the 20% per-entry cap (HQ items fix-dd-trend, p20261109curiosity, p20261106curiosity)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import governor


def agent(cash, wins, closed, positions=()):
    return {"cash": cash, "wins": wins, "closed": closed, "positions": {f"p{i}": {"cost": c} for i, c in enumerate(positions)}}


def test_trend_half_size_while_losing():
    d, why = governor.size_entry(agent(1000, 4, 23), "trend", 100)
    assert d == 50 and "half size" in why


def test_trend_full_size_once_winning():
    assert governor.size_entry(agent(1000, 8, 23), "trend", 100) == (100, "")


def test_no_cut_on_small_sample():
    assert governor.size_entry(agent(1000, 0, 5), "trend", 100)[0] == 100


def test_entry_cap_keeps_cash_for_next_setup():
    d, why = governor.size_entry(agent(300, 13, 19, positions=(700,)), "meanrev", 600)
    assert d == 200 and "20%" in why


def test_other_firms_untouched():
    assert governor.size_entry(agent(1000, 0, 30), "volsell", 900) == (900, "")

"""HQ items p20261118curiosity and p20261119curiosity: Trend Co. exhibition with a rejoin rule, and avg win/loss sizes."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, run, store


def test_trend_is_exhibition_with_rejoin_value():
    t = config.AGENTS["trend"]
    assert not t["competing"] and t["exhibition"] and t["rejoin_win_rate"] == 0.30


def test_win_loss_sizes_and_dashes():
    led = [{"type": "trade_closed", "agent": "a", "pnl": 10, "return_pct": 5},
           {"type": "trade_closed", "agent": "a", "pnl": -5, "return_pct": -2.5},
           {"type": "trade_closed", "agent": "a", "pnl": 99, "partial": True},
           {"type": "trade_closed", "agent": "b", "pnl": 4, "return_pct": 2}]
    s = run.win_loss_sizes(led)
    assert s["a"]["win"]["usd"] == 10 and s["a"]["loss"]["usd"] == -5 and s["a"]["ratio"] == 2.0
    assert s["b"]["loss"] is None and s["b"]["ratio"] is None


def test_rejoin_when_win_rate_tops_30(monkeypatch):
    monkeypatch.setattr(store, "journal", lambda *a, **k: None)
    saved = dict(config.AGENTS["trend"])
    try:
        st = {"agents": {"trend": {"closed": 25, "wins": 4}}}
        store.apply_rejoin(st)
        assert not config.AGENTS["trend"]["competing"]
        st["agents"]["trend"].update(closed=30, wins=10)
        store.apply_rejoin(st)
        assert config.AGENTS["trend"]["competing"] and not config.AGENTS["trend"]["exhibition"]
    finally:
        config.AGENTS["trend"].clear(); config.AGENTS["trend"].update(saved)

"""Tests for the 8 built-in proactive triggers.

These hit the rule logic without LLM / network by passing brain=None
(falls back to short_msg, no flavour call). State is forced into a
temporary AIME_HOME per test so they don't see each other.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Make repo root importable
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _fresh_home(tmp_path, monkeypatch):
    """Point AIME_HOME at a brand-new dir so memory + alerts_state start empty."""
    home = tmp_path / "aime"
    home.mkdir()
    monkeypatch.setenv("AIME_HOME", str(home))
    monkeypatch.setenv("AIME_API_KEY", "fake")
    monkeypatch.setenv("AIME_LLM_PROVIDER", "stub")
    # Reload modules so they see the new HOME
    for mod in ("memory", "event_watcher"):
        sys.modules.pop(mod, None)
    return home


def _read_outbox(home: Path) -> list[dict]:
    p = home / "outbox.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Individual triggers
# ---------------------------------------------------------------------------

def test_balance_low_fires_once_then_cooldown(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew

    n = ew.check_all(balance=42, brain=None)
    assert n >= 1
    out = _read_outbox(home)
    assert any(o["msg_type"] == "balance_low" for o in out)

    # Second call within cooldown: must NOT re-fire
    n2 = ew.check_all(balance=30, brain=None)
    out2 = _read_outbox(home)
    assert len([o for o in out2 if o["msg_type"] == "balance_low"]) == 1


def test_balance_low_does_not_fire_above_threshold(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    ew.check_all(balance=1000, brain=None)
    out = _read_outbox(home)
    assert all(o["msg_type"] != "balance_low" for o in out)


def test_drawdown_tracks_peak_and_fires_thresholds(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew

    # First call sets peak
    ew.check_all(balance=1000, brain=None)
    # Down 22% — should trip the 20% threshold (but not the 50%)
    ew.check_all(balance=780, brain=None)
    out = _read_outbox(home)
    dd = [o for o in out if o["msg_type"] == "drawdown"]
    assert len(dd) == 1

    # Down 55% — trips the 50% threshold (not 20% again)
    ew.check_all(balance=450, brain=None)
    dd = [o for o in _read_outbox(home) if o["msg_type"] == "drawdown"]
    assert len(dd) == 2

    # Slight recovery, still below 50%: no new fire
    ew.check_all(balance=460, brain=None)
    dd = [o for o in _read_outbox(home) if o["msg_type"] == "drawdown"]
    assert len(dd) == 2


def test_profit_milestone_fires_at_each_threshold(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew

    # First call sets starting balance
    ew.check_all(balance=1000, brain=None)
    # Up 11% -> fires 0.1
    ew.check_all(balance=1110, brain=None)
    pm = [o for o in _read_outbox(home) if o["msg_type"] == "profit_milestone"]
    assert len(pm) == 1

    # Up 22% -> fires 0.2 (not 0.1 again, not 0.5)
    ew.check_all(balance=1220, brain=None)
    pm = [o for o in _read_outbox(home) if o["msg_type"] == "profit_milestone"]
    assert len(pm) == 2

    # Up 55% -> fires 0.5
    ew.check_all(balance=1550, brain=None)
    pm = [o for o in _read_outbox(home) if o["msg_type"] == "profit_milestone"]
    assert len(pm) == 3


def test_losing_streak_needs_three_consecutive(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    import memory as mem

    mem.add_reflection("m1", "r", "NO", won=False, pnl=-1)
    mem.add_reflection("m2", "r", "NO", won=False, pnl=-1)
    # Only 2 losses: shouldn't trigger
    ew.check_all(balance=900, brain=None)
    out = _read_outbox(home)
    assert all(o["msg_type"] != "losing_streak" for o in out)

    # Third loss: triggers
    mem.add_reflection("m3", "r", "NO", won=False, pnl=-2)
    ew.check_all(balance=850, brain=None)
    ls = [o for o in _read_outbox(home) if o["msg_type"] == "losing_streak"]
    assert len(ls) == 1


def test_winning_streak_needs_three_consecutive(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    import memory as mem

    mem.add_reflection("m1", "r", "YES", won=True, pnl=2)
    mem.add_reflection("m2", "r", "YES", won=True, pnl=3)
    ew.check_all(balance=1100, brain=None)
    assert not [o for o in _read_outbox(home) if o["msg_type"] == "winning_streak"]

    mem.add_reflection("m3", "r", "YES", won=True, pnl=1)
    ew.check_all(balance=1110, brain=None)
    ws = [o for o in _read_outbox(home) if o["msg_type"] == "winning_streak"]
    assert len(ws) == 1


def test_market_settled_one_alert_per_market(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    import memory as mem

    mem.add_reflection("mkt-A", "r", "YES", won=True, pnl=5)
    mem.add_reflection("mkt-B", "r", "NO", won=False, pnl=-3)
    ew.check_all(balance=1002, brain=None)
    ms = [o for o in _read_outbox(home) if o["msg_type"] == "market_settled"]
    assert len(ms) == 2

    # Same reflections shouldn't re-announce
    ew.check_all(balance=1002, brain=None)
    ms = [o for o in _read_outbox(home) if o["msg_type"] == "market_settled"]
    assert len(ms) == 2

    # New reflection -> new alert
    mem.add_reflection("mkt-C", "r", "YES", won=True, pnl=4)
    ew.check_all(balance=1006, brain=None)
    ms = [o for o in _read_outbox(home) if o["msg_type"] == "market_settled"]
    assert len(ms) == 3


def test_owner_intel_paid_off_picks_intel_helped_rows(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    import memory as mem

    # No intel_helped flag: should not trigger
    mem.add_reflection("mkt-X", "r", "YES", won=True, pnl=3)
    ew.check_all(balance=1003, brain=None)
    assert not [o for o in _read_outbox(home) if o["msg_type"] == "owner_intel_paid_off"]

    # Add intel_helped reflection
    mem._append(mem.REFLECTIONS, {
        "kind": "reflection", "market_id": "mkt-Y", "reasoning": "r",
        "outcome": "YES", "won": True, "pnl": 4, "intel_helped": True,
    })
    ew.check_all(balance=1007, brain=None)
    intel = [o for o in _read_outbox(home) if o["msg_type"] == "owner_intel_paid_off"]
    assert len(intel) == 1

    # Should not re-announce
    ew.check_all(balance=1007, brain=None)
    intel = [o for o in _read_outbox(home) if o["msg_type"] == "owner_intel_paid_off"]
    assert len(intel) == 1


def test_chain_error_rate_fires_when_majority_failed(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew

    # 7/10 fails -> should fire
    for _ in range(7):
        ew.record_trade_outcome(False)
    for _ in range(3):
        ew.record_trade_outcome(True)
    ew.check_all(balance=1000, brain=None)
    ce = [o for o in _read_outbox(home) if o["msg_type"] == "chain_error_rate"]
    assert len(ce) == 1

    # Within cooldown, even more failures shouldn't re-fire immediately
    for _ in range(5):
        ew.record_trade_outcome(False)
    ew.check_all(balance=1000, brain=None)
    ce = [o for o in _read_outbox(home) if o["msg_type"] == "chain_error_rate"]
    assert len(ce) == 1


def test_chain_error_rate_quiet_when_mostly_ok(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew

    for _ in range(9):
        ew.record_trade_outcome(True)
    for _ in range(1):
        ew.record_trade_outcome(False)
    ew.check_all(balance=1000, brain=None)
    assert not [o for o in _read_outbox(home) if o["msg_type"] == "chain_error_rate"]


def test_disabled_flag_means_no_fires(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import event_watcher as ew
    n = ew.check_all(balance=5, brain=None, enabled=False)
    assert n == 0
    assert _read_outbox(home) == []

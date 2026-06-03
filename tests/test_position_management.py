"""Stop-loss / take-profit rule tests for manage_positions().

These hit the rule logic without a real backend by stubbing APIClient with
fake position lists and recording sell calls. Run with:

    cd <repo>
    python -m pytest tests/test_position_management.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the top-level agent.py importable. The repo layout puts everything at
# the root, so add the repo directory to sys.path.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Stub envs so importing agent doesn't try to touch the real backend.
os.environ.setdefault("AIME_API_KEY", "fake-test-key")
os.environ.setdefault("AIME_LLM_PROVIDER", "stub")
os.environ.setdefault("AIME_HOME", "/tmp/aime-test-home")

import agent  # noqa: E402


class FakeAPI:
    """Minimal stand-in for APIClient — captures sell calls."""

    def __init__(self, positions):
        self._positions = positions
        self.sell_calls = []

    def get_positions(self):
        return list(self._positions)

    def sell(self, market_id, shares, reasoning, position=None, outcome_index=None):
        self.sell_calls.append({
            "market_id": market_id,
            "shares": shares,
            "reasoning": reasoning,
            "position": position,
            "outcome_index": outcome_index,
        })
        return {"ok": True}


def _pos(market_id, *, shares, spent, value, position="YES", outcome_index=None,
         question="Will X happen?"):
    return {
        "market_id": market_id,
        "market_question": question,
        "position": position,
        "outcome_index": outcome_index,
        "total_shares": shares,
        "total_spent": spent,
        "current_value": value,
        "pnl": value - spent,
        "current_price": (value / shares) if shares else 0.0,
        "trade_count": 1,
    }


def test_healthy_position_is_left_alone():
    api = FakeAPI([_pos("m1", shares=10, spent=5, value=6)])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 0
    assert api.sell_calls == []


def test_stop_loss_closes_at_half_cost():
    api = FakeAPI([_pos("m2", shares=20, spent=10, value=4, position="NO")])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 1
    sell = api.sell_calls[0]
    assert sell["market_id"] == "m2"
    assert sell["shares"] == 20.0
    assert sell["position"] == "NO"
    assert "stop-loss" in sell["reasoning"]


def test_take_profit_closes_at_2x():
    api = FakeAPI([_pos("m3", shares=5, spent=2, value=5)])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 1
    assert "take-profit" in api.sell_calls[0]["reasoning"]


def test_mixed_only_underwater_closes():
    api = FakeAPI([
        _pos("ma", shares=5, spent=2, value=2.5),                        # healthy
        _pos("mb", shares=5, spent=4, value=1.5, position="NO"),         # SL
    ])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 1
    assert api.sell_calls[0]["market_id"] == "mb"


def test_multi_outcome_uses_outcome_index():
    api = FakeAPI([_pos("mm", shares=3, spent=6, value=1,
                        position=None, outcome_index=2)])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 1
    sell = api.sell_calls[0]
    assert sell["outcome_index"] == 2
    assert sell["position"] is None


def test_zero_spent_is_skipped():
    api = FakeAPI([_pos("mz", shares=5, spent=0, value=0)])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 0
    assert api.sell_calls == []


def test_both_thresholds_none_disables_everything():
    api = FakeAPI([_pos("md", shares=5, spent=10, value=1)])
    closed = agent.manage_positions(api, stop_loss_pct=None, take_profit_pct=None)
    assert closed == 0
    assert api.sell_calls == []


def test_stop_loss_only_take_profit_disabled():
    """If take_profit is None, a winning position should NOT close."""
    api = FakeAPI([_pos("mw", shares=5, spent=2, value=10)])  # 5x — would TP normally
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=None)
    assert closed == 0


def test_take_profit_only_stop_loss_disabled():
    """If stop_loss is None, a losing position should NOT close."""
    api = FakeAPI([_pos("ml", shares=5, spent=10, value=1)])  # heavy loss
    closed = agent.manage_positions(api, stop_loss_pct=None, take_profit_pct=1.0)
    assert closed == 0


def test_get_positions_error_does_not_raise():
    class BrokenAPI:
        def get_positions(self):
            raise RuntimeError("backend down")
        def sell(self, *a, **kw):
            raise AssertionError("should not be called")
    closed = agent.manage_positions(BrokenAPI(), stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 0


# ---------------------------------------------------------------------------
# Smart-exit chain (brain review + trailing stop + time-awareness)
# ---------------------------------------------------------------------------

import memory as mem  # noqa: E402


class FakeBrain:
    """Records decide_exit calls and returns a scripted action."""

    def __init__(self, action="close", reasoning="brain says exit because thesis changed"):
        self.action = action
        self.reasoning = reasoning
        self.calls = []

    def decide_exit(self, position, *, hit, ratio, threshold,
                    hours_to_resolve=None, entry=None):
        self.calls.append({
            "market_id": position.get("market_id"),
            "hit": hit,
            "ratio": ratio,
            "threshold": threshold,
            "hours_to_resolve": hours_to_resolve,
        })
        return {
            "action": self.action,
            "confidence": 0.8,
            "reasoning": self.reasoning,
            "internal_note": "scripted",
        }


def _clear_trailing():
    mem.TRAILING.unlink(missing_ok=True)


def test_brain_hold_keeps_position_open():
    """L1: brain can veto a mechanical stop-loss and hold instead."""
    _clear_trailing()
    api = FakeAPI([_pos("bh", shares=20, spent=10, value=4)])  # SL tripped
    brain = FakeBrain(action="hold", reasoning="thesis intact, holding to settlement")
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0,
                                    brain=brain)
    assert closed == 0
    assert api.sell_calls == []          # did NOT close
    assert brain.calls[0]["hit"] == "stop_loss"


def test_brain_close_uses_brain_reasoning():
    """L1: brain-approved close carries the brain's reasoning to the sell."""
    _clear_trailing()
    api = FakeAPI([_pos("bc", shares=20, spent=10, value=4)])
    brain = FakeBrain(action="close", reasoning="thesis broke, cutting the loser now")
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0,
                                    brain=brain)
    assert closed == 1
    assert api.sell_calls[0]["reasoning"] == "thesis broke, cutting the loser now"


def test_trailing_stop_trips_on_giveback():
    """L2: a position that ran up then gave back >25% of its peak trips.

    First scan at ratio 1.5 sets the peak; no threshold tripped.
    Second scan at ratio 1.0 (<= 1.5*0.75=1.125) trips the trailing stop.
    """
    _clear_trailing()
    # take_profit high so it doesn't fire; stop_loss off so only trailing can.
    p_peak = _pos("tr", shares=10, spent=10, value=15)   # ratio 1.5
    api1 = FakeAPI([p_peak])
    brain = FakeBrain(action="close")
    closed1 = agent.manage_positions(api1, stop_loss_pct=None, take_profit_pct=5.0,
                                     brain=brain)
    assert closed1 == 0                                   # just records peak

    p_back = _pos("tr", shares=10, spent=10, value=10)    # ratio 1.0, gave back
    api2 = FakeAPI([p_back])
    closed2 = agent.manage_positions(api2, stop_loss_pct=None, take_profit_pct=5.0,
                                     brain=brain)
    assert closed2 == 1
    assert brain.calls[-1]["hit"] == "take_profit"        # trailing -> TP-style review


def test_time_to_resolve_passed_to_brain():
    """L3: hours-to-resolution from the market index reaches the brain."""
    _clear_trailing()
    import time as _t
    p = _pos("tm", shares=20, spent=10, value=4)
    api = FakeAPI([p])
    brain = FakeBrain(action="hold")
    future = _t.time() + 12 * 3600                       # 12h out
    market_index = {"tm": {"id": "tm", "end_time": future}}
    agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0,
                           brain=brain, market_index=market_index)
    h = brain.calls[0]["hours_to_resolve"]
    assert h is not None and 11.5 < h < 12.5


def test_no_brain_falls_back_to_mechanical_close():
    """Backward-compat: no brain -> old mechanical close behaviour."""
    _clear_trailing()
    api = FakeAPI([_pos("nb", shares=20, spent=10, value=4)])
    closed = agent.manage_positions(api, stop_loss_pct=-0.5, take_profit_pct=1.0)
    assert closed == 1
    assert "stop-loss" in api.sell_calls[0]["reasoning"]

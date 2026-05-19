"""
event_watcher.py — 8 built-in triggers that decide when the agent should
proactively report to the owner.

Pets talk. So does this one. After every trade cycle, the watcher checks
8 rules; any that fire write a message to the outbox (with personality-
flavoured text from the brain), and high-priority ones also POST to
AIME_WEBHOOK_URL if set.

Rules:
  1. balance_low          : free balance < threshold (default $50)
  2. drawdown             : account down ≥20% or ≥50% from peak
  3. chain_error_rate     : recent trade attempts > 50% failing
  4. losing_streak        : 3 stop-losses in a row
  5. profit_milestone     : account first hits +10% / +20% / +50%
  6. winning_streak       : 3 wins in a row
  7. market_settled       : a market we participated in just resolved
  8. owner_intel_paid_off : a tell-influenced trade settled green

State (peak balance, fired thresholds, cooldown timestamps, etc.) lives
in ~/.aime/alerts_state.json so the watcher remembers across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import memory as mem

log = logging.getLogger("aime-agent.alerts")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

STATE_FILE = mem.HOME / "alerts_state.json"

_DEFAULT_STATE: dict[str, Any] = {
    "peak_balance": None,           # max balance ever seen
    "starting_balance": None,       # for profit_milestone (set on first run)
    "drawdown_thresholds_fired": [],  # [0.2, 0.5] subset already alerted
    "profit_thresholds_fired": [],    # [0.1, 0.2, 0.5] subset already alerted
    "last_fired": {},               # event_type -> ts of last fire (cooldowns)
    "settled_markets_seen": [],     # market_ids we've already announced settled
    "intel_acks_seen": [],          # tell_ids we've already acknowledged paid-off
    "recent_trade_outcomes": [],    # rolling window of last 10 outcomes for chain_error_rate
}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
            # Forward-fill any new keys added in later versions.
            for k, v in _DEFAULT_STATE.items():
                data.setdefault(k, v)
            return data
        except json.JSONDecodeError:
            log.warning("alerts_state.json corrupt; starting fresh")
    return dict(_DEFAULT_STATE)


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

DEFAULT_COOLDOWN_SECONDS = {
    "balance_low": 12 * 3600,
    "losing_streak": 12 * 3600,
    "winning_streak": 12 * 3600,
    "chain_error_rate": 3600,
    # drawdown / profit_milestone / market_settled / owner_intel_paid_off use
    # per-threshold or per-market dedup, not time-based cooldown
}


def _in_cooldown(state: dict, event_type: str) -> bool:
    cd = DEFAULT_COOLDOWN_SECONDS.get(event_type)
    if cd is None:
        return False
    last = state.get("last_fired", {}).get(event_type, 0.0)
    return (time.time() - last) < cd


def _mark_fired(state: dict, event_type: str) -> None:
    state.setdefault("last_fired", {})[event_type] = time.time()


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def _is_quiet_hours() -> bool:
    """23:00 - 08:00 local time. High-priority alerts override."""
    h = time.localtime().tm_hour
    return h >= 23 or h < 8


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _push_webhook(event: dict) -> None:
    url = os.environ.get("AIME_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        import requests
        requests.post(url, json=event, timeout=5)
    except Exception as e:
        log.warning("webhook POST failed: %s", e)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def _emit(
    event_type: str,
    priority: str,
    short_msg: str,
    *,
    brain=None,
    flavour_prompt: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Write one alert. Optionally let the brain write a personality-flavoured
    line. Outbox always gets it; webhook gets it for high priority."""
    quiet = _is_quiet_hours()
    if quiet and priority != "high":
        log.debug("quiet hours: skipping %s (priority=%s)", event_type, priority)
        return

    text = short_msg
    if brain is not None and flavour_prompt:
        try:
            flavoured = brain.answer(flavour_prompt)
            if flavoured and isinstance(flavoured, str):
                text = flavoured.strip()
        except Exception as e:
            log.warning("brain.answer for alert %s failed: %s", event_type, e)

    payload = {
        "event_type": event_type,
        "priority": priority,
        "short": short_msg,
        "text": text,
    }
    if extra:
        payload.update(extra)

    mem.post_to_outbox(text, priority=priority, msg_type=event_type, extra=extra or {})

    if priority == "high":
        _push_webhook(payload)

    log.info("📣 [%s] %s: %s", priority, event_type, short_msg)


# ---------------------------------------------------------------------------
# Triggers — each returns True if it fired (caller may want to know).
# ---------------------------------------------------------------------------

def _trigger_balance_low(
    state: dict, balance: Optional[float], threshold: float, brain
) -> bool:
    if balance is None or balance >= threshold:
        return False
    if _in_cooldown(state, "balance_low"):
        return False
    _mark_fired(state, "balance_low")
    short = f"balance is ${balance:.2f}, below ${threshold:.0f} threshold"
    prompt = (
        f"Tell your owner in 1-2 short sentences that your trading account is "
        f"running low — only ${balance:.2f} left, below your ${threshold:.0f} "
        f"floor. Stay in character. Ask if they want to top up."
    )
    _emit("balance_low", "high", short, brain=brain, flavour_prompt=prompt,
          extra={"balance": balance, "threshold": threshold})
    return True


def _trigger_drawdown(
    state: dict, balance: Optional[float], thresholds: list[float], brain
) -> bool:
    if balance is None:
        return False
    peak = state.get("peak_balance")
    if peak is None or balance > peak:
        state["peak_balance"] = balance
        return False
    if peak <= 0:
        return False

    dd = (peak - balance) / peak  # fraction down from peak, positive number
    fired = state.get("drawdown_thresholds_fired", [])
    # Find the largest threshold we've crossed but not yet alerted on
    candidates = [t for t in thresholds if dd >= t and t not in fired]
    if not candidates:
        return False
    biggest = max(candidates)
    state.setdefault("drawdown_thresholds_fired", []).append(biggest)

    short = f"down {dd*100:.1f}% from peak ${peak:.2f} (now ${balance:.2f})"
    prompt = (
        f"Tell your owner: you're down {dd*100:.0f}% from your peak balance of "
        f"${peak:.2f}, now sitting at ${balance:.2f}. 1-2 sentences, honest, "
        f"in character. Don't panic, but don't brush it off."
    )
    _emit("drawdown", "high", short, brain=brain, flavour_prompt=prompt,
          extra={"balance": balance, "peak": peak, "drawdown_pct": dd,
                 "threshold": biggest})
    return True


def _trigger_profit_milestone(
    state: dict, balance: Optional[float], thresholds: list[float], brain
) -> bool:
    if balance is None:
        return False
    start = state.get("starting_balance")
    if start is None:
        state["starting_balance"] = balance
        return False
    if start <= 0:
        return False

    gain = (balance - start) / start
    if gain <= 0:
        return False
    fired = state.get("profit_thresholds_fired", [])
    candidates = [t for t in thresholds if gain >= t and t not in fired]
    if not candidates:
        return False
    biggest = max(candidates)
    state.setdefault("profit_thresholds_fired", []).append(biggest)

    short = f"up {gain*100:.1f}% from start (${start:.2f} → ${balance:.2f})"
    prompt = (
        f"Brag (just a little) to your owner: account is up {gain*100:.0f}% "
        f"from start. From ${start:.2f} to ${balance:.2f}. 1-2 sentences, "
        f"stay in character, don't be insufferable."
    )
    _emit("profit_milestone", "info", short, brain=brain, flavour_prompt=prompt,
          extra={"balance": balance, "start": start, "gain_pct": gain,
                 "threshold": biggest})
    return True


def _consecutive_outcomes(reflections: list[dict], n: int, want_won: bool) -> bool:
    """True if the last n settled reflections all match won == want_won."""
    if len(reflections) < n:
        return False
    last = reflections[-n:]
    return all(r.get("won") is want_won for r in last)


def _trigger_losing_streak(state: dict, brain) -> bool:
    if _in_cooldown(state, "losing_streak"):
        return False
    refl = mem.recent_reflections(limit=10)
    if not _consecutive_outcomes(refl, 3, False):
        return False
    _mark_fired(state, "losing_streak")
    last_three = refl[-3:]
    total_pnl = sum(float(r.get("pnl") or 0) for r in last_three)
    short = f"3 stop-losses in a row (cumulative pnl ${total_pnl:+.2f})"
    prompt = (
        f"You just took 3 losses in a row, total pnl ${total_pnl:+.2f}. "
        f"Tell your owner honestly — 1-2 sentences. Maybe propose a pause "
        f"or rethink. Stay in character."
    )
    _emit("losing_streak", "info", short, brain=brain, flavour_prompt=prompt,
          extra={"streak": 3, "cum_pnl": total_pnl})
    return True


def _trigger_winning_streak(state: dict, brain) -> bool:
    if _in_cooldown(state, "winning_streak"):
        return False
    refl = mem.recent_reflections(limit=10)
    if not _consecutive_outcomes(refl, 3, True):
        return False
    _mark_fired(state, "winning_streak")
    last_three = refl[-3:]
    total_pnl = sum(float(r.get("pnl") or 0) for r in last_three)
    short = f"3 wins in a row (cumulative pnl ${total_pnl:+.2f})"
    prompt = (
        f"Three wins in a row, cum pnl ${total_pnl:+.2f}. Tell your owner. "
        f"Don't pretend it's all skill (markets are noisy), but enjoy it. "
        f"1-2 sentences, in character."
    )
    _emit("winning_streak", "info", short, brain=brain, flavour_prompt=prompt,
          extra={"streak": 3, "cum_pnl": total_pnl})
    return True


def _trigger_market_settled(state: dict, brain) -> bool:
    """Reflection rows are written when a market we played in settles.
    Announce each unique market once."""
    refl = mem.recent_reflections(limit=20)
    seen = set(state.get("settled_markets_seen", []))
    fresh = [r for r in refl if r.get("market_id") and r.get("market_id") not in seen]
    if not fresh:
        return False
    fired_any = False
    for r in fresh:
        mid = r["market_id"]
        won = r.get("won")
        pnl = float(r.get("pnl") or 0)
        outcome = r.get("outcome") or "?"
        verb = "won" if won else ("lost" if won is False else "settled")
        short = f"market {mid[:8]} {verb} ({outcome}); pnl ${pnl:+.2f}"
        if won:
            prompt = (
                f"A market you bet on just settled and you WON. Outcome={outcome}, "
                f"pnl=${pnl:+.2f}. Tell your owner in 1 sentence, in character, "
                f"a little proud but not braggy. Don't list raw numbers verbatim."
            )
        elif won is False:
            prompt = (
                f"A market you bet on just settled and you LOST. Outcome={outcome}, "
                f"pnl=${pnl:+.2f}. Tell your owner in 1 sentence, in character, "
                f"honest, no melodrama."
            )
        else:
            prompt = (
                f"A market you participated in just settled. Outcome={outcome}, "
                f"pnl=${pnl:+.2f}. Update your owner in 1 sentence."
            )
        _emit("market_settled", "info", short, brain=brain, flavour_prompt=prompt,
              extra={"market_id": mid, "won": won, "pnl": pnl, "outcome": outcome})
        state.setdefault("settled_markets_seen", []).append(mid)
        fired_any = True
    return fired_any


def _trigger_owner_intel_paid_off(state: dict, brain) -> bool:
    """A `tell` from the owner that influenced a trade which then settled
    green deserves a thank-you. We approximate this by looking at recent
    reflections that say `intel_helped` (set by reflection_loop when it
    detects an owner-context-tagged decision led to a win)."""
    refl = mem.recent_reflections(limit=30)
    seen = set(state.get("intel_acks_seen", []))
    fresh = [r for r in refl
             if r.get("intel_helped")
             and r.get("market_id")
             and r.get("market_id") not in seen]
    if not fresh:
        return False
    for r in fresh:
        mid = r["market_id"]
        pnl = float(r.get("pnl") or 0)
        short = f"owner intel paid off on market {mid[:8]} (+${pnl:.2f})"
        prompt = (
            f"Your owner gave you some context recently, and a trade you placed "
            f"after factoring it in just settled with pnl ${pnl:+.2f}. "
            f"Thank them in 1 sentence, in character. Ask for more if it fits."
        )
        _emit("owner_intel_paid_off", "info", short, brain=brain,
              flavour_prompt=prompt, extra={"market_id": mid, "pnl": pnl})
        state.setdefault("intel_acks_seen", []).append(mid)
    return True


def _trigger_chain_error_rate(
    state: dict, window: int, threshold: float, brain
) -> bool:
    """Check rolling trade outcomes. trade_once feeds outcomes into state."""
    if _in_cooldown(state, "chain_error_rate"):
        return False
    outcomes = state.get("recent_trade_outcomes") or []
    if len(outcomes) < max(4, window // 2):
        return False
    recent = outcomes[-window:]
    fails = sum(1 for o in recent if o == "fail")
    rate = fails / len(recent)
    if rate < threshold:
        return False
    _mark_fired(state, "chain_error_rate")
    short = f"{fails}/{len(recent)} recent trades failed ({rate*100:.0f}%)"
    prompt = (
        f"You're seeing {fails} out of the last {len(recent)} trade attempts "
        f"fail ({rate*100:.0f}%). Backend or chain is probably struggling. "
        f"Tell your owner, 1 sentence, in character, calm but real."
    )
    _emit("chain_error_rate", "high", short, brain=brain, flavour_prompt=prompt,
          extra={"fails": fails, "total": len(recent), "rate": rate})
    return True


# ---------------------------------------------------------------------------
# Public entry — called by trade_once at the end of each cycle.
# ---------------------------------------------------------------------------

def record_trade_outcome(success: bool) -> None:
    """trade_once calls this for each attempted trade so we can compute
    chain_error_rate. Buffer stays at 20 entries."""
    state = _load_state()
    arr = state.setdefault("recent_trade_outcomes", [])
    arr.append("ok" if success else "fail")
    if len(arr) > 20:
        del arr[:-20]
    _save_state(state)


def check_all(
    *,
    balance: Optional[float],
    brain,
    enabled: bool = True,
    balance_low_threshold: float = 50.0,
    drawdown_thresholds: tuple[float, ...] = (0.2, 0.5),
    profit_thresholds: tuple[float, ...] = (0.1, 0.2, 0.5),
    chain_error_window: int = 10,
    chain_error_threshold: float = 0.5,
) -> int:
    """Run every trigger. Returns count of events fired this cycle."""
    if not enabled:
        return 0
    state = _load_state()
    fired = 0
    try:
        if _trigger_balance_low(state, balance, balance_low_threshold, brain):
            fired += 1
        if _trigger_drawdown(state, balance, list(drawdown_thresholds), brain):
            fired += 1
        if _trigger_profit_milestone(state, balance, list(profit_thresholds), brain):
            fired += 1
        if _trigger_chain_error_rate(state, chain_error_window, chain_error_threshold, brain):
            fired += 1
        if _trigger_losing_streak(state, brain):
            fired += 1
        if _trigger_winning_streak(state, brain):
            fired += 1
        if _trigger_market_settled(state, brain):
            fired += 1
        if _trigger_owner_intel_paid_off(state, brain):
            fired += 1
    finally:
        _save_state(state)
    return fired

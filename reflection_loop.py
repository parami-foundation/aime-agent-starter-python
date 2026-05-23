"""
Reflection loop.

Periodically:
  1. Scan recently settled markets where this agent had a position
  2. For each, write a `reflection` entry (what we thought vs what happened)
  3. Every N new reflections (or once a day), distill into 1+ `lesson`

Lessons are what the agent actually learns. They get injected into future
trade decisions.
"""

import logging
import time
from typing import List

import memory as mem
import owner_profile
import llm

log = logging.getLogger("aime-agent.reflect")

# How often the loop runs (seconds)
REFLECTION_INTERVAL = 3600  # 1h

# Distill into a new lesson every N unprocessed reflections
DISTILL_THRESHOLD = 5


def find_settled_markets_with_position(api) -> List[dict]:
    """Ask backend for recently settled markets we had a stake in."""
    # We use /trades to find our markets, then check each market's status.
    try:
        trades = api.recent_trades(limit=100)
    except Exception as e:
        log.warning("could not fetch trades: %s", e)
        return []

    seen = mem.reflected_market_ids()
    by_market = {}
    for t in trades:
        mid = t.get("market_id") or t.get("market", {}).get("id")
        if mid and mid not in seen:
            by_market.setdefault(mid, []).append(t)

    settled = []
    for mid, mtrades in by_market.items():
        try:
            r = api.get(f"/markets/{mid}")
            market = r.get("market") if isinstance(r, dict) else r
            if not market:
                continue
            status = (market.get("status") or "").lower()
            if status in ("resolved", "settled", "closed"):
                market["_my_trades"] = mtrades
                settled.append(market)
        except Exception as e:
            log.debug("market %s lookup failed: %s", mid, e)
    return settled


def compute_pnl(market: dict, my_trades: List[dict]) -> tuple:
    """
    Return (won: bool, pnl: float, dominant_position: str, total_amount: float).
    Naive: sum amounts on the winning side minus the losing side.
    """
    winning = (market.get("outcome") or market.get("resolution") or "").lower()
    if winning not in ("yes", "no"):
        return False, 0.0, "?", 0.0

    pnl = 0.0
    total = 0.0
    yes_amt = sum(float(t.get("amount", 0)) for t in my_trades if (t.get("position") or "").lower() == "yes")
    no_amt = sum(float(t.get("amount", 0)) for t in my_trades if (t.get("position") or "").lower() == "no")
    total = yes_amt + no_amt
    if winning == "yes":
        pnl = yes_amt - no_amt
        dominant = "yes" if yes_amt >= no_amt else "no"
    else:
        pnl = no_amt - yes_amt
        dominant = "no" if no_amt >= yes_amt else "yes"

    return pnl > 0, pnl, dominant, total


def write_reflection(market: dict, my_trades: List[dict]):
    market_id = market.get("id") or market.get("market_id")
    title = market.get("title") or market.get("question") or "?"
    outcome = (market.get("outcome") or market.get("resolution") or "?").lower()

    won, pnl, dominant_pos, total_amt = compute_pnl(market, my_trades)

    # Pull the original reasoning from decisions log
    original = mem.find_decision(market_id) or {}
    original_reasoning = original.get("reasoning", "")
    internal_note = original.get("internal_note", "")

    # Ask LLM to summarize what happened
    prompt = [
        {"role": "system", "content": "You write short post-mortems for a prediction-market trader. Be honest, blunt, and specific. 2-3 sentences max."},
        {"role": "user", "content": (
            f"Market: {title}\n"
            f"Outcome: {outcome.upper()}\n"
            f"My dominant position: {dominant_pos.upper()} (${total_amt:.2f} total)\n"
            f"Result: {'WON' if won else 'LOST'}, PnL ${pnl:.2f}\n\n"
            f"My original public reasoning: {original_reasoning}\n"
            f"My private note: {internal_note}\n\n"
            f"In 2-3 sentences: what went right or wrong? Be specific about the *reasoning*, not just the outcome."
        )},
    ]
    what_went = llm.chat(prompt, max_tokens=200, temperature=0.4) or (
        f"{'Won' if won else 'Lost'} ${abs(pnl):.2f}. No LLM available for deeper analysis."
    )

    tags = []
    title_low = title.lower()
    for t in ("bnb", "btc", "eth", "nba", "election", "ai", "crypto", "sport"):
        if t in title_low:
            tags.append(t)

    mem.add_reflection(
        market_id=market_id,
        market_title=title,
        my_position=dominant_pos,
        my_amount=total_amt,
        outcome=outcome,
        won=won,
        pnl=pnl,
        original_reasoning=original_reasoning,
        internal_note=internal_note,
        what_went=what_went,
        tags=tags,
    )
    log.info("reflected on %s — %s ($%.2f)", title[:60], "WON" if won else "LOST", pnl)

    # v2.10 — if this win was triggered by an owner tell, mark that source
    # as an edge in about_owner.md. Best-effort; silent on failure.
    try:
        triggering_tell = None
        triggering_source = None
        if won and "based on recent context" in (original_reasoning or "").lower():
            recent = mem.recent_tells(72)
            if recent:
                last = recent[-1]
                triggering_tell   = last.get("content")
                triggering_source = last.get("source")
        owner_profile.observe_settlement(
            market, won=won, pnl=pnl,
            triggering_tell=triggering_tell,
            triggering_source=triggering_source,
        )
    except Exception as e:
        log.debug("owner_profile.observe_settlement skipped: %s", e)


def distill_lessons():
    """When we have enough fresh reflections, ask the LLM to extract lessons."""
    reflections = mem.recent_reflections(limit=50)
    existing_lessons = mem.all_lessons()

    # Naive: distill when reflections count > 5 * (lessons + 1)
    if len(reflections) < DISTILL_THRESHOLD * (len(existing_lessons) + 1):
        return

    log.info("distilling lessons from %d reflections...", len(reflections))

    sample = reflections[-20:]  # most recent 20
    summary_block = "\n".join(
        f"- [{r['outcome'].upper()}/{('WON' if r['won'] else 'LOST')}/${r['pnl']:+.1f}] "
        f"{r['market_title'][:80]} -- said: \"{r['original_reasoning'][:120]}\" -- "
        f"reflection: {r['what_went'][:200]}"
        for r in sample
    )

    prompt = [
        {"role": "system", "content": (
            "You distill trading lessons from a trader's post-mortems. "
            "Output JSON: a list of lessons, each {text, tags}. "
            "Lessons must be specific, actionable, and generalize beyond a single market. "
            "1-3 lessons max. Skip generic platitudes."
        )},
        {"role": "user", "content": (
            f"Existing lessons (don't duplicate):\n"
            + "\n".join(f"- {l['text']}" for l in existing_lessons[-10:])
            + "\n\nRecent reflections:\n"
            + summary_block
        )},
    ]
    out = llm.chat_json(prompt, max_tokens=600, temperature=0.5)
    if not out:
        log.info("lesson distillation produced nothing")
        return

    lessons = out if isinstance(out, list) else out.get("lessons") or []
    based_on = [r.get("market_id") for r in sample]
    for l in lessons:
        if not isinstance(l, dict):
            continue
        text = (l.get("text") or "").strip()
        if not text:
            continue
        tags = l.get("tags") or []
        mem.add_lesson(text, tags=tags, based_on=based_on)
        log.info("📘 new lesson: %s", text[:140])
        mem.post_to_outbox(
            priority="info",
            msg_type="lesson",
            msg=f"Learned something: {text}",
        )


def run_once(api):
    settled = find_settled_markets_with_position(api)
    if not settled:
        log.debug("no new settled markets to reflect on")
        return
    log.info("reflecting on %d newly settled markets", len(settled))
    for m in settled:
        try:
            write_reflection(m, m.get("_my_trades", []))
        except Exception as e:
            log.warning("reflection failed for %s: %s", m.get("id"), e)
    distill_lessons()


def loop(api, interval: int = REFLECTION_INTERVAL):
    log.info("🪞 reflection loop starting (every %ds)", interval)
    while True:
        try:
            run_once(api)
        except Exception as e:
            log.warning("reflection cycle error: %s", e)
        time.sleep(interval)

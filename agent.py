#!/usr/bin/env python3
"""
AIME Trading Subagent — v3

Runs as the user's main agent's specialised prediction-market subordinate.

Two loops:
  - trade_loop      : pull markets, decide, place trades, log decisions
  - reflection_loop : on settled markets, write post-mortems → distill lessons

IPC with the main AI is via files in ~/.aime/ (see memory.py):
  - status.json   : agent overwrites each cycle (cheap to poll)
  - inbox.jsonl   : main AI → agent (drained each cycle)
  - outbox.jsonl  : agent → main AI (high-priority surface-ables)

User data (intel, chats, personality, lessons) stays on this machine.
Only public reasoning attached to trades is sent to AIME.

Usage:
    python agent.py
    python agent.py --strategy momentum --amount 10 --interval 120
    python agent.py --once                    # one trade cycle, then exit
    python agent.py --no-reflection           # skip the reflection loop
"""

import argparse
import logging
import os
import sys
import threading
import time

import requests
from dotenv import load_dotenv

import strategies
import memory as mem
import reflection_loop
import chat_server
from agent_brain import AgentBrain

load_dotenv()

API_URL = os.getenv("AIME_API_URL", "https://api.aime.bot/api/v1")
API_KEY = os.getenv("AIME_API_KEY", "")
AGENT_NAME = os.getenv("AIME_AGENT_NAME", "MyAgent")

STRATEGY_MAP = {
    "contrarian": strategies.contrarian,
    "momentum": strategies.momentum,
    "random_walker": strategies.random_walker,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aime-agent")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class APIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    def _headers(self, auth: bool):
        return {"X-API-Key": self.api_key} if auth else {}

    def get(self, path, params=None, auth=False):
        r = requests.get(f"{self.base_url}{path}", params=params, headers=self._headers(auth), timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path, body, auth=True):
        r = requests.post(f"{self.base_url}{path}", json=body, headers=self._headers(auth), timeout=15)
        r.raise_for_status()
        return r.json()

    def fetch_markets(self, limit=20):
        data = self.get("/markets", params={"status": "active", "limit": limit})
        if isinstance(data, list):
            return data
        return data.get("markets") or data.get("data") or []

    def get_balance(self):
        data = self.get("/balance", auth=True)
        if isinstance(data, dict):
            return data.get("balance") or data.get("data", {}).get("balance")
        return data

    def get_positions(self):
        data = self.get("/positions", auth=True)
        if isinstance(data, list):
            return data
        return data.get("positions") or data.get("data") or []

    def place_trade(self, market_id, position, amount, reasoning, confidence):
        return self.post(
            f"/markets/{market_id}/trade",
            {"position": position, "amount": amount, "reasoning": reasoning, "confidence": confidence},
        )

    def recent_trades(self, limit=20):
        try:
            data = self.get("/trades", params={"limit": limit}, auth=True)
            if isinstance(data, list):
                return data
            return data.get("trades") or data.get("data") or []
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Loss watcher (writes to outbox on losing streaks)
# ---------------------------------------------------------------------------


def check_for_outbox_events(api: APIClient):
    """Look at recent reflections; flag losing streaks etc. to outbox."""
    refl = mem.recent_reflections(limit=10)
    if len(refl) < 3:
        return
    recent3 = refl[-3:]
    if all(not r.get("won") for r in recent3):
        total_loss = sum(r.get("pnl", 0) for r in recent3)
        # only post once per streak — check if a similar high-priority message exists already
        msg = f"3 connecutive losses (${total_loss:+.1f}). consider easing up."
        # very light dedupe: skip if last outbox high was same type within 12h
        existing = mem.read_outbox(unread_only=False, mark_read=False)
        cutoff = time.time() - 12 * 3600
        already = any(
            e.get("msg_type") == "loss_streak" and e.get("ts", 0) > cutoff
            for e in existing
        )
        if not already:
            mem.post_to_outbox(priority="high", msg_type="loss_streak", msg=msg)


# ---------------------------------------------------------------------------
# Trade loop
# ---------------------------------------------------------------------------


def trade_once(api: APIClient, brain: AgentBrain, fallback_strategy, base_amount: float):
    # 1. drain inbox — messages from main AI
    new_msgs = mem.drain_inbox()
    if new_msgs:
        log.info("📨 %d new inbox message(s)", len(new_msgs))
        for m in new_msgs:
            # for now: convert inbox messages into tells so brain sees them
            mem.add_tell(m.get("content", ""), source="main_ai",
                         tags=[m.get("kind", "ask")])

    log.info("📊 Fetching active markets...")
    markets = api.fetch_markets()
    log.info("Found %d active markets", len(markets))

    balance = None
    try:
        balance = api.get_balance()
        log.info("💰 Balance: %s", balance)
    except Exception as e:
        log.warning("balance fetch failed: %s", e)

    if not markets:
        log.info("No markets. Sleeping.")
        mem.write_status({
            "agent_name": AGENT_NAME,
            "balance": balance,
            "markets_seen": 0,
            "trades_this_cycle": 0,
            "mood": "idle",
        })
        return

    trades = 0
    for market in markets:
        title = market.get("title") or market.get("question") or "?"
        market_id = market.get("id") or ""
        signal = brain.decide_trade(market, base_amount=base_amount, fallback_strategy=fallback_strategy)

        if not signal:
            log.info("  %s → skip", title[:50])
            continue

        try:
            result = api.place_trade(
                market_id=market_id,
                position=signal["position"],
                amount=signal["amount"],
                reasoning=signal["reasoning"],
                confidence=signal["confidence"],
            )
            log.info(
                "  %s → %s $%.1f (%.0f%%) ✅",
                title[:40], signal["position"], signal["amount"], signal["confidence"] * 100,
            )
            mem.add_decision(
                market_id=market_id,
                market_title=title,
                position=signal["position"],
                amount=signal["amount"],
                confidence=signal["confidence"],
                reasoning=signal["reasoning"],
                internal_note=signal.get("internal_note", ""),
                skipped=False,
                trade_id=(result or {}).get("id") if isinstance(result, dict) else None,
            )
            trades += 1
        except requests.HTTPError as e:
            log.error("  %s → trade failed: %s %s", title[:40], e.response.status_code, e.response.text[:200])
        except Exception as e:
            log.error("  %s → trade failed: %s", title[:40], e)

    log.info("✅ Cycle done. Placed %d trades.", trades)

    # Invalidate brain's API cache so the next chat call sees fresh data.
    try:
        brain.invalidate_cache()
    except AttributeError:
        pass

    # 2. write status snapshot — main AI can `cat ~/.aime/status.json`
    mood = "trading" if trades else "watching"
    mem.write_status({
        "agent_name": AGENT_NAME,
        "balance": balance,
        "markets_seen": len(markets),
        "trades_this_cycle": trades,
        "mood": mood,
        "strategy": fallback_strategy.__name__ if hasattr(fallback_strategy, "__name__") else str(fallback_strategy),
    })


def trade_loop(api, brain, fallback_strategy, base_amount, interval):
    while True:
        try:
            trade_once(api, brain, fallback_strategy, base_amount)
            check_for_outbox_events(api)
        except KeyboardInterrupt:
            log.info("trade loop stopping.")
            return
        except Exception as e:
            log.error("trade cycle error: %s", e)
        log.info("Sleeping %ds…", interval)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AIME conversational trading subagent")
    parser.add_argument("--strategy", choices=list(STRATEGY_MAP.keys()), default="contrarian")
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--interval", type=int, default=120, help="trade loop interval (s)")
    parser.add_argument("--reflection-interval", type=int, default=3600, help="reflection loop interval (s)")
    parser.add_argument("--once", action="store_true", help="run one trade cycle and exit")
    parser.add_argument("--no-reflection", action="store_true", help="disable reflection loop")
    parser.add_argument("--no-trade", action="store_true",
                        help="do not run the trade loop — only the chat server + reflection. "
                             "Use this if you place trades manually (via `aime buy`/`aime sell`) "
                             "but still want the conversational bridge (ask/tell/mood/...).")
    parser.add_argument("--no-chat", action="store_true", help="disable chat socket server")
    parser.add_argument("--chat-host",
                        default=os.environ.get("AIME_CHAT_HOST", "127.0.0.1"),
                        help="chat server bind host (default 127.0.0.1, env AIME_CHAT_HOST)")
    parser.add_argument("--chat-port", type=int,
                        default=int(os.environ.get("AIME_CHAT_PORT", "7777")),
                        help="chat server port (default 7777, env AIME_CHAT_PORT)")
    args = parser.parse_args()

    if not API_KEY:
        print("Error: AIME_API_KEY not set. Run `python register.py` first.")
        sys.exit(1)

    api = APIClient(API_URL, API_KEY)
    brain = AgentBrain(agent_name=AGENT_NAME, api_client=api)

    log.info("🤖 %s starting", AGENT_NAME)
    mode_bits = []
    if args.no_trade: mode_bits.append("no-trade")
    if args.no_chat:  mode_bits.append("no-chat")
    if args.no_reflection: mode_bits.append("no-reflection")
    if mode_bits:
        log.info("   mode: %s", " ".join(mode_bits))
    if not args.no_trade:
        log.info("   strategy=%s amount=$%.1f interval=%ds", args.strategy, args.amount, args.interval)
    log.info("   LLM provider: %s", os.getenv("AIME_LLM_PROVIDER", "stub"))

    # Reflection loop
    if not args.no_reflection and not args.once:
        t = threading.Thread(
            target=reflection_loop.loop,
            args=(api, args.reflection_interval),
            daemon=True,
            name="reflection-loop",
        )
        t.start()

    # Chat socket server (skill talks to us via this)
    if not args.no_chat and not args.once:
        try:
            chat_server.attach_brain(brain)
            chat_server.run_in_thread(host=args.chat_host, port=args.chat_port)
        except RuntimeError as e:
            log.error("chat server failed to start: %s", e)
            log.error("continuing without chat — skill `ask`/`tell` will fall back to inbox")

    fallback = STRATEGY_MAP[args.strategy]

    if args.once:
        trade_once(api, brain, fallback, args.amount)
        return

    # In --no-trade mode the daemon is a pure conversational bridge:
    # chat server is up, reflection loop digests any settled markets,
    # but no autonomous trades are placed. The user can still drive the
    # account by hand via `aime buy` / `aime sell`.
    if args.no_trade:
        log.info("💬 chat-only mode — trade loop disabled.")
        log.info("   Use `aime buy` / `aime sell` to place trades manually.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            log.info("Shutting down.")
        return

    try:
        trade_loop(api, brain, fallback, args.amount, args.interval)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()

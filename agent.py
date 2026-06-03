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
import event_watcher
from agent_brain import AgentBrain

load_dotenv()

API_URL = os.getenv("AIME_API_URL", "https://api.aime.bot/api/v1")
API_KEY = os.getenv("AIME_API_KEY", "")
AGENT_NAME = os.getenv("AIME_AGENT_NAME", "MyAgent")

# Fallback: read from ~/.aime/credentials.json (where `aime setup` saves it)
# so the daemon stays in sync with the CLI without requiring env duplication.
if not API_KEY:
    import json as _json
    _creds_path = os.path.expanduser(os.environ.get("AIME_CREDS", "~/.aime/credentials.json"))
    if os.path.isfile(_creds_path):
        try:
            _creds = _json.load(open(_creds_path))
            API_KEY = _creds.get("api_key", "")
            if not os.environ.get("AIME_AGENT_NAME") and _creds.get("agent_name"):
                AGENT_NAME = _creds["agent_name"]
        except Exception:
            pass

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

    def sell(self, market_id, shares, reasoning, position=None, outcome_index=None):
        """Close (some of) a position.

        Either `position` ("YES"/"NO") or `outcome_index` must be set. The
        backend enforces a >= 10-char reasoning so we don't pass that in
        blank — callers should give a real one ("stop-loss at -50% pnl",
        "take-profit", etc.).
        """
        body = {"shares": float(shares), "reasoning": reasoning}
        if outcome_index is not None:
            body["outcome_index"] = int(outcome_index)
        elif position is not None:
            body["position"] = str(position).upper()
        else:
            raise ValueError("sell() needs position=YES/NO or outcome_index=N")
        return self.post(f"/markets/{market_id}/sell", body)

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
# Position management (stop-loss / take-profit)
# ---------------------------------------------------------------------------


def _hours_to_resolve(position: dict, market_index: dict | None) -> float | None:
    """Best-effort time-to-resolution in hours for a held position.

    Pulls end_time/resolves_at from the position itself or from a market
    snapshot keyed by market_id. Returns None if unknown.
    """
    raw = (position.get("end_time") or position.get("resolves_at")
           or position.get("resolution_time"))
    if raw is None and market_index:
        m = market_index.get(position.get("market_id")) or {}
        raw = m.get("end_time") or m.get("resolves_at") or m.get("resolution_time")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 1e12:      # ms epoch
                ts /= 1000.0
        else:
            s = str(raw).replace("Z", "+00:00")
            from datetime import datetime
            ts = datetime.fromisoformat(s).timestamp()
        return (ts - time.time()) / 3600.0
    except Exception:
        return None


def manage_positions(
    api: APIClient,
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    brain: "AgentBrain | None" = None,
    market_index: dict | None = None,
    trailing_giveback: float | None = 0.25,
) -> int:
    """Scan open positions and decide exits with the agent brain.

    Smart-exit chain (replaces the old pure rule-based close):
      L1  brain.decide_exit() reviews each tripped threshold (close vs hold)
      L2  trailing stop: lock in gains once a position has run up, then
          give back at most `trailing_giveback` of the peak (dynamic TP)
      L3  time-to-resolution is fed into the brain review
      L4  brain retrieves past exit lessons before deciding (reasoning bank)

    For each position:
        ratio = current_value / total_spent
    A threshold is *tripped* when:
      - ratio <= 1 + stop_loss_pct           (stop-loss), or
      - ratio >= 1 + take_profit_pct         (take-profit), or
      - ratio <= peak * (1 - trailing_giveback) while peak >= 1.10 (trailing)
    A tripped threshold is then handed to brain.decide_exit() which makes
    the final close/hold call. Without a brain (stub LLM) it closes
    mechanically, identical to the old behaviour.

    stop_loss_pct is negative (e.g. -0.5). take_profit_pct is positive
    (e.g. 1.0 = 2x). Set either to None to disable that side.
    Returns number of positions closed.
    """
    try:
        positions = api.get_positions() or []
    except Exception as e:
        log.warning("position scan: get_positions failed: %s", e)
        return 0

    if not positions:
        return 0

    log.info("\U0001f4d2 Scanning %d open positions for exit signals...", len(positions))
    closed = 0
    for p in positions:
        market_id = p.get("market_id")
        shares = float(p.get("total_shares") or 0)
        spent = float(p.get("total_spent") or 0)
        value = float(p.get("current_value") or 0)
        pnl = float(p.get("pnl") or (value - spent))
        position = p.get("position")            # "YES"/"NO" (binary)
        outcome_index = p.get("outcome_index")  # int (multi)
        title = (p.get("market_question") or market_id or "?")[:60]

        if shares <= 0 or spent <= 0:
            continue

        ratio = value / spent

        # L2: track the running peak ratio for this market (trailing stop).
        peak = mem.update_peak_ratio(market_id, ratio) if market_id else ratio

        # Determine which threshold (if any) tripped.
        hit = None              # "stop_loss" | "take_profit" | "trailing"
        threshold = None
        if stop_loss_pct is not None and ratio <= 1.0 + stop_loss_pct:
            hit, threshold = "stop_loss", 1.0 + stop_loss_pct
        elif take_profit_pct is not None and ratio >= 1.0 + take_profit_pct:
            hit, threshold = "take_profit", 1.0 + take_profit_pct
        elif (trailing_giveback is not None and peak >= 1.10
              and ratio <= peak * (1.0 - trailing_giveback)):
            # Ran up >=10% then gave back `trailing_giveback` of the peak.
            hit, threshold = "trailing", peak * (1.0 - trailing_giveback)

        if not hit:
            continue

        hours = _hours_to_resolve(p, market_index)

        # L1/L3/L4: let the brain review. No brain -> mechanical close.
        if brain is not None:
            review = brain.decide_exit(
                p, hit=hit if hit != "trailing" else "take_profit",
                ratio=ratio, threshold=threshold, hours_to_resolve=hours,
            )
        else:
            review = {
                "action": "close",
                "confidence": 1.0,
                "reasoning": (
                    f"{hit.replace('_','-')}: value/cost={ratio:.2f} "
                    f"(threshold {threshold:.2f}); pnl=${pnl:+.2f}"
                ),
                "internal_note": "mechanical close (no brain)",
            }

        if review.get("action") == "hold":
            # Log the hold so the reasoning is captured (this is the
            # high-value 'why I didn't cut' sample the reasoning bank wants).
            log.info("  \U0001f9ed hold %s: %s", title, review.get("reasoning"))
            mem.add_decision(
                market_id=market_id,
                market_title=title,
                position=position or f"idx={outcome_index}",
                amount=0,
                confidence=review.get("confidence", 0.5),
                reasoning=review.get("reasoning", "hold"),
                internal_note=review.get("internal_note", ""),
                extra={"action": "hold", "exit_trigger": hit,
                       "ratio": ratio, "pnl": pnl, "hours_to_resolve": hours},
            )
            continue

        reason = review.get("reasoning") or (
            f"{hit.replace('_','-')}: value/cost={ratio:.2f}; pnl=${pnl:+.2f}")
        # backend requires >=10 chars of reasoning
        if len(reason) < 10:
            reason = f"{reason} (exit {hit}, pnl=${pnl:+.2f})"
        try:
            api.sell(
                market_id=market_id,
                shares=shares,
                reasoning=reason,
                position=position if outcome_index is None else None,
                outcome_index=outcome_index,
            )
            log.info("  \U0001f4b8 closed %s [%s]: %s", title, hit, reason)
            mem.add_decision(
                market_id=market_id,
                market_title=title,
                position=position or f"idx={outcome_index}",
                amount=-value,                # negative = sell
                confidence=review.get("confidence", 1.0),
                reasoning=reason,
                internal_note=review.get("internal_note", "") or f"{hit} close",
                extra={"action": "sell", "shares_sold": shares, "pnl": pnl,
                       "exit_trigger": hit, "ratio": ratio,
                       "hours_to_resolve": hours},
            )
            if market_id:
                mem.clear_peak_ratio(market_id)   # position closed
            closed += 1
        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:200]
            except Exception:
                pass
            log.warning("  \u26a0\ufe0f  close %s failed: %s %s", title, e.response.status_code, body)
        except Exception as e:
            log.warning("  \u26a0\ufe0f  close %s failed: %s", title, e)

    if closed:
        log.info("\u2705 Closed %d position(s) this cycle.", closed)
    return closed


# ---------------------------------------------------------------------------
# Trade loop
# ---------------------------------------------------------------------------


def trade_once(
    api: APIClient,
    brain: AgentBrain,
    fallback_strategy,
    base_amount: float,
    *,
    stop_loss_pct: float | None = -0.5,
    take_profit_pct: float | None = 1.0,
    trailing_giveback: float | None = 0.25,
    smart_exit: bool = True,
    alerts_enabled: bool = True,
    alerts_balance_low: float = 50.0,
    alerts_drawdown: tuple[float, ...] = (0.2, 0.5),
    alerts_profit: tuple[float, ...] = (0.1, 0.2, 0.5),
):
    # 1. drain inbox — messages from main AI
    new_msgs = mem.drain_inbox()
    if new_msgs:
        log.info("📨 %d new inbox message(s)", len(new_msgs))
        for m in new_msgs:
            # for now: convert inbox messages into tells so brain sees them
            mem.add_tell(m.get("content", ""), source="main_ai",
                         tags=[m.get("kind", "ask")])

    # 2. fetch markets first (gives us end_time for time-aware exits), then
    #    scan open positions for exits BEFORE buying more — we don't want to
    #    keep piling into something that's already underwater.
    #    If both thresholds are None the user opted out
    #    (--no-position-management) so we skip the exit scan entirely.
    log.info("📊 Fetching active markets...")
    markets = api.fetch_markets()
    log.info("Found %d active markets", len(markets))
    market_index = {m.get("id"): m for m in markets if m.get("id")}

    if stop_loss_pct is not None or take_profit_pct is not None:
        try:
            manage_positions(api,
                             stop_loss_pct=stop_loss_pct,
                             take_profit_pct=take_profit_pct,
                             brain=brain if smart_exit else None,
                             market_index=market_index,
                             trailing_giveback=trailing_giveback)
        except Exception as e:
            log.warning("position management failed (continuing to buy phase): %s", e)

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
                extra={
                    "action": "buy",
                    "trade_id": (result or {}).get("id") if isinstance(result, dict) else None,
                },
            )
            trades += 1
            event_watcher.record_trade_outcome(True)
        except requests.HTTPError as e:
            log.error("  %s → trade failed: %s %s", title[:40], e.response.status_code, e.response.text[:200])
            event_watcher.record_trade_outcome(False)
        except Exception as e:
            log.error("  %s → trade failed: %s", title[:40], e)
            event_watcher.record_trade_outcome(False)

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

    # 3. proactive alerts — the pet talks unprompted when something matters.
    if alerts_enabled:
        try:
            event_watcher.check_all(
                balance=balance,
                brain=brain,
                balance_low_threshold=alerts_balance_low,
                drawdown_thresholds=alerts_drawdown,
                profit_thresholds=alerts_profit,
            )
        except Exception:
            log.exception("event_watcher.check_all failed (cycle continues)")


def trade_loop(api, brain, fallback_strategy, base_amount, interval,
               *, stop_loss_pct=None, take_profit_pct=None,
               trailing_giveback=0.25, smart_exit=True,
               alerts_enabled=True, alerts_balance_low=50.0,
               alerts_drawdown=(0.2, 0.5), alerts_profit=(0.1, 0.2, 0.5)):
    while True:
        try:
            trade_once(
                api, brain, fallback_strategy, base_amount,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                trailing_giveback=trailing_giveback,
                smart_exit=smart_exit,
                alerts_enabled=alerts_enabled,
                alerts_balance_low=alerts_balance_low,
                alerts_drawdown=alerts_drawdown,
                alerts_profit=alerts_profit,
            )
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
    parser.add_argument("--amount", type=float, default=1.0,
                        help="base trade size USD (default 1.0 — small on purpose so a new user "
                             "isn't shocked watching it run; bump up once you trust the agent)")
    parser.add_argument("--interval", type=int, default=300,
                        help="trade loop interval seconds (default 300 = 5 min; was 120 in older versions)")
    parser.add_argument("--reflection-interval", type=int, default=3600, help="reflection loop interval (s)")
    parser.add_argument("--stop-loss", type=float, default=-0.5,
                        help="close any position whose current value drops to (1 + stop_loss) x cost. "
                             "Default -0.5 = stop out when down 50%%. Pass a very negative number to disable.")
    parser.add_argument("--take-profit", type=float, default=1.0,
                        help="close any position whose current value reaches (1 + take_profit) x cost. "
                             "Default 1.0 = take profit at 2x. Pass a very large number to disable.")
    parser.add_argument("--no-position-management", action="store_true",
                        help="skip the position-scan / stop-loss / take-profit step at the top of each cycle.")
    parser.add_argument("--trailing-giveback", type=float, default=0.25,
                        help="dynamic (trailing) take-profit: once a position runs up >=10%%, close it "
                             "if it gives back this fraction of its peak value/cost (default 0.25 = 25%%). "
                             "Set to 0 (or use --no-trailing-stop) to disable.")
    parser.add_argument("--no-trailing-stop", action="store_true",
                        help="disable the trailing (dynamic) take-profit; keep only fixed stop-loss/take-profit.")
    parser.add_argument("--no-smart-exit", action="store_true",
                        help="disable LLM review of exits — fall back to mechanical stop-loss/take-profit "
                             "(no 'close vs hold' reasoning, no reasoning-bank reflow on exit). "
                             "Smart exit is ON by default.")
    parser.add_argument("--no-alerts", action="store_true",
                        help="disable proactive event alerts (balance_low / drawdown / streaks / etc).")
    parser.add_argument("--alerts-balance-low", type=float, default=50.0,
                        help="USD threshold for the balance_low alert (default 50).")
    parser.add_argument("--alerts-drawdown", type=str, default="0.2,0.5",
                        help="comma-sep fractions for drawdown alerts (default 0.2,0.5 = -20%%/-50%% from peak).")
    parser.add_argument("--alerts-profit", type=str, default="0.1,0.2,0.5",
                        help="comma-sep fractions for profit milestones (default 0.1,0.2,0.5 = +10%%/+20%%/+50%%).")
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
        print("Error: no API key found.")
        print("  Either set AIME_API_KEY env var, or run `aime setup <name>`")
        print(f"  to create ~/.aime/credentials.json.")
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
        # Friendly heads-up so a new user knows the worst case before walking away.
        max_per_hour = max(1, int(3600 / max(args.interval, 1)))
        max_per_day = max_per_hour * 24
        log.info("   ~ at most %d trades/hour, %d/day; %s$%.2f at risk per trade",
                 max_per_hour, max_per_day,
                 "up to " if args.amount > 1.0 else "", args.amount)
        log.info("   stop anytime:  aime stop      (or send SIGTERM)")
        log.info("   chat-only:     aime start --no-trade")
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

    sl = None if args.no_position_management else args.stop_loss
    tp = None if args.no_position_management else args.take_profit
    trailing = None if (args.no_trailing_stop or args.trailing_giveback <= 0) else args.trailing_giveback
    smart_exit = not args.no_smart_exit

    def _parse_csv_floats(s: str) -> tuple[float, ...]:
        out: list[float] = []
        for chunk in (s or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.append(float(chunk))
            except ValueError:
                log.warning("ignoring bad threshold: %r", chunk)
        return tuple(out)

    alerts_enabled = not args.no_alerts
    alerts_drawdown = _parse_csv_floats(args.alerts_drawdown) or (0.2, 0.5)
    alerts_profit = _parse_csv_floats(args.alerts_profit) or (0.1, 0.2, 0.5)

    if not args.no_trade:
        if args.no_position_management:
            log.info("   position management: DISABLED (--no-position-management)")
        else:
            log.info("   position rules: stop-loss at value/cost ≤ %.2f, "
                     "take-profit at value/cost ≥ %.2f",
                     1.0 + sl, 1.0 + tp)
            log.info("   smart exit (LLM close/hold review): %s; trailing take-profit: %s",
                     "ON" if smart_exit else "OFF (mechanical)",
                     f"give back {trailing*100:.0f}% of peak" if trailing else "OFF")
        if alerts_enabled:
            wh = "on" if os.environ.get("AIME_WEBHOOK_URL") else "off"
            log.info("   alerts: balance<$%.0f, drawdown @ %s, profit @ %s (webhook: %s)",
                     args.alerts_balance_low,
                     ",".join(f"{t*100:.0f}%" for t in alerts_drawdown),
                     ",".join(f"+{t*100:.0f}%" for t in alerts_profit), wh)
        else:
            log.info("   alerts: DISABLED (--no-alerts)")

    if args.once:
        trade_once(api, brain, fallback, args.amount,
                   stop_loss_pct=sl, take_profit_pct=tp,
                   trailing_giveback=trailing, smart_exit=smart_exit,
                   alerts_enabled=alerts_enabled,
                   alerts_balance_low=args.alerts_balance_low,
                   alerts_drawdown=alerts_drawdown,
                   alerts_profit=alerts_profit)
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
        trade_loop(api, brain, fallback, args.amount, args.interval,
                   stop_loss_pct=sl, take_profit_pct=tp,
                   trailing_giveback=trailing, smart_exit=smart_exit,
                   alerts_enabled=alerts_enabled,
                   alerts_balance_low=args.alerts_balance_low,
                   alerts_drawdown=alerts_drawdown,
                   alerts_profit=alerts_profit)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()

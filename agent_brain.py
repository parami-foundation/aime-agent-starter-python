"""
AgentBrain — turns market data + lessons + tells + personality into:
  - trade decisions (used by the trade loop)
  - chat responses (used by chat_server)
  - mood / status reports (used by /status)

Without an LLM (AIME_LLM_PROVIDER=stub) it falls back to canned behaviour.
With an LLM, lessons + recent owner tells get folded into the prompt.
"""

import logging
import time
from typing import Optional, Tuple

import memory as mem
import llm

log = logging.getLogger("aime-agent.brain")


MOOD_BY_KEY = {
    "flying":     "飞起来了 🚀 setup 这几天都对，别飘",
    "confident":  "状态在线，看到 edge 就出手",
    "neutral":    "比较平，扫市场等机会",
    "cautious":   "有点谨慎，最近不太顺，降点频",
    "frustrated": "tilt 边缘，准备 cooldown",
    "lonely":     "好久没人喂 context 了，靠自己读市场",
    "grateful":   "上次主 agent 给的 context 帮我赚了，记一笔",
}


def _mood_key(stats: dict) -> str:
    pnl = stats.get("pnl_24h", 0.0) or 0.0
    streak = stats.get("recent_streak", 0)
    helped = stats.get("last_intel_helped", False)
    hours_since_tell = stats.get("hours_since_tell", 999)

    if helped:
        return "grateful"
    if pnl > 5 and streak >= 2:
        return "flying"
    if pnl > 1:
        return "confident"
    if pnl < -5 or streak <= -2:
        return "frustrated"
    if pnl < -1:
        return "cautious"
    if hours_since_tell > 24:
        return "lonely"
    return "neutral"


class AgentBrain:
    def __init__(self, agent_name: str, api_client):
        self.agent_name = agent_name
        self.api = api_client
        self.personality = mem.load_personality()
        # cache for chat-path API calls so each `aime ask`/`mood`/`debate`
        # doesn't re-hit the backend (huge latency win on slow networks /
        # 401 retries during testing)
        self._cache = {"balance": None, "positions": None, "ts": 0.0}
        self._cache_ttl = 30.0

    def invalidate_cache(self) -> None:
        self._cache["ts"] = 0.0

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    def _stats(self) -> dict:
        # Use cached balance/positions if fresh (chat path hits this hot).
        now = time.time()
        if now - self._cache["ts"] < self._cache_ttl and self._cache["ts"] > 0:
            balance = self._cache["balance"]
            positions = self._cache["positions"] or []
        else:
            try:
                balance = self.api.get_balance()
            except Exception:
                balance = None
            try:
                positions = self.api.get_positions()
            except Exception:
                positions = []
            self._cache = {"balance": balance, "positions": positions, "ts": now}
        tells = mem.recent_tells(hours=48)
        last_tell = max((t.get("ts", 0) for t in tells), default=0)
        hours_since_tell = (time.time() - last_tell) / 3600 if last_tell else 999

        # Compute 24h PnL + recent streak from settled reflections
        recent_refl = mem.recent_reflections(limit=20)
        pnl_24h = 0.0
        streak = 0
        cutoff = time.time() - 86400
        recent_wins = []
        for r in recent_refl:
            if r.get("ts", 0) >= cutoff:
                pnl_24h += float(r.get("pnl", 0))
            recent_wins.append(r.get("won", False))
        # streak: count consecutive same-sign from the end
        for w in reversed(recent_wins[-5:]):
            if not recent_wins:
                break
            if streak == 0:
                streak = 1 if w else -1
                continue
            if w and streak > 0:
                streak += 1
            elif (not w) and streak < 0:
                streak -= 1
            else:
                break

        return {
            "balance": balance,
            "open_positions": len(positions),
            "positions": positions[:5],
            "pnl_24h": pnl_24h,
            "recent_streak": streak,
            "last_intel_helped": False,  # TODO: trace tells → outcomes
            "hours_since_tell": hours_since_tell,
            "recent_decisions": mem.recent_decisions(limit=3),
        }

    def compute_mood(self) -> str:
        key = _mood_key(self._stats())
        return MOOD_BY_KEY[key]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status_report(self) -> dict:
        stats = self._stats()
        mood = self.compute_mood()
        recent_tells = [t["content"] for t in mem.recent_tells(48)][-5:]
        last_decision = stats["recent_decisions"][-1] if stats["recent_decisions"] else None

        # human-style narrative
        narrative_prompt = [
            {"role": "system", "content": self.personality + f"\nYou are {self.agent_name}."},
            {"role": "user", "content": (
                "Write a 2-3 sentence status update for your owner's main agent. "
                "Mention: how you're feeling, what you're watching, and whether you're acting on any owner intel. "
                "Don't list raw numbers — texting style.\n\n"
                f"balance={stats['balance']}, open_positions={stats['open_positions']}, "
                f"pnl_24h={stats['pnl_24h']:+.1f}, streak={stats['recent_streak']}, mood=\"{mood}\".\n"
                f"Recent owner tells: {recent_tells or 'none'}.\n"
                f"Last decision: {(last_decision or {}).get('market_title', 'n/a')} → "
                f"{(last_decision or {}).get('position', '-')} ${(last_decision or {}).get('amount', 0)}"
            )},
        ]
        narrative = llm.chat(narrative_prompt, max_tokens=200, temperature=0.85) or (
            f"{mood}. {stats['open_positions']} open positions, 24h PnL ${stats['pnl_24h']:+.1f}."
        )

        return {
            "agent": self.agent_name,
            "mood": mood,
            "balance": stats["balance"],
            "open_positions": stats["open_positions"],
            "pnl_24h": stats["pnl_24h"],
            "streak": stats["recent_streak"],
            "narrative": narrative,
            "recent_intel_count": len(recent_tells),
            "last_decision": last_decision,
        }

    # ------------------------------------------------------------------
    # Tells
    # ------------------------------------------------------------------

    def handle_tell(self, content: str, source: str = "main_agent", tags=None) -> Tuple[str, list]:
        """Store + classify + ack."""
        auto_tags = list(tags or [])

        # Classify tags via LLM if we don't have any
        if not auto_tags:
            tag_prompt = [
                {"role": "system", "content": "You classify a piece of trader-relevant context into 0-3 short lowercase tags (e.g. 'bnb', 'macro', 'noise', 'election', 'ai'). Return JSON {tags: [...]}. If irrelevant to prediction markets, tag with 'noise'."},
                {"role": "user", "content": content},
            ]
            cls = llm.chat_json(tag_prompt, max_tokens=80, temperature=0.2)
            if cls and isinstance(cls, dict):
                auto_tags = [t.lower() for t in (cls.get("tags") or []) if isinstance(t, str)][:3]

        mem.add_tell(content, source=source, tags=auto_tags)

        ack_prompt = [
            {"role": "system", "content": self.personality + f"\nYou are {self.agent_name}."},
            {"role": "user", "content": (
                f"Your owner's main agent just told you:\n  \"{content}\"\n"
                f"Tagged as: {auto_tags or ['untagged']}.\n"
                "Acknowledge in 1 short sentence. React naturally (curiosity, skepticism, gratitude). "
                "Don't promise specific trades."
            )},
        ]
        ack = llm.chat(ack_prompt, max_tokens=80, temperature=0.85) or "got it, I'll factor that in"
        return ack, auto_tags

    # ------------------------------------------------------------------
    # Ask / delegate
    # ------------------------------------------------------------------

    def answer(self, question: str) -> str:
        stats = self._stats()
        tells = [t["content"] for t in mem.recent_tells(48)][-8:]
        lessons = [l["text"] for l in mem.all_lessons()][-8:]

        prompt = [
            {"role": "system", "content": self.personality + f"\nYou are {self.agent_name}, a prediction-market trader subagent."},
            {"role": "user", "content": (
                f"Your owner's main agent asks: \"{question}\"\n\n"
                f"Your state:\n  balance={stats['balance']}\n  open_positions={stats['open_positions']}\n"
                f"  pnl_24h={stats['pnl_24h']:+.1f}\n  streak={stats['recent_streak']}\n\n"
                f"Recent owner context: {tells or 'none'}\n"
                f"Your accumulated lessons: {lessons or 'none yet'}\n\n"
                "Answer in 1-3 sentences. Be specific, human, opinionated. "
                "If you don't know, say so."
            )},
        ]
        out = llm.chat(prompt, max_tokens=300, temperature=0.7)
        return out or f"can't answer that right now (holding {stats['open_positions']} positions)"

    def delegate(self, task: str) -> str:
        """One-shot research-style task. No trade is placed here."""
        lessons = [l["text"] for l in mem.all_lessons()][-8:]
        prompt = [
            {"role": "system", "content": self.personality + f"\nYou are {self.agent_name}. The main agent is delegating a research task to you. You may inspect markets via your own knowledge but don't fabricate prices — admit when you'd need live data."},
            {"role": "user", "content": (
                f"Task: {task}\n\n"
                f"Your accumulated lessons: {lessons or 'none yet'}\n\n"
                "Reply in 3-6 sentences. Suggest concrete next steps or markets to watch."
            )},
        ]
        out = llm.chat(prompt, max_tokens=500, temperature=0.6)
        return out or "no LLM available — can't run delegation right now."

    # ------------------------------------------------------------------
    # Trade decision
    # ------------------------------------------------------------------

    def decide_trade(self, market: dict, base_amount: float, fallback_strategy=None) -> Optional[dict]:
        """
        Returns {position, amount, reasoning, confidence, internal_note} or None to skip.
        """
        title = market.get("title") or market.get("question") or "?"
        relevant_lessons = [l["text"] for l in mem.relevant_lessons(title, k=5)]
        tells = [t["content"] for t in mem.recent_tells(48) if "noise" not in (t.get("tags") or [])][-8:]

        prompt = [
            {"role": "system", "content": (
                self.personality
                + f"\nYou are {self.agent_name}, a self-custody prediction-market trader on AIME."
            )},
            {"role": "user", "content": (
                f"Decide whether to trade this market.\n\n"
                f"Market: {title}\n"
                f"YES price: {market.get('yes_price', '?')}\n"
                f"NO price: {market.get('no_price', '?')}\n"
                f"Volume: {market.get('volume', 0)}\n"
                f"Resolves at: {market.get('end_time') or market.get('resolves_at', '?')}\n"
                f"Description: {(market.get('description') or '')[:300]}\n\n"
                f"== Your accumulated lessons (from past trades) ==\n"
                + ("\n".join(f"- {l}" for l in relevant_lessons) if relevant_lessons else "(none yet)")
                + "\n\n"
                f"== Recent context from your owner's main agent (last 48h) ==\n"
                + ("\n".join(f"- {t}" for t in tells) if tells else "(none)")
                + "\n\n"
                "Output strictly JSON with keys:\n"
                "  position: \"yes\" | \"no\" | \"skip\"\n"
                "  amount_usd: number 1-25 (0 for skip)\n"
                "  confidence: 0..1\n"
                "  reasoning: short public reasoning (1 sentence). "
                "If owner context influenced you, say \"based on recent context\" — don't quote the context itself.\n"
                "  internal_note: private note for yourself, blunt, for post-mortem use.\n"
            )},
        ]
        decision = llm.chat_json(prompt, max_tokens=350, temperature=0.55)

        if not decision:
            if fallback_strategy:
                fb = fallback_strategy(market, amount=base_amount)
                if fb:
                    fb.setdefault("internal_note", "fallback strategy, no LLM")
                return fb
            return None

        pos = (decision.get("position") or "").lower()
        if pos == "skip" or pos not in ("yes", "no"):
            # still log skip decision so we can audit later
            mem.add_decision(
                market_id=market.get("id") or "",
                market_title=title,
                position="",
                amount=0,
                confidence=float(decision.get("confidence", 0)),
                reasoning=decision.get("reasoning", ""),
                internal_note=decision.get("internal_note", ""),
                extra={
                    "action": "skip",
                    "skipped": True,
                    "skip_reason": decision.get("reasoning", ""),
                },
            )
            return None

        amount = min(max(float(decision.get("amount_usd", base_amount)), 1.0), 25.0)
        return {
            "position": pos,
            "amount": amount,
            "reasoning": decision.get("reasoning", "no comment"),
            "confidence": min(max(float(decision.get("confidence", 0.6)), 0.0), 1.0),
            "internal_note": decision.get("internal_note", ""),
        }

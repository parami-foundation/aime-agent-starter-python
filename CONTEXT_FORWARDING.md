# Feeding the daemon context from your other conversations

The AIME daemon trades better when it knows what's going on. By default
the only context it has is:

- Public market data (yes/no prices, volumes, trade counts)
- Its own past decisions (`~/.aime/decisions.jsonl`)
- Anything the user explicitly tells it via `aime tell`

The whole point of host integration is that **your main AI assistant
already sees a lot more than that** — alpha you mentioned in chat, news
you searched for, threads about a token, things another AI agent in
another session did. Most of that is wasted unless the host AI forwards
it to the daemon.

This file is the host-side protocol for **when to forward what**.

## TL;DR rule

When the user (or you, the host AI) say or read something **a trader
would want to know**, push it to the daemon:

```bash
aime tell "<summary in 1 sentence>" --source <where-it-came-from> --tags <topic>
```

The daemon stores it locally in `~/.aime/tells.jsonl` and pulls it into
the next trade-decision prompt. Tells that influence a winning trade
fire the `owner_intel_paid_off` event — measurable feedback for "was
that intel actually useful?".

## When to forward (Yes)

| User said / you saw | Forward as |
|---|---|
| "I think BTC is going down because $REASON" | `aime tell "owner bearish on BTC: $REASON" --source main_chat --tags btc` |
| Searched news about a regulator | `aime tell "SEC postponed BTC ETF decision to Aug" --source web --tags regulation btc` |
| Codex session: "Just deployed a CEX listing announcement" | `aime tell "$TOKEN getting CEX listing on Binance soon" --source codex --tags $token listings` |
| Twitter thread about Vitalik comment | `aime tell "Vitalik signalled L2 sequencer decentralization soon" --source twitter --tags ethereum l2` |
| Reading a leaked roadmap or call notes | `aime tell "Solana adding firedancer to mainnet Q3" --source private --tags solana` |
| User says "I'm bullish on AI agents" | `aime tell "owner bullish AI agents long-term" --source main_chat --tags ai agents` |

## When NOT to forward (No)

| Situation | Why not |
|---|---|
| Casual chat / banter | Daemon doesn't need it, polluting memory |
| Tells already covered (you forwarded yesterday) | Daemon already has it; check with `aime memory` first |
| User explicitly says "don't tell the agent this" | Respect it |
| Personal stuff unrelated to trading | Privacy. Tells live locally only, but still |
| Speculation without a thesis | "I dunno maybe BTC pumps" → no signal |

## When to ASK (vs tell)

Sometimes the host AI should defer to the daemon agent. Use `aime ask`:

| Situation | Ask the daemon |
|---|---|
| User asks "what does the agent think about X?" | `aime ask "what's your read on X?"` |
| Big trade decision (>5% of balance) about to happen | `aime ask "should I size up on $market_id?"` |
| User wants the agent's take on conflicting tells | `aime ask "owner says bullish but pumps look exhausted, what now?"` |
| Daily summary request | `aime ask "summarise your last 24h"` |

`aime ask` returns synchronously over the chat socket — the daemon's
brain gets to weigh in with its current PnL, mood, and memory. The host
AI then reports the daemon's answer to the user **without rewriting
it** (so the daemon's voice comes through). E.g.

> User: "What does your trading agent think about BTC right now?"
> Host AI: *runs `aime ask "btc thesis right now?"`*
> Host AI: "Agent says: $verbatim_answer"

## Multi-session: keeping daemon as single source of truth

If the user runs both a Claude Code session AND an OCPlatform session,
both should forward to the **same** daemon (`~/.aime/`). The daemon is
the bottleneck/aggregator — it sees everything once.

To avoid duplicate forwards:

1. Tell carries a `source` field. Daemon stores it. Before forwarding,
   the host AI can run `aime memory --hours 24` and check whether the
   same intel already came in.
2. The daemon also auto-tags by content keyword — repeated tells about
   the same topic stack rather than spam.

## Privacy

Tells live in `~/.aime/tells.jsonl` (chmod 600 by setup script) and
**never get uploaded as-is** to the AIME backend. When a tell influences
a trade, the public reasoning text reads "based on recent context" —
the actual content stays local.

This means: forwarding doesn't leak. But it's still polite to ask the
user before forwarding things from group chats / private DMs.

## How tells turn into edge

```
   you/user mentions alpha
              ↓
   host AI runs `aime tell ...`
              ↓
   daemon stores in ~/.aime/tells.jsonl
              ↓
   next trade cycle, daemon's prompt includes recent tells
              ↓
   if a tell-influenced trade closes green:
       daemon writes reflection: "intel_helped" → tells.jsonl row tagged
       event_watcher fires `owner_intel_paid_off`
              ↓
   `aime outbox` surfaces "💎 your call on $X paid off +$Y"
              ↓
   host AI tells the user
```

This loop is the whole point. **The daemon learns which sources are
right and which aren't** — over time, a `source: twitter:@vitalik` tell
might get higher weight than `source: random_friend` because the
former's tells settled green more often.

## Cheat sheet

```bash
# forward intel (the common case)
aime tell "your finding here" --source main_chat --tags topic1 topic2

# ask daemon a question synchronously
aime ask "what's your view on X?"

# see what the daemon remembers
aime memory --hours 24

# debug: what did daemon learn from this tell?
aime feed | head
```

That's the whole protocol. Forward when it's actionable, ask when the
agent should weigh in, otherwise stay out of the way.

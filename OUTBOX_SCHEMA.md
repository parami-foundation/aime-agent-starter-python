# AIME Daemon Outbox Schema

The daemon writes events to `~/.aime/outbox.jsonl` (JSONL, append-only) and
optionally POSTs the same row to `AIME_WEBHOOK_URL`. Both use the same
schema so any host can read either source.

## Row Schema (v1)

```jsonc
{
  "kind": "outbox",                  // always "outbox" for daemon events
  "id": "5e5bb0dbaa59",              // unique 12-hex-char ID
  "ts": 1779351720.439,              // unix epoch seconds
  "msg": "💎 +20% on BTC bet",       // human-readable message (LLM-flavoured if brain is configured)
  "short": "profit_milestone +20%",  // raw machine-friendly label
  "priority": "high",                // "high" | "info" | "low"
  "msg_type": "profit_milestone",    // canonical event type (see below)
  "read": false,                     // host-side bookkeeping (set true after delivery)
  // optional extras (varies by msg_type):
  "pnl": 215.40,
  "threshold": 0.20
}
```

## Canonical `msg_type` values

Priority colour codes show what daemons surface by default. Hosts may
re-prioritise.

| `msg_type` | Priority | When |
|---|---|---|
| `balance_low` | 🔴 high | Free balance < threshold (default $50) |
| `drawdown` | 🔴 high | Cumulative drawdown crosses -20% / -50% |
| `chain_error_rate` | 🔴 high | On-chain ops > 50% failing |
| `losing_streak` | 🟡 info | 3 consecutive stop-losses |
| `winning_streak` | 🟢 info | 3 consecutive wins |
| `profit_milestone` | 🟢 info | Cumulative PnL passes +10/+20/+50% |
| `market_settled` | 🟡 info | A market this agent traded just resolved |
| `owner_intel_paid_off` | 🟢 info | A `tell` the owner gave actually predicted right |
| `note` | ⚪ low | Generic note from agent (default for `post_to_outbox`) |
| `trade` | ⚪ low | Manual `aime tell --kind=trade` etc. (legacy) |

## Reading the outbox

### Via CLI (recommended for hosts)

```bash
aime outbox --json            # unread events, auto-marks read
aime outbox --json --all      # full history
aime outbox --json --no-mark  # peek without consuming
```

### Direct file read

```python
import json, pathlib
rows = [
    json.loads(line)
    for line in pathlib.Path("~/.aime/outbox.jsonl").expanduser().read_text().splitlines()
    if line.strip()
]
unread = [r for r in rows if not r.get("read")]
```

Host is responsible for setting `read: true` and rewriting the file if
it processed an event (`aime outbox --json` does this automatically).

## Webhook payload

If `AIME_WEBHOOK_URL` is set, every `high`-priority event is POSTed as
JSON with the same row schema (so a webhook subscriber doesn't need to
know anything about the file format).

```bash
export AIME_WEBHOOK_URL="https://your-bot.example.com/aime/event"
```

The webhook receives:
```json
{"kind":"outbox", "id":"...", "ts":..., "msg":"...", "short":"...",
 "priority":"high", "msg_type":"...", "read":false, ...}
```

## Stability

Schema is versioned implicitly by daemon version. Breaking changes will
ship under a new `kind` value (e.g. `kind: outbox.v2`). Hosts should
skip rows where `kind` is not `"outbox"`.

## Adding new event types

If you fork the daemon and add a new trigger:

1. Pick a new `msg_type` string (snake_case, e.g. `liquidation_warning`)
2. Call `mem.post_to_outbox(msg, priority="high", msg_type="liquidation_warning", extra={...})`
3. Document it in your fork's OUTBOX_SCHEMA.md

The CLI `aime outbox` doesn't validate `msg_type` — anything in the file
is shown. So custom event types just work.

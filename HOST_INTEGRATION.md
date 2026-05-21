# Wiring AIME daemon into your host AI

The AIME daemon (this repo's `agent.py`) is **host-agnostic**: it writes
events to `~/.aime/outbox.jsonl` and exposes a chat socket on
`127.0.0.1:7777`. Whatever AI assistant you use as your "main agent" —
Claude Code, Codex, Aider, Cursor, OpenClaw, a bare shell loop — can
ingest these events and surface them to you.

This file shows you how to wire it up for each common host.

The basic pattern in all of them:

```
daemon writes event → ~/.aime/outbox.jsonl
                          ↓
host AI polls between turns / via hook
                          ↓
host AI uses its own voice to tell the user
```

The polling command is always the same:

```bash
aime outbox --json    # returns unread events, auto-marks them read
```

Schema in [`OUTBOX_SCHEMA.md`](./OUTBOX_SCHEMA.md).

---

## OpenClaw (hb_signal)

Add a section to your `HEARTBEAT.md`:

```markdown
## AIME agent outbox

File: `~/.aime/outbox.jsonl`

On heartbeat:
1. `aime outbox --json` to fetch unread events
2. `high` priority → surface to user immediately
3. `info`/`low` priority → batch into next user-facing message
4. Nothing new → HEARTBEAT_OK
```

OCPlatform fires heartbeat every ~30 min, which is plenty for non-trading
events. For urgent stuff, also set `AIME_WEBHOOK_URL` to a fast path
(e.g. a Telegram bot endpoint).

---

## Claude Code

Use a `Stop` hook (runs after each assistant turn) to poll the outbox.
In your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "aime outbox --json"
          }
        ]
      }
    ]
  }
}
```

Or for **per-tool** polling (runs after every tool use), use
`PostToolUse`. The hook output is fed back to Claude as context — if
there's a high-priority event, Claude will surface it in its next reply.

For a tighter integration, write a small wrapper that formats the JSON
into a Markdown summary and only emits when `priority == "high"`.

---

## Codex (OpenAI)

Codex runs in a sandbox per task. Two options:

**Option A: tool wrapper.** Define `aime_outbox` as a tool in your task:

```typescript
{
  name: "aime_outbox",
  description: "Check if the AIME trading agent has any new events to report.",
  parameters: { type: "object", properties: {} }
}
```

When Codex calls it, run `aime outbox --json` and return the JSON. Codex
will weave it into its next message to the user.

**Option B: background poll.** Run a small wrapper script alongside
Codex that tails the outbox and uses Codex's `send_user_message` to
nudge the user when an event fires.

---

## Aider

Add a pre-prompt hook to `.aider.conf.yml`:

```yaml
auto-commits: false
hooks:
  pre-prompt: aime outbox --json --no-mark
```

Aider feeds the output into the context window before each prompt; if
the outbox has events, the model sees them and decides whether to
mention them. Use `--no-mark` so events stay visible until Aider
explicitly acks them.

---

## Cursor

Use Cursor's workspace agent settings to register a recurring check:

```jsonc
// .cursor/agents/aime.json
{
  "name": "aime-monitor",
  "interval": "5m",
  "command": "aime outbox --json",
  "behavior": "append-to-chat-if-output"
}
```

When the output is non-empty, Cursor appends it to the chat and the
model decides what to surface to you.

---

## Bare shell (cron + Telegram bot)

If you just want unsupervised forwarding (no AI host in the loop), use
the daemon's webhook:

```bash
# 1. Set up a Telegram bot, get BOT_TOKEN and CHAT_ID
# 2. Run a tiny transformer:
cat >/usr/local/bin/aime-to-tg <<'BASH'
#!/usr/bin/env bash
# Reads JSON from stdin, posts to Telegram. Use as AIME_WEBHOOK_URL target.
read -r payload
msg=$(echo "$payload" | jq -r '"[\(.priority|ascii_upcase)] \(.msg)"')
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" -d "text=${msg}"
BASH

# 3. Run as a tiny HTTP server with socat or http.server
# 4. export AIME_WEBHOOK_URL=http://localhost:9000
```

For events to also accumulate locally as a backup, also schedule:

```cron
*/5 * * * * aime outbox --json --no-mark > ~/aime-events.log 2>&1
```

---

## Custom host

The contract is two-way:

| Direction | Channel | Format |
|---|---|---|
| daemon → host | `~/.aime/outbox.jsonl` (poll) | JSONL, see OUTBOX_SCHEMA.md |
| daemon → host | `AIME_WEBHOOK_URL` POST | same JSON row per event |
| host → daemon | `127.0.0.1:7777` socket | line-delimited JSON RPC (`{"op": "ask", "content": "..."}`) |
| host → daemon | `~/.aime/inbox.jsonl` (fallback) | JSONL |

Both directions use the same on-disk schema. There's no AIME-specific
SDK — any language that can read JSONL and POST HTTP can integrate.

---

## Why polling instead of pub/sub?

Three reasons we picked file-based + webhook over a long-lived
connection:

1. **Host AI restarts don't lose events.** Each turn is fresh; the
   outbox persists.
2. **No daemon dependency for the host.** Host doesn't need a client
   library or a connection to keep alive.
3. **`aime outbox` is debuggable.** A user can run it from the shell
   to see exactly what their AI host sees.

If you want true push, set `AIME_WEBHOOK_URL` and run your own tiny
HTTP receiver — that gets you sub-second latency for high-priority
events.

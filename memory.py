"""
AIME starter-agent local memory (SPEC v2).

All persistent state lives in ~/.aime/ as append-only JSONL files.
Nothing here ever leaves the user's machine unless the agent
explicitly chooses to publish it (e.g. public reasoning to the
AIME backend).

Files
-----
~/.aime/
    tells.jsonl         user/main-agent hints fed to the trading agent
    lessons.jsonl       self-distilled lessons (from reflections)
    reflections.jsonl   per-market post-mortems
    decisions.jsonl     trade decisions (with public + private notes)
    outbox.jsonl        messages the agent wants the main agent to see
    personality.txt     editable personality / trading style preamble
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

HOME = Path(os.environ.get("AIME_HOME", str(Path.home() / ".aime")))
HOME.mkdir(parents=True, exist_ok=True)

TELLS = HOME / "tells.jsonl"
LESSONS = HOME / "lessons.jsonl"
REFLECTIONS = HOME / "reflections.jsonl"
DECISIONS = HOME / "decisions.jsonl"
OUTBOX = HOME / "outbox.jsonl"
INBOX = HOME / "inbox.jsonl"
STATUS = HOME / "status.json"
INBOX_CURSOR = HOME / ".inbox.cursor"
PERSONALITY = HOME / "personality.txt"
TRAILING = HOME / "trailing.json"   # per-market peak value/cost ratio (trailing stop)


DEFAULT_PERSONALITY = """\
You are a thoughtful prop trader on AIME, an AI-native prediction market.
You think in probabilities, size positions by conviction, and treat every
mistake as data. You are not a hype machine; you are not a doom-monger.
You take hints from your owner seriously but verify before you act, and
you say so when you disagree.
"""


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def _append(path: Path, obj: dict) -> dict:
    obj.setdefault("id", uuid.uuid4().hex[:12])
    obj.setdefault("ts", time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return obj


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _rewrite(path: Path, rows: Iterable[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# tells: user/main-agent hints
# ---------------------------------------------------------------------------

def add_tell(content: str, source: str = "main_agent",
             tags: list[str] | None = None) -> dict:
    return _append(TELLS, {
        "kind": "tell",
        "content": content,
        "source": source,
        "tags": tags or [],
    })


def recent_tells(hours: float = 48) -> list[dict]:
    cutoff = time.time() - hours * 3600
    return [t for t in _read_all(TELLS) if t.get("ts", 0) >= cutoff]


# ---------------------------------------------------------------------------
# lessons: distilled wisdom (used in trade prompts)
# ---------------------------------------------------------------------------

def add_lesson(text: str, tags: list[str] | None = None,
               based_on: list[str] | None = None) -> dict:
    return _append(LESSONS, {
        "kind": "lesson",
        "text": text,
        "tags": tags or [],
        "based_on": based_on or [],
    })


def all_lessons() -> list[dict]:
    return _read_all(LESSONS)


def recent_lessons(n: int = 10) -> list[dict]:
    rows = _read_all(LESSONS)
    return rows[-n:] if n else rows


def relevant_lessons(query: str, k: int = 5) -> list[dict]:
    """Naive lexical match: rank lessons by token overlap with query."""
    rows = _read_all(LESSONS)
    if not rows:
        return []
    q_tokens = {t.lower() for t in query.split() if len(t) > 2}
    if not q_tokens:
        return rows[-k:]
    scored = []
    for r in rows:
        text = (r.get("text") or "").lower()
        tokens = set(text.split())
        overlap = len(q_tokens & tokens)
        tag_hit = sum(1 for tag in (r.get("tags") or []) if tag.lower() in q_tokens)
        scored.append((overlap + 2 * tag_hit, r))
    scored.sort(key=lambda x: (x[0], x[1].get("ts", 0)), reverse=True)
    return [r for _, r in scored[:k]]


# ---------------------------------------------------------------------------
# reflections: per-market post-mortems (raw material for lessons)
# ---------------------------------------------------------------------------

def add_reflection(market_id: str, reasoning: str, outcome: str,
                   note: str = "", won: bool | None = None,
                   pnl: float | None = None) -> dict:
    return _append(REFLECTIONS, {
        "kind": "reflection",
        "market_id": market_id,
        "reasoning": reasoning,
        "outcome": outcome,
        "note": note,
        "won": won,
        "pnl": pnl,
    })


def recent_reflections(limit: int = 20) -> list[dict]:
    rows = _read_all(REFLECTIONS)
    return rows[-limit:] if limit else rows


def reflected_market_ids() -> set[str]:
    return {r.get("market_id") for r in _read_all(REFLECTIONS) if r.get("market_id")}


# ---------------------------------------------------------------------------
# decisions: trade log (public reasoning + private internal note)
# ---------------------------------------------------------------------------

def add_decision(market_id: str, market_title: str, position: str,
                 amount: float, reasoning: str,
                 internal_note: str = "",
                 confidence: float | None = None,
                 extra: dict | None = None) -> dict:
    row = {
        "kind": "decision",
        "market_id": market_id,
        "market_title": market_title,
        "position": position,
        "amount": amount,
        "reasoning": reasoning,         # public — uploaded to backend
        "internal_note": internal_note, # private — stays local
        "confidence": confidence,
    }
    if extra:
        row.update(extra)
    return _append(DECISIONS, row)


def recent_decisions(limit: int = 10) -> list[dict]:
    rows = _read_all(DECISIONS)
    return rows[-limit:] if limit else rows


def find_decision(market_id: str) -> dict | None:
    for row in reversed(_read_all(DECISIONS)):
        if row.get("market_id") == market_id:
            return row
    return None


# ---------------------------------------------------------------------------
# trailing-stop peaks: highest value/cost ratio seen per open market.
# Used by the dynamic (trailing) stop in manage_positions. Local-only kv.
# ---------------------------------------------------------------------------

def _load_trailing() -> dict:
    if not TRAILING.exists():
        return {}
    try:
        return json.loads(TRAILING.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def get_peak_ratio(market_id: str) -> float | None:
    v = _load_trailing().get(market_id)
    return float(v) if v is not None else None


def update_peak_ratio(market_id: str, ratio: float) -> float:
    """Record a new peak if higher; return the current peak."""
    data = _load_trailing()
    cur = float(data.get(market_id, 0) or 0)
    if ratio > cur:
        data[market_id] = ratio
        TRAILING.write_text(json.dumps(data), encoding="utf-8")
        return ratio
    return cur


def clear_peak_ratio(market_id: str) -> None:
    data = _load_trailing()
    if market_id in data:
        data.pop(market_id, None)
        TRAILING.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# outbox: pet → main agent messages
# ---------------------------------------------------------------------------

def post_to_outbox(msg: str, priority: str = "info",
                   msg_type: str = "note",
                   extra: dict | None = None) -> dict:
    row = {
        "kind": "outbox",
        "msg": msg,
        "priority": priority,   # "high" | "info" | "low"
        "msg_type": msg_type,
        "read": False,
    }
    if extra:
        row.update(extra)
    return _append(OUTBOX, row)


def read_outbox(unread_only: bool = True, mark_read: bool = True) -> list[dict]:
    rows = _read_all(OUTBOX)
    target = [r for r in rows if (not unread_only) or (not r.get("read"))]
    if mark_read and target:
        ids = {r["id"] for r in target}
        for r in rows:
            if r.get("id") in ids:
                r["read"] = True
        _rewrite(OUTBOX, rows)
    return target


def clear_outbox(message_ids: list[str] | None = None) -> int:
    rows = _read_all(OUTBOX)
    if message_ids is None:
        _rewrite(OUTBOX, [])
        return len(rows)
    drop = set(message_ids)
    kept = [r for r in rows if r.get("id") not in drop]
    _rewrite(OUTBOX, kept)
    return len(rows) - len(kept)


# ---------------------------------------------------------------------------
# inbox: main agent → trading agent (questions, instructions)
# ---------------------------------------------------------------------------

def push_inbox(content: str, kind: str = "ask",
               extra: dict | None = None) -> dict:
    """External callers (main AI, CLI) drop a message here."""
    row = {
        "kind": kind,           # "ask" | "instruct" | "info"
        "content": content,
    }
    if extra:
        row.update(extra)
    return _append(INBOX, row)


def drain_inbox() -> list[dict]:
    """Agent calls each cycle: return messages newer than the cursor."""
    rows = _read_all(INBOX)
    last_seen = 0.0
    if INBOX_CURSOR.exists():
        try:
            last_seen = float(INBOX_CURSOR.read_text().strip() or "0")
        except ValueError:
            last_seen = 0.0
    new = [r for r in rows if r.get("ts", 0) > last_seen]
    if new:
        INBOX_CURSOR.write_text(str(max(r["ts"] for r in new)))
    return new


# ---------------------------------------------------------------------------
# status: agent heartbeat (overwritten each cycle)
# ---------------------------------------------------------------------------

def write_status(state: dict) -> None:
    """Atomic overwrite of status.json so main AI can poll cheaply."""
    state["updated_at"] = time.time()
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(STATUS)


def read_status() -> dict:
    if not STATUS.exists():
        return {}
    try:
        return json.loads(STATUS.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# personality
# ---------------------------------------------------------------------------

def load_personality() -> str:
    if not PERSONALITY.exists():
        PERSONALITY.write_text(DEFAULT_PERSONALITY, encoding="utf-8")
    return PERSONALITY.read_text(encoding="utf-8").strip() or DEFAULT_PERSONALITY


def save_personality(text: str) -> None:
    PERSONALITY.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("AIME memory home:", HOME)
    print("personality:\n", load_personality())
    add_tell("smoke test tell", tags=["test"])
    add_lesson("low-volume short-resolution markets are noisy", tags=["risk"])
    add_decision("mkt_demo", "Demo market: will X happen?", "yes",
                 1.0, "test reasoning", internal_note="private",
                 confidence=0.6)
    add_reflection("mkt_demo", "test reasoning", "yes", note="resolved YES",
                   won=True, pnl=0.5)
    post_to_outbox("hello main agent, smoke test ok", priority="info",
                   msg_type="smoke")
    print("tells:", len(_read_all(TELLS)))
    print("lessons:", len(_read_all(LESSONS)))
    print("decisions:", len(_read_all(DECISIONS)))
    print("reflections:", len(_read_all(REFLECTIONS)))
    print("outbox (unread):", len(read_outbox(unread_only=True, mark_read=False)))

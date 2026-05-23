"""
owner_profile.py — the pet's mental model of its owner.

Three plain-text files in ~/.aime/, kept in sync with the aime-skill CLI
(`aime profile show`, `aime rule "..."`, `aime profile correct "..."`):

    about_owner.md   observed profile (interests, edges, style notes)
    beliefs.md       things the owner has said they think are true
    house_rules.md   explicit hard agreements ("don't trade sports")

Two halves to the contract:

1.  **Read side** — `load_context()` returns a compact prompt fragment we
    inject into trade decisions, ask/debate/brag prompts, and reflections.
    `enforce_rules()` checks a proposed trade against house rules and
    returns either {ok: True} or {ok: False, rule: "...", reason: "..."}.

2.  **Write side** — `observe_tell(...)` / `observe_settlement(...)` /
    `observe_reaction(...)` are hooks the daemon calls when something
    profile-worthy happens. They append candidate entries into the
    `pet:auto` blocks of the files, tagged `(observed)` so the user can
    spot them and push back via `aime profile correct`.

Files are markdown with `<!-- pet:auto:MARKER -->` / `<!-- /pet:auto:MARKER -->`
fences. The pet only writes between the fences; everything outside is the
user's territory.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import memory as mem


# ---------------------------------------------------------------------------
# seed content (mirrors aime-skill scripts/aime.py)
# ---------------------------------------------------------------------------

ABOUT_OWNER_SEED = """# About my owner

*The pet writes here as it learns about you. Edit freely; the pet won't
overwrite hand-written sections (anything outside `<!-- pet:auto -->` blocks).*

## Interests
<!-- pet:auto:interests -->
_(not yet observed)_
<!-- /pet:auto:interests -->

## Topics they don't care about
<!-- pet:auto:disinterests -->
_(not yet observed)_
<!-- /pet:auto:disinterests -->

## Areas where they seem to have an edge
<!-- pet:auto:edges -->
_(not yet observed)_
<!-- /pet:auto:edges -->

## Style notes
<!-- pet:auto:style -->
_(how they talk, what they react to, when they go quiet — pet fills this in)_
<!-- /pet:auto:style -->
"""

BELIEFS_SEED = """# What my owner believes

*Things the owner has said they think are true. Each line is a belief +
when/where the pet picked it up. Use `aime profile correct "..."` to push
back if the pet got it wrong.*

<!-- pet:auto:beliefs -->
_(no beliefs recorded yet)_
<!-- /pet:auto:beliefs -->
"""

HOUSE_RULES_SEED = """# House rules

*Explicit agreements between you and the pet. These take priority over
everything else — the pet must respect them or explain why it didn't.*

<!-- pet:auto:rules -->
_(no rules set yet — try `aime rule "don't trade sports"`)_
<!-- /pet:auto:rules -->
"""


# ---------------------------------------------------------------------------
# low-level block helpers
# ---------------------------------------------------------------------------

def _ensure(path: Path, seed: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed, encoding="utf-8")


def _read_block(path: Path, marker: str) -> list[str]:
    """Return non-placeholder lines from a pet:auto block."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    open_tag  = f"<!-- pet:auto:{marker} -->"
    close_tag = f"<!-- /pet:auto:{marker} -->"
    if open_tag not in text or close_tag not in text:
        return []
    body = text.split(open_tag, 1)[1].split(close_tag, 1)[0]
    return [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("_(")]


def _append_block(path: Path, seed: str, marker: str, line: str) -> bool:
    _ensure(path, seed)
    text = path.read_text(encoding="utf-8")
    open_tag  = f"<!-- pet:auto:{marker} -->"
    close_tag = f"<!-- /pet:auto:{marker} -->"
    if open_tag not in text or close_tag not in text:
        # corrupt or hand-edited away — append to end
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + line + "\n")
        return True
    head, rest = text.split(open_tag, 1)
    body, tail = rest.split(close_tag, 1)
    keep = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("_(")]
    if line.rstrip() in keep:
        return False   # dedup: skip if exact line already present
    keep.append(line.rstrip())
    new_body = "\n" + "\n".join(keep) + "\n"
    path.write_text(head + open_tag + new_body + close_tag + tail, encoding="utf-8")
    return True


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# READ side: prompt context + rule enforcement
# ---------------------------------------------------------------------------

def load_context(max_chars: int = 1200) -> str:
    """Return a compact prompt fragment summarising what we know about the owner.

    Intentionally short — this gets injected on every trade decision, so we
    don't want to balloon the system message. Truncates to ~max_chars.
    """
    _ensure(mem.ABOUT_OWNER, ABOUT_OWNER_SEED)
    _ensure(mem.BELIEFS,     BELIEFS_SEED)
    _ensure(mem.HOUSE_RULES, HOUSE_RULES_SEED)

    rules    = _read_block(mem.HOUSE_RULES, "rules")
    beliefs  = _read_block(mem.BELIEFS, "beliefs")
    interests = _read_block(mem.ABOUT_OWNER, "interests")
    disinterests = _read_block(mem.ABOUT_OWNER, "disinterests")
    edges = _read_block(mem.ABOUT_OWNER, "edges")
    style = _read_block(mem.ABOUT_OWNER, "style")

    parts = []
    if rules:
        parts.append("# House rules (priority — must respect)\n" + "\n".join(rules[-10:]))
    if beliefs:
        parts.append("# Owner beliefs (priors, not gospel)\n" + "\n".join(beliefs[-8:]))
    profile_bits = []
    if interests:    profile_bits.append("interests: "   + "; ".join(s.lstrip("- ").strip() for s in interests[-5:]))
    if disinterests: profile_bits.append("avoids: "      + "; ".join(s.lstrip("- ").strip() for s in disinterests[-5:]))
    if edges:        profile_bits.append("edges: "       + "; ".join(s.lstrip("- ").strip() for s in edges[-3:]))
    if style:        profile_bits.append("style: "       + "; ".join(s.lstrip("- ").strip() for s in style[-3:]))
    if profile_bits:
        parts.append("# Owner profile\n" + "\n".join(profile_bits))

    if not parts:
        return ""
    out = "\n\n".join(parts)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit("\n", 1)[0] + "\n…(profile truncated)"
    return out


# crude keyword check for a few common rule patterns. The LLM still gets the
# full rules list via load_context() and is encouraged to respect them; this
# function catches the obvious violations deterministically.
_RULE_CATEGORY_PATTERNS = {
    "sports":   re.compile(r"\b(don'?t|no|avoid|never).{0,15}(trade|touch|bet|do).{0,15}sport", re.I),
    "politics": re.compile(r"\b(don'?t|no|avoid|never).{0,15}(trade|touch|bet|do).{0,15}polit", re.I),
    "crypto":   re.compile(r"\b(don'?t|no|avoid|never).{0,15}(trade|touch|bet|do).{0,15}crypto", re.I),
}

_TITLE_CATEGORY_KEYWORDS = {
    "sports":   ["nfl", "nba", "mlb", "fifa", "world cup", "soccer", "football", "tennis", "olympic", "ufc", "boxing"],
    "politics": ["election", "trump", "biden", "congress", "senate", "president", "vote", "polit"],
    "crypto":   ["btc", "eth", "sol", "bitcoin", "ethereum", "crypto", "token", "coin"],
}


def _market_category(market: dict) -> Optional[str]:
    cat = (market.get("category") or "").lower()
    if cat:
        for c in _TITLE_CATEGORY_KEYWORDS:
            if c in cat:
                return c
    title = (market.get("title") or market.get("question") or "").lower()
    desc  = (market.get("description") or "").lower()
    blob = title + " " + desc
    for c, kws in _TITLE_CATEGORY_KEYWORDS.items():
        if any(kw in blob for kw in kws):
            return c
    return None


def enforce_rules(market: dict, amount_usd: float | None = None) -> dict:
    """Pre-check a proposed trade against house rules.

    Returns:
      {"ok": True}                                — nothing in the rules blocks it
      {"ok": False, "rule": "...", "reason": "..."}  — rule violated, daemon should skip
                                                       (or override with an explicit note)
    """
    rules = _read_block(mem.HOUSE_RULES, "rules")
    if not rules:
        return {"ok": True}

    cat = _market_category(market)

    for raw in rules:
        rule = raw.lstrip("- ").strip()
        # strip leading [YYYY-MM-DD] timestamp
        rule_clean = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", rule)

        # category bans
        for c, pat in _RULE_CATEGORY_PATTERNS.items():
            if pat.search(rule_clean) and cat == c:
                return {
                    "ok": False,
                    "rule": rule_clean,
                    "reason": f"market is in category '{c}' which is on the no-trade list",
                }

        # size caps: "ask me before any trade over $20" / "max 5 per trade"
        m = re.search(r"(?:over|above|>|max|limit\s*to)\s*\$?\s*(\d+(?:\.\d+)?)", rule_clean, re.I)
        if m and amount_usd is not None:
            cap = float(m.group(1))
            # "ask before X" → treat as soft cap; let LLM decide
            soft = bool(re.search(r"\b(ask|check|confirm|tell|notify)\b", rule_clean, re.I))
            if amount_usd > cap and not soft:
                return {
                    "ok": False,
                    "rule": rule_clean,
                    "reason": f"trade size ${amount_usd:.2f} exceeds rule cap ${cap:.2f}",
                }

    return {"ok": True}


# ---------------------------------------------------------------------------
# WRITE side: passive learning hooks
# ---------------------------------------------------------------------------

# These get called from agent.py / agent_brain.py at obvious moments.
# We deliberately keep heuristics SIMPLE — the goal isn't to be clever, it's
# to leave a paper trail the user can correct.

_BELIEF_TRIGGERS = re.compile(
    r"\b(i (?:think|believe|reckon|expect)|owner thinks|owner believes|imo|in my view|"
    r"my take|hot take|tldr|gonna|will probably|going to)\b",
    re.I,
)

_RULE_TRIGGERS = re.compile(
    r"\b(don'?t (?:ever )?|never |from now on|always |stop |only |no more |max(?:imum)? )",
    re.I,
)


def observe_tell(content: str, source: str = "", tags: Optional[list[str]] = None) -> dict:
    """Called when a new tell arrives (from `aime tell ...`).

    Looks for belief-shaped or rule-shaped statements and appends them as
    (observed) candidates. The user can correct or remove them later.
    Returns {"belief_added": bool, "rule_added": bool, "skipped": bool}.
    """
    if not content or len(content) > 500:
        return {"belief_added": False, "rule_added": False, "skipped": True}

    tags = tags or []
    if "noise" in tags:
        return {"belief_added": False, "rule_added": False, "skipped": True}

    txt = content.strip()
    out = {"belief_added": False, "rule_added": False, "skipped": False}

    # rule-shaped wins over belief-shaped (more actionable)
    if _RULE_TRIGGERS.search(txt):
        # don't mistake "I think the dollar will fall" for a rule
        if not _BELIEF_TRIGGERS.search(txt):
            src = f" — via {source}" if source else ""
            line = f"- [{_today()}] (observed) {txt}{src}"
            if _append_block(mem.HOUSE_RULES, HOUSE_RULES_SEED, "rules", line):
                out["rule_added"] = True
                return out

    if _BELIEF_TRIGGERS.search(txt):
        src = f" ({source})" if source else ""
        tagstr = " " + " ".join(f"#{t}" for t in tags if t and t != "noise") if tags else ""
        line = f"- [{_today()}]{src} (observed) {txt}{tagstr}"
        if _append_block(mem.BELIEFS, BELIEFS_SEED, "beliefs", line):
            out["belief_added"] = True

    return out


def observe_settlement(market: dict, won: bool, pnl: float,
                       triggering_tell: Optional[str] = None,
                       triggering_source: Optional[str] = None) -> dict:
    """Called from the reflection loop after a market settles.

    If a tell from the owner triggered this trade and it WON, that source
    earns an 'edge' marker. If it LOST, we don't blame the owner — silence.
    """
    if not won or not triggering_tell or pnl <= 0:
        return {"edge_added": False}

    src = triggering_source or "owner"
    cat = _market_category(market) or "general"
    line = f"- [{_today()}] {src}-sourced tells have paid off in {cat} (+${pnl:.2f} on \"{(market.get('title') or '')[:60]}\")"
    added = _append_block(mem.ABOUT_OWNER, ABOUT_OWNER_SEED, "edges", line)
    return {"edge_added": added}


def observe_reaction(kind: str, detail: str = "") -> dict:
    """Called when the owner reacts to something the pet did.

    kind: one of {"shrug", "complain", "praise", "stop"}.
    Writes a short style-note candidate. Very low-frequency by design — only
    fires on strong signals from the host-AI side (handled separately).
    """
    if kind not in {"shrug", "complain", "praise", "stop"}:
        return {"style_added": False}
    detail = (detail or "").strip()
    line = f"- [{_today()}] owner reaction: {kind}" + (f" — {detail}" if detail else "")
    added = _append_block(mem.ABOUT_OWNER, ABOUT_OWNER_SEED, "style", line)
    return {"style_added": added}


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    print("AIME owner_profile smoke test")
    print("home:", mem.HOME)

    print("\n-- observe_tell (belief-shaped) --")
    print(_json.dumps(observe_tell("I think BTC tops out near $120k this cycle", source="main_chat", tags=["btc", "macro"])))

    print("\n-- observe_tell (rule-shaped) --")
    print(_json.dumps(observe_tell("Never trade sports markets", source="main_chat")))

    print("\n-- enforce_rules vs sports market --")
    print(_json.dumps(enforce_rules({"title": "Will the Lakers win tonight?", "category": "sports"})))

    print("\n-- enforce_rules vs crypto market --")
    print(_json.dumps(enforce_rules({"title": "BTC > $100k by end of month"})))

    print("\n-- load_context --")
    print(load_context()[:500])

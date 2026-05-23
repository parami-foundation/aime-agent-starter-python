"""Tests for owner_profile.py — the pet's mental model of the owner.

Covers:
  - observe_tell auto-categorises belief- vs rule-shaped statements
  - enforce_rules blocks category-banned markets, allows others
  - enforce_rules clamps size caps
  - load_context renders a compact, prompt-safe summary
  - hand-written content outside pet:auto blocks is never touched
  - dedup: the same line isn't appended twice
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "aime"
    home.mkdir()
    monkeypatch.setenv("AIME_HOME", str(home))
    monkeypatch.setenv("AIME_LLM_PROVIDER", "stub")
    # Reload memory + owner_profile so they bind to the new HOME
    for mod in ("memory", "owner_profile"):
        sys.modules.pop(mod, None)
    return home


# ---------- observe_tell ----------

def test_observe_tell_belief(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    out = op.observe_tell("I think BTC tops out near $120k this cycle", source="chat", tags=["btc"])
    assert out["belief_added"] is True
    assert out["rule_added"] is False
    text = (home / "beliefs.md").read_text()
    assert "BTC tops out" in text
    assert "(observed)" in text


def test_observe_tell_rule(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    out = op.observe_tell("Never trade sports markets", source="chat")
    assert out["rule_added"] is True
    assert out["belief_added"] is False
    text = (home / "house_rules.md").read_text()
    assert "sports" in text.lower()


def test_observe_tell_neither(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    # casual banter, no belief or rule marker → both False
    out = op.observe_tell("the weather is nice today")
    assert out["belief_added"] is False
    assert out["rule_added"] is False


def test_observe_tell_dedup(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    op.observe_tell("Never trade politics markets", source="chat")
    op.observe_tell("Never trade politics markets", source="chat")
    text = (home / "house_rules.md").read_text()
    # exact same line shouldn't appear twice
    assert text.count("Never trade politics markets") == 1


def test_observe_tell_noise_tag_skipped(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    out = op.observe_tell("I think this is great", tags=["noise"])
    assert out["skipped"] is True


# ---------- enforce_rules ----------

def test_enforce_blocks_sports_when_banned(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    op.observe_tell("Never trade sports markets")
    check = op.enforce_rules({"title": "Lakers vs Celtics tonight", "category": "sports"})
    assert check["ok"] is False
    assert "sports" in check["reason"]


def test_enforce_allows_unrelated_when_sports_banned(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    op.observe_tell("Never trade sports markets")
    check = op.enforce_rules({"title": "BTC > 100k by EOY"})
    assert check["ok"] is True


def test_enforce_size_cap_blocks_oversize(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    # hard cap (no "ask before" softener)
    op.observe_tell("max 5 USD per trade")
    check = op.enforce_rules({"title": "Some market"}, amount_usd=10.0)
    assert check["ok"] is False
    assert "5" in check["reason"]


def test_enforce_size_soft_cap_passes(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    # "ask before" → soft, doesn't block; pet should self-clamp + record
    op.observe_tell("ask me before any trade over 20 USD")
    check = op.enforce_rules({"title": "Some market"}, amount_usd=25.0)
    assert check["ok"] is True


def test_enforce_no_rules_ok(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    check = op.enforce_rules({"title": "Anything goes"})
    assert check["ok"] is True


# ---------- load_context ----------

def test_load_context_empty_when_fresh(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    # files don't exist yet → context is empty string
    assert op.load_context() == ""


def test_load_context_includes_rules_and_beliefs(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    op.observe_tell("Never trade sports")
    op.observe_tell("I think AI stocks are undervalued", tags=["ai"])
    ctx = op.load_context()
    assert "House rules" in ctx
    assert "sports" in ctx
    assert "Owner beliefs" in ctx
    assert "AI stocks" in ctx


def test_load_context_truncates(tmp_path, monkeypatch):
    _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    for i in range(50):
        op.observe_tell(f"I think proposition number {i} is true")
    ctx = op.load_context(max_chars=400)
    assert len(ctx) <= 500   # 400 + truncation marker padding


# ---------- handwritten preservation ----------

def test_handwritten_outside_block_preserved(tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    import owner_profile as op
    # seed the file via observe_tell so seed text is in place
    op.observe_tell("Never trade sports")
    # user manually appends a hand-written paragraph OUTSIDE the auto block
    p = home / "house_rules.md"
    p.write_text(p.read_text() + "\n\n## My own notes\nI wrote this myself.\n")
    # pet adds another rule
    op.observe_tell("Never trade politics")
    final = p.read_text()
    assert "I wrote this myself." in final  # hand-written intact
    assert "politics" in final.lower()       # new rule still added

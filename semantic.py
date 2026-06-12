"""Semantic recall — vecgrep-backed retrieval for the per-turn prompt hook.

Turns memory recall from "the agent notices an index line and decides to
fetch it" into "the relevant memory is already in front of it." Embeds the
incoming prompt, queries the local vecgrep memories corpus via
``vecgrep_client``, dedupes to the top-N distinct memories above a score
floor, and formats a compact block printed above the names+tags index.

Design constraints:
  - Reuses ``vecgrep_client`` (the same search layer the web UI uses) rather
    than duplicating an HTTP client — one place tunes corpus/ranking.
  - Fail silent: vecgrep down / slow / malformed -> return "". NEVER block a
    turn or raise into the hook. The names+tags index is the fallback.
  - Cheap: one search call, capped output.

Toggle: env ``CCDK_SEMANTIC_RECALL`` in {0,off,false,no} disables it.
Tunables: ``CCDK_SEMANTIC_K`` (distinct memories, default 3),
``CCDK_SEMANTIC_FLOOR`` (min similarity_pct, default 50).
"""

from __future__ import annotations

import os
import re

import store
import vecgrep_client as vg

TOP_N = int(os.environ.get("CCDK_SEMANTIC_K", "3"))
FLOOR = float(os.environ.get("CCDK_SEMANTIC_FLOOR", "50"))
MIN_PROMPT_CHARS = 12          # skip "ok" / "go" / "thanks"
FETCH_K = max(12, TOP_N * 4)   # over-fetch so dedupe still yields TOP_N distinct
SNIPPET_CHARS = 150


def _enabled() -> bool:
    return os.environ.get("CCDK_SEMANTIC_RECALL", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _clean(text: str) -> str:
    """Collapse a memory body to a single trimmed line for the snippet."""
    text = re.sub(r"\s+", " ", text or "").strip(" -•\t\n")
    return text[:SNIPPET_CHARS].strip()


def format_recall(prompt: str) -> str:
    """Return a compact recall block for `prompt`, or "" if nothing/unavailable."""
    if not _enabled():
        return ""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return ""

    try:
        # Deduped + hybrid-ranked (memory,pct,matched_by); want_kind keeps
        # journal chunks out of the memory recall block.
        ranked = vg.search_corpus_to_ids_with_match(
            prompt, vg.VECGREP_CORPUS_MEMORIES, top_k=FETCH_K, want_kind="memory",
        )
    except vg.VecgrepUnavailable:
        return ""          # fail silent — index fallback covers us
    except Exception:
        return ""

    hits = [(eid, pct) for eid, pct, _by in ranked if pct >= FLOOR][:TOP_N]
    if not hits:
        return ""

    by_id = {m.get("id"): m for m in store.load_memories()}
    lines = ["RELEVANT TO YOUR MESSAGE (semantic recall — full text via `ccdk memory show <id>`):"]
    for eid, pct in hits:
        mem = by_id.get(eid)
        if not mem:
            continue
        name = (mem.get("name") or "").strip()
        snippet = _clean(mem.get("text", ""))
        body = f"{name} — {snippet}" if name else snippet
        if body:
            lines.append(f"  • #{eid} [{pct:.0f}%] {body}")
    return "\n".join(lines) if len(lines) > 1 else ""

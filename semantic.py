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
import time

import store
import vecgrep_client as vg

TOP_N = int(os.environ.get("CCDK_SEMANTIC_K", "3"))       # max authoritative HITS
LEAD_N = int(os.environ.get("CCDK_SEMANTIC_LEAD_N", "2"))  # max lower-conf LEADS
# FLOOR retained for files-recall (a plain score gate). Memory recall now tiers
# by HOW a hit matched (see _tier) instead of a hard score floor — a hard floor
# silently drops correct-but-conversational vector matches (person lookups land
# at 15-30%). LEAD_FLOOR gates vector-only leads (junk vector-only peaks ~11%,
# correct leads bottom ~14%, so 13 splits them).
FLOOR = float(os.environ.get("CCDK_SEMANTIC_FLOOR", "50"))
LEAD_FLOOR = float(os.environ.get("CCDK_SEMANTIC_LEAD_FLOOR", "13"))
MIN_PROMPT_CHARS = 12          # skip "ok" / "go" / "thanks"
FETCH_K = max(12, TOP_N * 4)   # over-fetch so dedupe still yields TOP_N distinct
SNIPPET_CHARS = 150
FILES_CORPUS = os.environ.get("CCDK_SEMANTIC_FILES_CORPUS", "files")

# Observability: one line per recall outcome (down|empty|hit) so unreliable
# retrieval becomes measurable. Auto-pruned to stay bounded.
RECALL_LOG = os.path.join(store.DATA_DIR, "recall.log")
RECALL_LOG_MAX_LINES = int(os.environ.get("CCDK_RECALL_LOG_MAX", "2000"))


def _enabled() -> bool:
    return os.environ.get("CCDK_SEMANTIC_RECALL", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _log_recall(outcome: str, n_raw: int, *, n_hits: int = 0, n_leads: int = 0) -> None:
    """Append one outcome line: `<unix_ts> <outcome> raw hits leads`. Auto-pruned
    to the last half when it exceeds RECALL_LOG_MAX_LINES. Best-effort, never raises."""
    try:
        line = f"{int(time.time())} {outcome} raw={n_raw} hits={n_hits} leads={n_leads}\n"
        os.makedirs(os.path.dirname(RECALL_LOG), exist_ok=True)
        with open(RECALL_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        if os.path.getsize(RECALL_LOG) > RECALL_LOG_MAX_LINES * 64:
            _prune_recall_log()
    except OSError:
        pass


def _prune_recall_log() -> None:
    try:
        with open(RECALL_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= RECALL_LOG_MAX_LINES:
            return
        keep = lines[-(RECALL_LOG_MAX_LINES // 2):]
        tmp = RECALL_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, RECALL_LOG)
    except OSError:
        pass


def _tier(pct: float, matched_by) -> str:
    """Classify by HOW a hit matched, not just score (score alone is an unreliable
    signal/noise separator: off-topic queries make bm25-only matches at 80-90%,
    correct conversational queries make vector-only matches at 15-30%).
      vector+bm25 → HIT (authoritative); vector-only ≥LEAD_FLOOR → LEAD (semantic
      match keywords missed — the person-lookup saver); bm25-only → DROP (noise)."""
    by = set(matched_by or [])
    has_vec, has_bm = "vector" in by, "bm25" in by
    if has_vec and has_bm:
        return "hit"
    if has_vec and pct >= LEAD_FLOOR:
        return "lead"
    return "drop"


def _clean(text: str) -> str:
    """Collapse a memory body to a single trimmed line for the snippet."""
    text = re.sub(r"\s+", " ", text or "").strip(" -•\t\n")
    return text[:SNIPPET_CHARS].strip()


def format_recall(prompt: str) -> str:
    """Return a tiered recall block for `prompt`, or "" if nothing/unavailable.

    Two tiers (see _tier): authoritative HITS (treat as known) and lower-
    confidence LEADS (verify before relying). The header is a directive so the
    bot acts on the memory instead of skimming past it."""
    if not _enabled():
        return ""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return ""

    try:
        # Deduped + hybrid-ranked (memory, pct, matched_by); want_kind keeps
        # journal chunks out of the memory recall block.
        ranked = vg.search_corpus_to_ids_with_match(
            prompt, vg.VECGREP_CORPUS_MEMORIES, top_k=FETCH_K, want_kind="memory",
        )
    except vg.VecgrepUnavailable:
        _log_recall("down", 0)   # observability: distinguish down from empty
        return ""          # fail silent — index fallback covers us
    except Exception:
        _log_recall("down", 0)
        return ""

    tiers = [(eid, pct, _tier(pct, by)) for eid, pct, by in ranked]
    hits = [(eid, pct) for eid, pct, t in tiers if t == "hit"][:TOP_N]
    leads = [(eid, pct) for eid, pct, t in tiers if t == "lead"][:LEAD_N]
    if not hits and not leads:
        _log_recall("empty", len(ranked))
        return ""

    by_id = {m.get("id"): m for m in store.load_memories()}

    def _row(eid, pct):
        mem = by_id.get(eid)
        if not mem:
            return None
        name = (mem.get("name") or "").strip()
        snippet = _clean(mem.get("text", ""))
        body = f"{name} — {snippet}" if name else snippet
        return f"  • #{eid} [{pct:.0f}%] {body}" if body else None

    out: list[str] = []
    if hits:
        out.append(
            "★ MEMORY HITS for this message — treat as authoritative, prefer over "
            "guessing. Full body: `ccdk memory show <id>`.")
        out += [r for r in (_row(e, p) for e, p in hits) if r]
    if leads:
        out.append(
            "  possible leads (lower confidence — `ccdk memory show <id>` to "
            "confirm before relying):")
        out += [r for r in (_row(e, p) for e, p in leads) if r]

    _log_recall("hit", len(ranked), n_hits=len(hits), n_leads=len(leads))
    return "\n".join(out) if len(out) > 1 else ""


def _file_label(source_id: str) -> str:
    """Map a files-corpus source to a citation: file-7.md -> 'file #7'."""
    base = (source_id or "").rsplit("/", 1)[-1]
    m = re.match(r"file-(\d+)", base)
    return ("file #" + m.group(1)) if m else base.replace(".md", "")


def format_files_recall(prompt: str) -> str:
    """RAG over the shared FILES corpus — project-knowledge-style retrieval.

    Same mechanism as format_recall, pointed at the documents store: embed the
    prompt, pull the top relevant file chunks, inject them so file content is
    in front of the agent automatically (push, not pull). "" if nothing matches
    or vecgrep is down. No-op until the files store has content.
    """
    if not _enabled():
        return ""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return ""
    try:
        hits = vg._post_search(prompt, FILES_CORPUS, top_k=FETCH_K)
    except vg.VecgrepUnavailable:
        return ""
    except Exception:
        return ""

    best: dict[str, dict] = {}
    for h in hits:
        pct = h.get("similarity_pct", 0)
        if pct < FLOOR:
            continue
        label = _file_label(h.get("source_id", ""))
        if label not in best or pct > best[label].get("similarity_pct", 0):
            best[label] = h

    ranked = sorted(best.items(), key=lambda kv: kv[1].get("similarity_pct", 0), reverse=True)[:TOP_N]
    if not ranked:
        return ""

    lines = ["RELEVANT FILES (shared docs — full text via `ccdk files show <id>`):"]
    for label, h in ranked:
        snippet = _clean(h.get("chunk", ""))
        if snippet:
            lines.append(f"  • {label} [{h['similarity_pct']:.0f}%] {snippet}")
    return "\n".join(lines) if len(lines) > 1 else ""

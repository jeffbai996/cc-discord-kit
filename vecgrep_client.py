"""Thin HTTP client for vecgrep's /api/search endpoint.

Used by server.py to render semantic-search results inline in the memories
and journal index pages. Vecgrep returns chunk-level hits keyed by source
file path; we map that back to memory/journal entry IDs by parsing the
filename (the converter writes files as `<id4>-<slug>.md`).

Vecgrep is expected to be running locally (or reachable over tailnet) at
the URL configured via VECGREP_URL env var (default http://127.0.0.1:8765).
If the endpoint is unreachable, callers fall back to literal substring
search via store.search_memories / store.search_journal — this module
raises VecgrepUnavailable so the caller can do that fallback cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

VECGREP_URL = os.environ.get("VECGREP_URL", "http://127.0.0.1:8765").rstrip("/")
# Single corpus that holds both memories and journal entries
# (filename-prefixed: memory-N.md / journal-N.md). Override via env if you
# want to point at separate squad-memories / squad-journal corpora instead.
VECGREP_CORPUS_MEMORIES = os.environ.get("VECGREP_CORPUS_MEMORIES", "cc-discord-kit")
VECGREP_CORPUS_JOURNAL = os.environ.get("VECGREP_CORPUS_JOURNAL", "cc-discord-kit")
SEARCH_TIMEOUT_SEC = 5

# How many top hits to surface to the UI. We deliberately don't apply a
# similarity threshold here. vecgrep's sigmoid-calibrated cosine mapping
# (vecgrep/backend/service.py: cos 0.66 → 50%, cos 0.75 → 75%, cos 0.85
# → ~91%) already pushes unrelated content well below the user-visible
# band, so a hard threshold would only reject legitimate-but-soft matches
# (single names, short queries). Strategy instead: show the percentage in
# the UI per row, and trust the user to judge the top-N hits as a ranked
# list. Garbage queries return low % across the board; real queries have
# a top hit visibly above the rest.
TOP_K_DEFAULT = int(os.environ.get("VECGREP_TOP_K", "10"))

# Filename patterns we know about:
#   memory-N.md / journal-N.md  — emitted by the corpus dump script
#                                 (the auto-maintained corpus)
#   NNNN-slug.md                 — emitted by squad_to_markdown.py (rsync flow)
_FILENAME_RE = re.compile(
    r"^(?:(?P<kind>memory|journal)-(?P<eid>\d+)\.md|"
    r"(?P<eid2>\d{2,6})-[^.]*\.md)$"
)


class VecgrepUnavailable(Exception):
    """Raised when /api/search is unreachable or returns a non-2xx."""


def _post_search(query: str, corpus: str, top_k: int = 25) -> list[dict]:
    body = json.dumps({
        "query": query,
        "corpus": corpus,
        "top_k": top_k,
        "mode": "hybrid",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{VECGREP_URL}/api/search",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read())
            return data.get("hits", [])
    except urllib.error.URLError as e:
        raise VecgrepUnavailable(f"vecgrep at {VECGREP_URL} unreachable: {e}") from e
    except (json.JSONDecodeError, OSError, TimeoutError) as e:
        raise VecgrepUnavailable(f"vecgrep search failed: {e}") from e


def _hit_to_entry_id(hit: dict, want_kind: str | None = None) -> int | None:
    """Pull the memory/journal entry ID out of a vecgrep hit's source path.

    `want_kind` (optional, "memory" or "journal") filters out hits from the
    other kind when scanning the unified corpus, where the
    filename prefix encodes the kind. Filenames matching the legacy
    NNNN-slug pattern have no kind in the name and are returned regardless.
    """
    src = hit.get("source_id", "")
    if not src:
        return None
    base = os.path.basename(src)
    m = _FILENAME_RE.match(base)
    if not m:
        return None
    kind = m.group("kind")
    if want_kind and kind and kind != want_kind:
        return None
    raw_id = m.group("eid") or m.group("eid2")
    try:
        return int(raw_id)
    except (ValueError, TypeError):
        return None


def search_corpus_to_ids(
    query: str,
    corpus: str,
    top_k: int | None = None,
    want_kind: str | None = None,
) -> list[tuple[int, float]]:
    """Run a vecgrep search and return (entry_id, similarity_pct) pairs.

    Dedupes if the same entry has multiple chunks hit — keeps the highest pct.
    Order preserves vecgrep's hybrid-fusion ranking. `want_kind` filters
    out hits whose filename declares the wrong kind ("memory" vs "journal")
    — needed when searching the unified corpus that mixes both.

    No similarity threshold is applied: see the module-level comment for
    why thresholds don't work cleanly with nomic-embed-text. Callers (the
    template) display the score next to each entry so users can judge which
    hits are real and which are noise.

    May raise VecgrepUnavailable.
    """
    return [
        (eid, pct) for eid, pct, _by in _search_corpus_with_match(
            query, corpus, top_k=top_k, want_kind=want_kind,
        )
    ]


def search_corpus_to_ids_with_match(
    query: str,
    corpus: str,
    top_k: int | None = None,
    want_kind: str | None = None,
) -> list[tuple[int, float, frozenset[str]]]:
    """Like search_corpus_to_ids, but also returns vecgrep's `matched_by`
    set (subset of {"vector","bm25"}) per entry.

    A BM25 hit on a short query is the strongest "exact-keyword" signal
    even when its pct is tiny — RRF compresses BM25-only scores into the
    same noise band as the vector floor. UI callers use the match-set to
    promote color tier (a 1.6% BM25 hit is a real keyword match, not noise).
    """
    return _search_corpus_with_match(query, corpus, top_k=top_k, want_kind=want_kind)


def _search_corpus_with_match(
    query: str,
    corpus: str,
    top_k: int | None,
    want_kind: str | None,
) -> list[tuple[int, float, frozenset[str]]]:
    hits = _post_search(query, corpus, top_k=top_k or TOP_K_DEFAULT)
    if not hits:
        return []
    seen_pct: dict[int, float] = {}
    seen_by: dict[int, set[str]] = {}
    order: list[int] = []
    for h in hits:
        eid = _hit_to_entry_id(h, want_kind=want_kind)
        if eid is None:
            continue
        pct = float(h.get("similarity_pct", 0.0))
        by = set(h.get("matched_by") or [])
        if eid not in seen_pct:
            seen_pct[eid] = pct
            seen_by[eid] = by
            order.append(eid)
        else:
            seen_pct[eid] = max(seen_pct[eid], pct)
            seen_by[eid] |= by
    return [(eid, seen_pct[eid], frozenset(seen_by[eid])) for eid in order]


_AVAILABILITY_CACHE: dict[str, float | bool] = {"ts": 0.0, "ok": False}
# Asymmetric TTL by design:
#   ok=True   — cache for a short window. If vecgrep dies, we want to notice
#               quickly so the UI hides the semantic toggle before a user
#               clicks it and eats the search timeout.
#   ok=False  — cache for a long window. The probe itself costs time when
#               vecgrep is down; once we know it's down, no point re-probing
#               every few seconds. The watchdog will bounce vecgrep within
#               ~60s if it's stuck, so a 2-min negative cache is fine.
_AVAILABILITY_TTL_OK_SEC = 10.0
_AVAILABILITY_TTL_BAD_SEC = 120.0
_AVAILABILITY_TIMEOUT_SEC = 0.5


def is_available() -> bool:
    """Cheap probe — call before showing a 'semantic' toggle in the UI.

    Cached so a hung vecgrep doesn't block every page render with the probe
    timeout. Positive and negative results have different TTLs (see the
    constants above) because their failure modes are asymmetric.
    """
    import time
    now = time.monotonic()
    age = now - _AVAILABILITY_CACHE["ts"]
    cached_ok = bool(_AVAILABILITY_CACHE["ok"])
    ttl = _AVAILABILITY_TTL_OK_SEC if cached_ok else _AVAILABILITY_TTL_BAD_SEC
    if age < ttl:
        return cached_ok
    try:
        # /api/health is the right endpoint — /api/config is auth-gated now
        # and would always return 401 (wrong false-positive direction).
        req = urllib.request.Request(f"{VECGREP_URL}/api/health")
        with urllib.request.urlopen(req, timeout=_AVAILABILITY_TIMEOUT_SEC) as resp:
            ok = 200 <= resp.status < 300
    except Exception:
        ok = False
    _AVAILABILITY_CACHE["ts"] = now
    _AVAILABILITY_CACHE["ok"] = ok
    return ok

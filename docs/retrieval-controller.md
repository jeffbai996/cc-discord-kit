# Retrieval controller — the recall loop

A bot's session loads most of the memory store directly at startup (full memory
bodies + recent journal, up to `CCDK_PRELOAD_BUDGET` tokens — default unbounded).
So the right reflex is: **check what you already have before reaching for a
tool.**

The loop a bot follows when it needs a fact:

1. **ANSWER** — Is the answer already in your loaded context? At small store
   sizes it usually is, because the session preloaded the full bodies. If so,
   answer — don't reflexively search.
2. **RETRIEVE** — If it's genuinely not loaded, run
   `cc-discord-kit recall "<a targeted query>"` (extract what you're missing;
   don't echo the raw user prompt). It returns full bodies of the
   best-matching memories.
3. **REFLECT** — Did the hits answer it? If not, re-query *differently* once.
   If a second recall still misses, say plainly you don't have it — don't loop.
   (An unbounded search loop wastes turns and invents answers.)

`recall` reports its source:

- `via vecgrep` — the semantic engine answered (vector + keyword hybrid).
- `via substring` — vecgrep was unavailable, so it fell back to a plain
  substring match. A substring result is weaker — treat it as a lead, not a
  confident hit.

## Why preload-then-recall, instead of always searching

At small store sizes (a few hundred entries) a model reading the full bodies
in context outperforms a retriever fishing for the right few — full context
beats sophisticated RAG until the store grows large enough that loading it all
dilutes attention or busts the budget. So the cheapest correct strategy while
small is: **load more directly, and only reach for `recall` on a miss.**

Tune `CCDK_PRELOAD_BUDGET` (the token budget for the full-body preload) as your
store grows:

- `0` (default) — load every memory body. Good while the store is small.
- a positive integer — cap the preload at that many tokens (newest-first);
  entries that don't fit stay index-only and are reached via `recall`. Dropped
  entries are logged to `boot_hook.log` so a truncated memory is never mistaken
  for one that doesn't exist.

The crossover point — where a fuller context stops helping — depends on your
data and model. Raise the budget until added context stops improving answers,
then set it just below that.

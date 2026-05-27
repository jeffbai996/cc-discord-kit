"""Store module tests — memory/journal CRUD, soft-delete, caps, filtering.

Uses the `fresh_store` fixture (conftest.py) which reloads store pointed at a
temp DATA_DIR, so the user's real data files are never touched. Generic
fixtures only (AAPL/MSFT/GOOGL, fake bot/actor names).
"""

from __future__ import annotations

import os


# ─────────────────────────── memory CRUD ───────────────────────────


def test_save_memory_assigns_monotonic_ids(fresh_store):
    store, _ = fresh_store
    a = store.save_memory("first", name="a")
    b = store.save_memory("second", name="b")
    assert a["id"] == 1
    assert b["id"] == 2
    assert a["type"] == "feedback"  # default
    assert a["about"] == []


def test_save_memory_invalid_type_falls_back_to_feedback(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("x", type="not-a-real-type")
    assert mem["type"] == "feedback"


def test_save_memory_valid_types_preserved(fresh_store):
    store, _ = fresh_store
    for t in ("user", "feedback", "project", "reference"):
        mem = store.save_memory("body", type=t)
        assert mem["type"] == t


def test_save_memory_strips_whitespace_and_rendered_header(fresh_store):
    store, _ = fresh_store
    # The rendered-header regex strips leading "About: ...\nSaved: ..." blocks
    # that agents paste back when round-tripping `memory show` output.
    body = "About: AAPL\n\nSaved: 2026-01-01\n\nActual content here"
    mem = store.save_memory("   " + body + "   ")
    assert mem["text"] == "Actual content here"


def test_save_memory_bot_field_only_when_provided(fresh_store):
    store, _ = fresh_store
    no_bot = store.save_memory("shared")
    with_bot = store.save_memory("scoped", bot=["agent-1"])
    assert "bot" not in no_bot
    assert with_bot["bot"] == ["agent-1"]


def test_edit_memory_updates_fields(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("v1", name="orig", tags=["t1"])
    assert store.edit_memory(mem["id"], text="v2", name="new", tags=["t2"]) is True
    cur = [m for m in store.load_memories() if m["id"] == mem["id"]][0]
    assert cur["text"] == "v2"
    assert cur["name"] == "new"
    assert cur["tags"] == ["t2"]


def test_edit_memory_no_fields_returns_false(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body")
    assert store.edit_memory(mem["id"]) is False


def test_edit_memory_invalid_type_ignored(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body", type="user")
    # Invalid type is silently dropped from the fields dict; nothing else to
    # edit means no update happened.
    assert store.edit_memory(mem["id"], type="garbage") is False
    cur = [m for m in store.load_memories() if m["id"] == mem["id"]][0]
    assert cur["type"] == "user"


def test_edit_memory_missing_id_returns_false(fresh_store):
    store, _ = fresh_store
    assert store.edit_memory(99999, text="ghost") is False


def test_edit_memory_pinned_flag(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body")
    assert store.edit_memory(mem["id"], pinned=True) is True
    cur = [m for m in store.load_memories() if m["id"] == mem["id"]][0]
    assert cur["pinned"] is True


# ─────────────────────────── soft-delete + tombstones ───────────────────────────


def test_remove_memory_soft_deletes(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("kill", name="k")
    assert store.remove_memory(mem["id"]) is True
    # Filtered out of the live view...
    assert all(m["id"] != mem["id"] for m in store.load_memories())
    # ...but the tombstone persists in raw.
    raw = store.load_memories_raw()
    tomb = [m for m in raw if m["id"] == mem["id"]][0]
    assert tomb["deleted"] is True
    assert "deleted_ts" in tomb


def test_remove_already_deleted_returns_false(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("x")
    assert store.remove_memory(mem["id"]) is True
    assert store.remove_memory(mem["id"]) is False


def test_remove_missing_returns_false(fresh_store):
    store, _ = fresh_store
    assert store.remove_memory(12345) is False


def test_tombstones_reserve_ids_forever(fresh_store):
    store, _ = fresh_store
    a = store.save_memory("a")
    store.remove_memory(a["id"])
    b = store.save_memory("b")
    # Deleted ID 1 is never reused — monotonic allocation continues from max raw.
    assert b["id"] == 2


def test_clear_soft_deletes_all_live(fresh_store):
    store, _ = fresh_store
    store.save_memory("a")
    store.save_memory("b")
    store.save_memory("c")
    n = store._memories.clear()
    assert n == 3
    assert store.load_memories() == []
    # Next add resumes monotonic IDs past the tombstones.
    nxt = store.save_memory("d")
    assert nxt["id"] == 4


# ─────────────────────────── cap enforcement ───────────────────────────


def test_cap_drops_oldest_live_entries(fresh_store, monkeypatch):
    store, _ = fresh_store
    # Shrink the cap to make the test cheap.
    monkeypatch.setattr(store._memories, "max_entries", 3)
    ids = [store.save_memory(f"m{i}")["id"] for i in range(5)]
    live = store.load_memories()
    assert len(live) == 3
    # Oldest two (ids[0], ids[1]) were dropped; newest three remain.
    live_ids = {m["id"] for m in live}
    assert ids[0] not in live_ids
    assert ids[1] not in live_ids
    assert {ids[2], ids[3], ids[4]} <= live_ids


def test_cap_counts_only_live_not_tombstones(fresh_store, monkeypatch):
    store, _ = fresh_store
    monkeypatch.setattr(store._memories, "max_entries", 3)
    a = store.save_memory("a")
    store.save_memory("b")
    store.remove_memory(a["id"])  # tombstone — doesn't count toward cap
    # Adding two more should not evict, since live count stays <= 3.
    store.save_memory("c")
    store.save_memory("d")
    live = store.load_memories()
    assert len(live) == 3


# ─────────────────────────── search ───────────────────────────


def test_search_memories_matches_text_name_tags_about(fresh_store):
    store, _ = fresh_store
    store.save_memory("nothing here", name="AAPL position")
    store.save_memory("body about MSFT", name="other")
    store.save_memory("plain", tags=["GOOGL"])
    store.save_memory("plain2", about=["earnings"])

    assert len(store.search_memories("aapl")) == 1  # name match, case-insensitive
    assert len(store.search_memories("msft")) == 1  # text match
    assert len(store.search_memories("googl")) == 1  # tag match
    assert len(store.search_memories("earnings")) == 1  # about match
    assert store.search_memories("nonexistent") == []


def test_search_excludes_tombstones(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("findable AAPL")
    store.remove_memory(mem["id"])
    assert store.search_memories("aapl") == []


# ─────────────────────────── filter_memories ───────────────────────────


def test_filter_by_type(fresh_store):
    store, _ = fresh_store
    store.save_memory("u", type="user")
    store.save_memory("p", type="project")
    out = store.filter_memories(type="user")
    assert len(out) == 1
    assert out[0]["type"] == "user"


def test_filter_by_about_or_semantics(fresh_store):
    store, _ = fresh_store
    store.save_memory("a", about=["AAPL", "earnings"])
    store.save_memory("b", about=["MSFT"])
    store.save_memory("c", about=[])
    # OR semantics: matches if ANY about label is in the filter list.
    out = store.filter_memories(about=["AAPL", "GOOGL"])
    assert len(out) == 1
    assert out[0]["text"] == "a"


def test_filter_bot_scoping_hides_other_bots(fresh_store):
    store, _ = fresh_store
    store.save_memory("shared")  # no bot field
    store.save_memory("for-agent-1", bot=["agent-1"])
    store.save_memory("for-agent-2", bot=["agent-2"])

    # Default view as agent-1: shared + agent-1's own, not agent-2's.
    out = store.filter_memories(bot="agent-1")
    texts = {m["text"] for m in out}
    assert texts == {"shared", "for-agent-1"}


def test_filter_bot_none_hides_all_scoped(fresh_store):
    store, _ = fresh_store
    store.save_memory("shared")
    store.save_memory("scoped", bot=["agent-1"])
    # No calling bot → only shared entries are visible.
    out = store.filter_memories(bot=None)
    assert len(out) == 1
    assert out[0]["text"] == "shared"


def test_filter_show_all_bypasses_bot_scoping(fresh_store):
    store, _ = fresh_store
    store.save_memory("shared")
    store.save_memory("scoped", bot=["agent-1"])
    out = store.filter_memories(bot="agent-2", show_all=True)
    assert len(out) == 2


# ─────────────────────────── prompt formatters ───────────────────────────


def test_format_memories_for_prompt_groups_by_type(fresh_store):
    store, _ = fresh_store
    store.save_memory("user fact", type="user", name="profile")
    store.save_memory("proj fact", type="project", name="thing", tags=["x"])
    out = store.format_memories_for_prompt()
    assert "[USER]" in out
    assert "[PROJECT]" in out
    # User section comes before project section (fixed ordering).
    assert out.index("[USER]") < out.index("[PROJECT]")
    assert "user fact" in out
    assert "profile" in out


def test_format_memories_for_prompt_empty_returns_blank(fresh_store):
    store, _ = fresh_store
    assert store.format_memories_for_prompt() == ""


def test_format_memories_for_prompt_type_filter(fresh_store):
    store, _ = fresh_store
    store.save_memory("u", type="user")
    store.save_memory("p", type="project")
    out = store.format_memories_for_prompt(types=["user"])
    assert "[USER]" in out
    assert "[PROJECT]" not in out


def test_format_memories_for_prompt_exclude_types(fresh_store):
    store, _ = fresh_store
    store.save_memory("u", type="user")
    store.save_memory("r", type="reference")
    out = store.format_memories_for_prompt(exclude_types=["reference"])
    assert "[USER]" in out
    assert "[REFERENCE]" not in out


def test_format_index_is_compact_no_body(fresh_store):
    store, _ = fresh_store
    store.save_memory("a very long body that should not appear in the index",
                      type="user", name="short-name", tags=["t1", "t2"])
    out = store.format_memories_index()
    assert "short-name" in out
    assert "a very long body" not in out
    assert "INDEX" in out


# ─────────────────────────── journal ───────────────────────────


def test_add_journal_defaults(fresh_store):
    store, _ = fresh_store
    j = store.add_journal("  shipped a thing  ", actor="agent-1", source="cli")
    assert j["text"] == "shipped a thing"  # trimmed
    assert j["actor"] == "agent-1"
    assert j["source"] == "cli"
    assert j["tags"] == []
    assert j["id"] == 1


def test_edit_journal_fields(fresh_store):
    store, _ = fresh_store
    j = store.add_journal("orig", actor="a")
    assert store.edit_journal(j["id"], text="new", source="discord:general") is True
    cur = [e for e in store.load_journal() if e["id"] == j["id"]][0]
    assert cur["text"] == "new"
    assert cur["source"] == "discord:general"


def test_edit_journal_no_fields_returns_false(fresh_store):
    store, _ = fresh_store
    j = store.add_journal("x")
    assert store.edit_journal(j["id"]) is False


def test_remove_journal_soft_deletes(fresh_store):
    store, _ = fresh_store
    j = store.add_journal("x")
    assert store.remove_journal(j["id"]) is True
    assert store.load_journal() == []


def test_search_journal_matches_text_and_tags(fresh_store):
    store, _ = fresh_store
    store.add_journal("note about AAPL", tags=["trade"])
    store.add_journal("unrelated")
    assert len(store.search_journal("aapl")) == 1
    assert len(store.search_journal("trade")) == 1
    assert store.search_journal("zzz") == []


def test_journal_recent_filters_by_age(fresh_store):
    store, _ = fresh_store
    from datetime import datetime, timezone, timedelta
    j_old = store.add_journal("old")
    store.add_journal("new")
    # Backdate the first entry past the 7-day window via raw save.
    raw = store.load_journal()
    for e in raw:
        if e["id"] == j_old["id"]:
            e["ts"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store._journal.save(raw)

    recent = store.journal_recent(days=7)
    texts = {e["text"] for e in recent}
    assert texts == {"new"}


def test_journal_recent_skips_malformed_ts(fresh_store):
    store, _ = fresh_store
    j = store.add_journal("good")
    raw = store.load_journal()
    raw.append({"id": 999, "ts": "not-a-date", "text": "bad", "tags": [],
                "actor": "", "source": ""})
    store._journal.save(raw)
    recent = store.journal_recent(days=7)
    # Bad timestamp is skipped, not crashed on.
    assert any(e["text"] == "good" for e in recent)
    assert all(e["text"] != "bad" for e in recent)


def test_format_journal_for_prompt(fresh_store):
    store, _ = fresh_store
    store.add_journal("did a thing", actor="agent-1", source="cli")
    out = store.format_journal_for_prompt(days=7)
    assert "did a thing" in out
    assert "agent-1" in out
    assert "cli" in out


def test_format_journal_for_prompt_empty(fresh_store):
    store, _ = fresh_store
    assert store.format_journal_for_prompt() == ""


# ─────────────────────────── cache / persistence ───────────────────────────


def test_data_survives_roundtrip_to_disk(fresh_store):
    store, _ = fresh_store
    store.save_memory("persist me", name="durable", tags=["a"], about=["AAPL"])
    # Read the file straight off disk, bypassing the in-memory cache.
    import json
    with open(store.MEMORIES_FILE) as f:
        on_disk = json.load(f)
    assert len(on_disk) == 1
    assert on_disk[0]["text"] == "persist me"
    assert on_disk[0]["about"] == ["AAPL"]


def test_mtime_cache_invalidation_picks_up_external_write(fresh_store):
    store, _ = fresh_store
    store.save_memory("from this process")
    # Simulate another process appending directly to the JSON file.
    import json
    import time
    with open(store.MEMORIES_FILE) as f:
        data = json.load(f)
    data.append({"id": 999, "ts": "2026-01-01T00:00:00+00:00", "type": "feedback",
                 "name": "external", "tags": [], "text": "from elsewhere", "about": []})
    # Bump mtime so the cache's mtime check sees a change.
    time.sleep(0.01)
    with open(store.MEMORIES_FILE, "w") as f:
        json.dump(data, f)
    os.utime(store.MEMORIES_FILE, None)

    live = store.load_memories()
    assert any(m["text"] == "from elsewhere" for m in live)


def test_load_handles_corrupt_json(fresh_store):
    store, _ = fresh_store
    os.makedirs(store.DATA_DIR, exist_ok=True)
    with open(store.MEMORIES_FILE, "w") as f:
        f.write("{ this is not valid json")
    # Corrupt file degrades to empty list, not a crash.
    assert store.load_memories() == []


def test_load_handles_non_list_json(fresh_store):
    store, _ = fresh_store
    os.makedirs(store.DATA_DIR, exist_ok=True)
    import json
    with open(store.MEMORIES_FILE, "w") as f:
        json.dump({"not": "a list"}, f)
    assert store.load_memories() == []

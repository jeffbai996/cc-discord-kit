"""Flask web UI + HTTP API for cc-discord-kit.

Routes:
  GET    /                    HTML index of memories with filters
  GET    /journal             HTML index of journal entries
  GET    /memory/<id>         HTML detail + edit form
  POST   /memory              Create memory (HTML form post or JSON)
  POST   /memory/<id>         Update via HTML form
  POST   /memory/<id>/delete  Delete via HTML form
  GET    /api/memory          JSON list, accepts ?type=&about=&bot=&q=
  GET    /api/memory/<id>     JSON single
  POST   /api/memory          JSON create
  PUT    /api/memory/<id>     JSON update
  DELETE /api/memory/<id>     Delete
  Same set under /api/journal[/<id>] (no `about` filter for journal)
  GET    /personas            HTML index of bots and their persona slots
  GET    /personas/<bot>/<slot>   HTML detail + edit textarea
  POST   /personas/<bot>/<slot>   Save edits (form post)
  GET    /api/personas        JSON registry of bots and slots
  GET    /api/personas/<bot>/<slot>   JSON {text, mtime, mode, path}
  PUT    /api/personas/<bot>/<slot>   JSON {text} → write file (+ commit if git mode)
  GET    /healthz             "ok"

Auth: none. Bind to localhost only; expose externally via `tailscale serve`
or behind a reverse proxy. The data is personal — never bind to 0.0.0.0
directly.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import rendering  # noqa: E402
import history  # noqa: E402
import merger  # noqa: E402
import personas  # noqa: E402
import digest as digest_mod  # noqa: E402
import inventory  # noqa: E402
import vecgrep_client  # noqa: E402
import files_store  # noqa: E402

app = Flask(__name__, template_folder="templates")
app.config["JSON_AS_ASCII"] = False  # render CJK / emoji as-is

# Behind a reverse proxy with a stripped path prefix (e.g. tailscale serve
# --set-path=/multiagent), the browser hits /multiagent/... but Flask
# receives /...  Set CCDK_URL_PREFIX=/multiagent so url_for() emits
# the public paths.
_url_prefix = os.environ.get("CCDK_URL_PREFIX", "").rstrip("/")
if _url_prefix:
    app.config["APPLICATION_ROOT"] = _url_prefix

    class _PrefixMiddleware:
        def __init__(self, wsgi_app, prefix):
            self.wsgi_app = wsgi_app
            self.prefix = prefix

        def __call__(self, environ, start_response):
            environ["SCRIPT_NAME"] = self.prefix
            return self.wsgi_app(environ, start_response)

    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, _url_prefix)

# Trust tailscale serve's X-Forwarded-Proto / X-Forwarded-Host so
# url_for(_external=True) generates correct https URLs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)


# Optional vecgrep UI URL — when set, _base.html renders a nav link to it.
VECGREP_UI_URL = os.environ.get("VECGREP_UI_URL", "").strip()


@app.context_processor
def _inject_globals():
    return {"vecgrep_ui_url": VECGREP_UI_URL}


def _discord_target_from_request() -> tuple[str | None, str | None]:
    """Resolve optional Discord card target from JSON body or query args."""
    body = request.get_json(silent=True) or {}
    chat_id = (
        body.get("discord_chat_id")
        or request.args.get("discord_chat_id")
        or ""
    )
    message_id = (
        body.get("discord_message_id")
        or request.args.get("discord_message_id")
        or ""
    )
    return (str(chat_id).strip() or None, str(message_id).strip() or None)


def _post_card(action: dict) -> None:
    """Best-effort Discord confirmation card for API/HTTP-mode CLI calls."""
    chat_id, message_id = _discord_target_from_request()
    if not chat_id:
        return
    try:
        import discord_card
        ok, err = discord_card.post_action_card(
            action,
            chat_id,
            reply_to=message_id,
            user_agent="cc-discord-kit-api (1.0)",
        )
        if not ok and err:
            print(f"[cc-discord-kit card post failed] {err}", file=sys.stderr)
    except Exception as e:
        print(f"[cc-discord-kit card post crashed] {type(e).__name__}: {e}",
              file=sys.stderr)


# ─────────────────────────── HTML routes ───────────────────────────


def _search_memories(
    *, q: str, mode: str, type_filter: str | None,
    about_filter: list[str], bot_filter: str | None, show_all: bool,
) -> tuple[list[dict], dict[int, float], dict[int, list[str]], str, str]:
    """Shared search/filter pipeline for both `/` (HTML) and `/api/search` (JSON).

    Returns (entries, semantic_scores, matched_by, warning, effective_mode).
    effective_mode may differ from requested mode if vecgrep was unavailable
    (semantic/hybrid degrade to literal).
    """
    semantic_scores: dict[int, float] = {}
    matched_by: dict[int, list[str]] = {}
    warning = ""
    all_memories = store.load_memories()
    by_id = {m["id"]: m for m in all_memories}

    def _filter_and_sort(entries: list) -> list:
        e = store.filter_memories(
            entries, type=type_filter, about=about_filter or None,
            bot=bot_filter, show_all=show_all,
        )
        return sorted(
            e,
            key=lambda m: (0 if m.get("pinned") else 1, -m.get("id", 0)),
        )

    if not q:
        return _filter_and_sort(list(all_memories)), {}, {}, "", mode

    if mode == "literal":
        return _filter_and_sort(store.search_memories(q)), {}, {}, "", mode

    # semantic or hybrid — both need vecgrep
    try:
        triples = vecgrep_client.search_corpus_with_matches(
            q, vecgrep_client.VECGREP_CORPUS_MEMORIES, want_kind="memory",
        )
    except vecgrep_client.VecgrepUnavailable as e:
        return (
            _filter_and_sort(store.search_memories(q)),
            {}, {},
            f"Vecgrep unavailable ({e}). Falling back to literal.",
            "literal",
        )

    semantic_scores = {eid: pct for eid, pct, _ in triples}
    matched_by = {eid: list(m) for eid, _, m in triples}

    if mode == "semantic":
        ranked = [by_id[eid] for eid, _, _ in triples if eid in by_id]
        ranked = store.filter_memories(
            ranked, type=type_filter, about=about_filter or None,
            bot=bot_filter, show_all=show_all,
        )
        order = {eid: i for i, (eid, _, _) in enumerate(triples)}
        return sorted(ranked, key=lambda m: order.get(m["id"], 1_000_000)), \
            semantic_scores, matched_by, "", mode

    # hybrid: semantic ranking + literal-match boost, union with literal hits
    sem_ids = {eid for eid, _, _ in triples}
    literal_hits = store.search_memories(q)
    literal_ids = {m["id"] for m in literal_hits}
    for lid in literal_ids:
        tags = set(matched_by.get(lid, []))
        tags.add("bm25")
        matched_by[lid] = sorted(tags)
        if lid in sem_ids:
            semantic_scores[lid] = min(100.0, semantic_scores[lid] + 5.0)

    combined_ids: list[int] = []
    for eid, _, _ in triples:
        if eid in by_id:
            combined_ids.append(eid)
    for m in sorted(literal_hits, key=lambda x: -x.get("id", 0)):
        if m["id"] not in sem_ids:
            combined_ids.append(m["id"])

    entries = [by_id[i] for i in combined_ids if i in by_id]
    entries = store.filter_memories(
        entries, type=type_filter, about=about_filter or None,
        bot=bot_filter, show_all=show_all,
    )
    order = {i: idx for idx, i in enumerate(combined_ids)}
    return sorted(entries, key=lambda m: order.get(m["id"], 1_000_000)), \
        semantic_scores, matched_by, "", mode


@app.route("/")
def index():
    type_filter = request.args.get("type") or None
    about_filter = [a for a in request.args.getlist("about") if a]
    bot_filter = request.args.get("bot") or None
    show_all = request.args.get("all", "").lower() in ("1", "true", "on", "yes")
    q = (request.args.get("q") or "").strip()

    # Back-compat: ?semantic=1 (old checkbox) maps to mode=semantic.
    raw_mode = (request.args.get("mode") or "").lower()
    if raw_mode in ("literal", "semantic", "hybrid"):
        mode = raw_mode
    elif request.args.get("semantic", "").lower() in ("1", "true", "on", "yes"):
        mode = "semantic"
    else:
        mode = "literal"

    entries, semantic_scores, matched_by, warning, mode = _search_memories(
        q=q, mode=mode, type_filter=type_filter, about_filter=about_filter,
        bot_filter=bot_filter, show_all=show_all,
    )

    all_entries = store.load_memories()
    types = sorted({m.get("type", "") for m in all_entries if m.get("type")})
    abouts = sorted({a for m in all_entries for a in m.get("about", []) if a})
    bots = sorted({b for m in all_entries for b in (m.get("bot") or []) if b})

    return render_template(
        "index.html",
        entries=entries,
        total=len(all_entries),
        type_filter=type_filter,
        about_filter=about_filter,
        bot_filter=bot_filter,
        show_all=show_all,
        mode=mode,
        semantic=(mode in ("semantic", "hybrid")),  # back-compat for any old template ref
        semantic_available=vecgrep_client.is_available(),
        semantic_scores=semantic_scores,
        matched_by=matched_by,
        semantic_warning=warning,
        q=q,
        types=types,
        abouts=abouts,
        bots=bots,
    )


@app.route("/journal")
def journal_index():
    # Two views toggled by ?view=: 'moments' (timeline, default) and 'todos'
    # (the open/done checklist). Both read journal.json; todos are kind=='todo'.
    view = (request.args.get("view") or "moments").lower()
    if view == "todos":
        open_todos = store.list_todos(status="open")
        import datetime as _dt
        _today = _dt.date.today()
        for _t in open_todos:
            _due = (_t.get("due") or "").strip()
            _t["due_status"] = ""
            if _due:
                try:
                    _d = _dt.date.fromisoformat(_due[:10])
                    if _d < _today:
                        _t["due_status"] = "over"
                    elif (_d - _today).days <= 3:
                        _t["due_status"] = "soon"
                except ValueError:
                    pass
        return render_template(
            "journal.html",
            view="todos",
            entries=[],
            open_todos=open_todos,
            done_todos=store.list_todos(status="done"),
            todo_priorities=store.TODO_PRIORITIES,
            todo_note_max=store.TODO_NOTE_MAX,
            days=0, q="", semantic=False,
            semantic_available=False, semantic_scores={}, semantic_warning="",
        )

    days = request.args.get("days", type=int) or 0
    q = (request.args.get("q") or "").strip()
    semantic = request.args.get("semantic", "").lower() in ("1", "true", "on", "yes")
    semantic_scores: dict[int, float] = {}
    semantic_warning: str = ""

    if q and semantic:
        try:
            id_pct_pairs = vecgrep_client.search_corpus_to_ids(
                q,
                vecgrep_client.VECGREP_CORPUS_JOURNAL,
                want_kind="journal",
            )
            semantic_scores = dict(id_pct_pairs)
            ranked_ids = [eid for eid, _ in id_pct_pairs]
            by_id = {e["id"]: e for e in store.load_journal()}
            entries = [by_id[i] for i in ranked_ids if i in by_id]
        except vecgrep_client.VecgrepUnavailable as e:
            semantic_warning = (
                f"Vecgrep unavailable ({e}). Falling back to literal substring search."
            )
            entries = store.search_journal(q)
            entries = sorted(entries, key=lambda e: e.get("id", 0), reverse=True)
    elif q:
        entries = store.search_journal(q)
        entries = sorted(entries, key=lambda e: e.get("id", 0), reverse=True)
    elif days:
        entries = store.journal_recent(days)
        entries = sorted(entries, key=lambda e: e.get("id", 0), reverse=True)
    else:
        entries = store.load_journal()
        entries = sorted(entries, key=lambda e: e.get("id", 0), reverse=True)
    # Moments timeline excludes todos (they have their own ?view=todos).
    entries = [e for e in entries if e.get("kind") != "todo"]
    return render_template(
        "journal.html",
        view="moments",
        entries=entries,
        open_todos=[], done_todos=[],
        days=days,
        q=q,
        semantic=semantic,
        semantic_available=vecgrep_client.is_available(),
        semantic_scores=semantic_scores,
        semantic_warning=semantic_warning,
    )


@app.route("/journal/<int:entry_id>/todo", methods=["POST"])
def journal_todo_status(entry_id: int):
    """Click-to-complete (and reopen) for a todo from the /journal todo-view."""
    status = (request.form.get("status") or "done").lower()
    if status in store.TODO_STATUSES:
        store.set_todo_status(entry_id, status, editor="web")
    return redirect(url_for("journal_index", view="todos"))


@app.route("/journal/<int:entry_id>/todo/edit", methods=["POST"])
def journal_todo_edit(entry_id: int):
    """Inline edit of a todo's fields (text/note/priority/due/flag). Each field
    is applied only when present in the form, so a partial form doesn't blank
    the rest. Kind-safe in the store layer (won't touch a moment)."""
    f = request.form
    if "text" in f and f.get("text", "").strip():
        store.set_todo_text(entry_id, f["text"], editor="web")
    if "note" in f:
        store.set_todo_note(entry_id, f.get("note", ""), editor="web")
    if "priority" in f:
        store.set_todo_priority(entry_id, f.get("priority", "none"), editor="web")
    if "due" in f:
        store.set_todo_due(entry_id, f.get("due", ""), editor="web")
    if "flag" in f:
        # getlist: hidden flag=0 + (when checked) flag=1, so unchecking persists.
        store.set_todo_flag(entry_id, "1" in f.getlist("flag"), editor="web")
    if "tags" in f:
        tags = [t.strip() for t in f.get("tags", "").split(",") if t.strip()]
        store.set_todo_tags(entry_id, tags, editor="web")
    return redirect(url_for("journal_index", view="todos"))


@app.route("/memory/<int:memory_id>", methods=["GET", "POST"])
def memory_detail(memory_id: int):
    m = next((x for x in store.load_memories() if x.get("id") == memory_id), None)
    if not m:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "edit")
        if action == "delete":
            history.remove_memory_with_history(memory_id)
            return redirect(url_for("index"))
        history.edit_memory_with_history(
            memory_id,
            text=request.form.get("text"),
            name=request.form.get("name"),
            type=request.form.get("type"),
            tags=_parse_csv(request.form.get("tags", "")),
            about=_parse_csv(request.form.get("about", "")),
            bot=_parse_csv(request.form.get("bot", "")) or None,
            pinned=request.form.get("pinned") == "1",
        )
        return redirect(url_for("memory_detail", memory_id=memory_id))
    backlinks = rendering.find_backlinks(
        memory_id, "memory", store.load_memories(), store.load_journal()
    )
    return render_template("memory_detail.html", m=m,
                           valid_types=sorted(store.VALID_TYPES),
                           backlinks=backlinks)


@app.route("/memory/new", methods=["GET", "POST"])
def memory_new():
    if request.method == "POST":
        m = store.save_memory(
            request.form.get("text", "").strip(),
            type=request.form.get("type") or "feedback",
            name=request.form.get("name") or "",
            tags=_parse_csv(request.form.get("tags", "")),
            about=_parse_csv(request.form.get("about", "")),
            bot=_parse_csv(request.form.get("bot", "")) or None,
        )
        return redirect(url_for("memory_detail", memory_id=m["id"]))
    return render_template("memory_new.html",
                           valid_types=sorted(store.VALID_TYPES))


@app.route("/memory/<int:memory_id>/delete", methods=["POST"])
def memory_delete_form(memory_id: int):
    history.remove_memory_with_history(memory_id)
    return redirect(url_for("index"))


@app.route("/memory/<int:memory_id>/pin", methods=["POST"])
def memory_pin(memory_id: int):
    m = next((x for x in store.load_memories() if x.get("id") == memory_id), None)
    if m:
        store.edit_memory(memory_id, pinned=not m.get("pinned", False))
    return redirect(request.referrer or url_for("index"))


def _find_memory_dict(memory_id: int) -> dict | None:
    return next((x for x in store.load_memories() if x.get("id") == memory_id), None)


def _find_journal_dict(entry_id: int) -> dict | None:
    return next((x for x in store.load_journal() if x.get("id") == entry_id), None)


@app.route("/memory/<int:winner_id>/merge", methods=["GET", "POST"])
def memory_merge(winner_id: int):
    """Merge another memory into this one (winner). Loser gets soft-deleted,
    backlinks rewritten."""
    winner = _find_memory_dict(winner_id)
    if not winner:
        abort(404)
    loser_id = (request.values.get("loser", type=int)
                or request.values.get("loser_id", type=int))

    # Picker: no loser yet, GET
    if request.method == "GET" and not loser_id:
        others = [m for m in store.load_memories() if m.get("id") != winner_id]
        others.sort(key=lambda m: -m.get("id", 0))
        return render_template("merge_pick.html", winner=winner, others=others)

    if not loser_id or loser_id == winner_id:
        abort(404)
    loser = _find_memory_dict(loser_id)
    if not loser:
        abort(404)

    backlinks = rendering.find_backlinks(
        loser_id, "memory", store.load_memories(), store.load_journal()
    )

    if request.method == "GET":
        suggested = merger.suggest_merged_text(
            winner.get("text", ""), loser.get("text", ""),
            loser_id=loser_id, loser_name=loser.get("name", ""),
        )
        return render_template(
            "merge_preview.html",
            winner=winner, loser=loser,
            suggested=suggested, backlinks=backlinks,
        )

    # POST — apply merge
    final_text = request.form.get("text", "")
    history.edit_memory_with_history(winner_id, text=final_text, actor="merge")

    for ref in backlinks.get("memories", []):
        m = _find_memory_dict(ref["id"])
        if not m:
            continue
        new_text, count = merger.rewrite_refs(m.get("text", ""), loser_id, winner_id)
        if count > 0:
            history.edit_memory_with_history(
                ref["id"], text=new_text, actor="merge:rewrite"
            )
    for ref in backlinks.get("journal", []):
        e = _find_journal_dict(ref["id"])
        if not e:
            continue
        new_text, count = merger.rewrite_refs(e.get("text", ""), loser_id, winner_id)
        if count > 0:
            history.edit_journal_with_history(
                ref["id"], text=new_text, actor="merge:rewrite"
            )

    history.remove_memory_with_history(loser_id, actor="merge")
    return redirect(url_for("memory_detail", memory_id=winner_id))


@app.route("/journal/new", methods=["GET", "POST"])
def journal_new():
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        if text:
            e = store.add_journal(
                text,
                source=request.form.get("source", "web") or "web",
                actor=request.form.get("actor", "") or "",
                tags=_parse_csv(request.form.get("tags", "")),
            )
            return redirect(url_for("journal_detail", entry_id=e["id"]))
    return render_template("journal_new.html")


@app.route("/journal/<int:entry_id>", methods=["GET", "POST"])
def journal_detail(entry_id: int):
    e = next((x for x in store.load_journal() if x.get("id") == entry_id), None)
    if not e:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "edit")
        if action == "delete":
            history.remove_journal_with_history(entry_id)
            return redirect(url_for("journal_index"))
        history.edit_journal_with_history(
            entry_id,
            text=request.form.get("text"),
            actor=request.form.get("actor"),
            source=request.form.get("source"),
            tags=_parse_csv(request.form.get("tags", "")),
        )
        return redirect(url_for("journal_detail", entry_id=entry_id))
    backlinks = rendering.find_backlinks(
        entry_id, "journal", store.load_memories(), store.load_journal()
    )
    return render_template("journal_detail.html", e=e, backlinks=backlinks)


@app.route("/journal/<int:entry_id>/delete", methods=["POST"])
def journal_delete_form(entry_id: int):
    history.remove_journal_with_history(entry_id)
    return redirect(url_for("journal_index"))


# ─────────────────────────── history & trash ───────────────────────────


@app.route("/memory/<int:memory_id>/history")
def memory_history(memory_id: int):
    m = next((x for x in store.load_memories() if x.get("id") == memory_id), None)
    if not m:
        abort(404)
    entries = list(reversed(history.load_history("memory", memory_id)))
    return render_template("history.html", target=m, target_kind="memory",
                           entries=entries)


@app.route("/journal/<int:entry_id>/history")
def journal_history(entry_id: int):
    e = next((x for x in store.load_journal() if x.get("id") == entry_id), None)
    if not e:
        abort(404)
    entries = list(reversed(history.load_history("journal", entry_id)))
    return render_template("history.html", target=e, target_kind="journal",
                           entries=entries)


@app.route("/history/revert", methods=["POST"])
def history_revert():
    kind = request.form.get("kind", "")
    target_id = int(request.form.get("id", "0"))
    ts = request.form.get("ts", "")
    if not kind or not target_id or not ts:
        abort(400)
    # find the matching history entry
    for h in history.load_history(kind, target_id):
        if h.get("ts") == ts:
            history.revert_edit(h)
            break
    if kind == "memory":
        return redirect(url_for("memory_detail", memory_id=target_id))
    return redirect(url_for("journal_detail", entry_id=target_id))


@app.route("/trash")
def trash():
    deletes = history.load_recent_deletes(limit=100)
    return render_template("trash.html", entries=deletes)


@app.route("/trash/restore", methods=["POST"])
def trash_restore():
    ts = request.form.get("ts", "")
    kind = request.form.get("kind", "")
    target_id = int(request.form.get("id", "0"))
    if not ts:
        abort(400)
    # find matching delete record
    for h in history.load_recent_deletes(limit=500):
        if h.get("ts") == ts and h.get("kind") == kind and h.get("id") == target_id:
            history.restore_deleted(h)
            break
    return redirect(url_for("trash"))


@app.route("/trash/purge", methods=["POST"])
def trash_purge():
    """Permanently remove ONE trash entry from edits.jsonl.

    Distinct from restore: restore reinstates the record, purge wipes the
    delete-history line so the record is unrecoverable. Matches on
    (kind, id, ts) so the right line is targeted even if the same id was
    deleted multiple times historically.
    """
    ts = request.form.get("ts", "")
    kind = request.form.get("kind", "")
    target_id = int(request.form.get("id", "0"))
    if not ts:
        abort(400)
    for h in history.load_recent_deletes(limit=500):
        if h.get("ts") == ts and h.get("kind") == kind and h.get("id") == target_id:
            history.purge_history_entry(h)
            break
    return redirect(url_for("trash"))


@app.route("/trash/purge-all", methods=["POST"])
def trash_purge_all():
    """Permanently remove EVERY trash entry from edits.jsonl in one shot.

    Tombstones in memories.json / journal.json are preserved, so monotonic
    ID allocation is untouched — the next save still gets `max(id over
    all incl tombstones) + 1`. The only thing lost is the ability to
    restore_deleted() these records.
    """
    history.purge_all_deletes()
    return redirect(url_for("trash"))


# ─────────────────────────── JSON API ───────────────────────────


@app.route("/api/memory", methods=["GET", "POST"])
def api_memory_collection():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        text = body.get("text") or body.get("body") or ""
        if not text:
            return jsonify({"ok": False, "error": "missing text"}), 400
        name = body.get("name", "")
        if len(name) > 200:
            return jsonify({
                "ok": False,
                "error": "invalid name: name cannot exceed 200 characters",
            }), 400
        mem_type = body.get("type", "feedback")
        if mem_type not in store.VALID_TYPES:
            return jsonify({
                "ok": False,
                "error": "invalid type: must be feedback, project, reference, or user",
            }), 400
        tags = _normalize_list(body.get("tags") or [])
        if len(tags) > 20:
            return jsonify({
                "ok": False,
                "error": "invalid tags: cannot exceed 20 entries",
            }), 400
        m = store.save_memory(
            text,
            type=mem_type,
            name=name,
            tags=tags,
            about=_normalize_list(body.get("about") or []),
            bot=_normalize_list(body.get("bot")) if body.get("bot") is not None else None,
        )
        _post_card({"kind": "memory_saved", "entry": m})
        return jsonify({"ok": True, "memory": m}), 201

    type_filter = request.args.get("type") or None
    about_filter = [a for a in request.args.getlist("about") if a]
    bot_filter = request.args.get("bot") or None
    show_all = request.args.get("all", "").lower() in ("1", "true", "on", "yes")
    q = (request.args.get("q") or "").strip()
    if q:
        entries = store.search_memories(q)
        entries = store.filter_memories(
            entries, type=type_filter, about=about_filter or None,
            bot=bot_filter, show_all=show_all,
        )
    else:
        entries = store.filter_memories(
            type=type_filter, about=about_filter or None,
            bot=bot_filter, show_all=show_all,
        )
    return jsonify({"ok": True, "memories": entries, "count": len(entries)})


@app.route("/api/search")
def api_search():
    """Live-search JSON endpoint backing the index page's debounced input.

    Returns filtered + ranked memory entries plus per-entry semantic hints
    when the mode requires them. The HTML view stays server-rendered on
    initial page load; this is the in-page re-render path.

    Query params:
        q          - search string (empty allowed; returns all filtered entries)
        mode       - literal | semantic | hybrid (default literal)
        type       - memory type filter
        about      - repeatable; AND-style narrow
        bot        - bot scope filter
        all        - "1" to include bot-scoped (default off)

    Response:
        {ok, mode, count, total, semantic_available, entries: [...],
         semantic_scores: {id: pct}, matched_by: {id: ["vector","bm25"]},
         warning: "..."}
    """
    type_filter = request.args.get("type") or None
    about_filter = [a for a in request.args.getlist("about") if a]
    bot_filter = request.args.get("bot") or None
    show_all = request.args.get("all", "").lower() in ("1", "true", "on", "yes")
    mode = (request.args.get("mode") or "literal").lower()
    if mode not in ("literal", "semantic", "hybrid"):
        mode = "literal"
    q = (request.args.get("q") or "").strip()

    entries, semantic_scores, matched_by, warning, mode = _search_memories(
        q=q, mode=mode, type_filter=type_filter, about_filter=about_filter,
        bot_filter=bot_filter, show_all=show_all,
    )
    total = len(store.load_memories())

    # Slim payload for the client — only fields the row renderer needs.
    def slim(m: dict) -> dict:
        return {
            "id": m.get("id"),
            "type": m.get("type"),
            "name": m.get("name"),
            "text": m.get("text", ""),
            "tags": m.get("tags") or [],
            "about": m.get("about") or [],
            "bot": m.get("bot") or [],
            "pinned": bool(m.get("pinned")),
            "ts": m.get("ts", ""),
            # Tier + attribution so the in-page re-render keeps the dots/byline.
            # No tier backend in this build; absent tier renders Live.
            "tier": m.get("tier") or "Live",
            "author": m.get("author") or "",
            "last_editor": m.get("last_editor") or "",
        }

    return jsonify({
        "ok": True,
        "mode": mode,
        "q": q,
        "count": len(entries),
        "total": total,
        "semantic_available": vecgrep_client.is_available(),
        "entries": [slim(m) for m in entries],
        "semantic_scores": {str(k): round(v, 1) for k, v in semantic_scores.items()},
        "matched_by": {str(k): v for k, v in matched_by.items()},
        "warning": warning,
    })


@app.route("/api/memory/<int:memory_id>", methods=["GET", "PUT", "DELETE"])
def api_memory_item(memory_id: int):
    if request.method == "GET":
        m = next((x for x in store.load_memories()
                  if x.get("id") == memory_id), None)
        if not m:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "memory": m})
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        before = _find_memory_dict(memory_id)
        ok = history.edit_memory_with_history(
            memory_id,
            actor="api",
            text=body.get("text"),
            name=body.get("name"),
            type=body.get("type"),
            tags=_normalize_list(body.get("tags")) if body.get("tags") is not None else None,
            about=_normalize_list(body.get("about")) if body.get("about") is not None else None,
            bot=_normalize_list(body.get("bot")) if body.get("bot") is not None else None,
            pinned=body.get("pinned"),
        )
        if not ok:
            return jsonify({"ok": False, "error": "not found or no fields"}), 404
        after = _find_memory_dict(memory_id)
        _post_card({
            "kind": "memory_edited", "id": memory_id,
            "before": before, "after": after,
        })
        return jsonify({"ok": True})
    # DELETE
    before = _find_memory_dict(memory_id)
    ok = history.remove_memory_with_history(memory_id, actor="api")
    if ok:
        _post_card({"kind": "memory_deleted", "before": before})
    return (jsonify({"ok": ok}),
            200 if ok else 404)


@app.route("/api/journal", methods=["GET", "POST"])
def api_journal_collection():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        text = body.get("text") or body.get("body") or ""
        if not text:
            return jsonify({"ok": False, "error": "missing text"}), 400
        e = store.add_journal(
            text,
            source=body.get("source", "api"),
            actor=body.get("actor", ""),
            tags=_normalize_list(body.get("tags") or []),
        )
        _post_card({"kind": "journal_added", "entry": e})
        return jsonify({"ok": True, "entry": e}), 201

    days = request.args.get("days", type=int) or 0
    q = (request.args.get("q") or "").strip()
    if q:
        entries = store.search_journal(q)
    elif days:
        entries = store.journal_recent(days)
    else:
        entries = store.load_journal()
    return jsonify({"ok": True, "entries": entries, "count": len(entries)})


@app.route("/api/journal/<int:entry_id>", methods=["GET", "PUT", "DELETE"])
def api_journal_item(entry_id: int):
    if request.method == "GET":
        e = next((x for x in store.load_journal()
                  if x.get("id") == entry_id), None)
        if not e:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "entry": e})
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        before = _find_journal_dict(entry_id)
        ok = history.edit_journal_with_history(
            entry_id,
            actor="api",
            text=body.get("text"),
            entry_actor=body.get("actor"),
            source=body.get("source"),
            tags=_normalize_list(body.get("tags")) if body.get("tags") is not None else None,
        )
        if not ok:
            return jsonify({"ok": False, "error": "not found or no fields"}), 404
        after = _find_journal_dict(entry_id)
        _post_card({
            "kind": "journal_edited", "id": entry_id,
            "before": before, "after": after,
        })
        return jsonify({"ok": True})
    before = _find_journal_dict(entry_id)
    ok = history.remove_journal_with_history(entry_id, actor="api")
    if ok:
        _post_card({"kind": "journal_deleted", "before": before})
    return (jsonify({"ok": ok}),
            200 if ok else 404)


# ─────────────────────────── personas ───────────────────────────


@app.route("/personas")
def personas_index():
    bots = []
    for bot in personas.list_bots():
        slots = []
        for slot in personas.get_files(bot):
            data = personas.read_slot(bot, slot["slot"])
            preview = data["text"][:200].strip()
            slots.append({
                **slot,
                "mtime": data["mtime"],
                "preview": preview,
                "missing": not data["text"] and data["mtime"] is None,
            })
        bots.append({"name": bot, "slots": slots})
    return render_template("personas_index.html", bots=bots)


@app.route("/personas/<bot>/<slot>", methods=["GET", "POST"])
def personas_detail(bot: str, slot: str):
    try:
        personas._resolve(bot, slot)
    except KeyError:
        abort(404)

    saved = None
    error = None
    if request.method == "POST":
        text = request.form.get("text", "")
        result = personas.write_slot(bot, slot, text)
        if result["error"]:
            error = result["error"]
        else:
            saved = {"committed": result["committed"], "sha": result["sha"]}

    data = personas.read_slot(bot, slot)
    return render_template(
        "personas_detail.html",
        bot=bot, slot=slot, data=data, saved=saved, error=error,
    )


@app.route("/api/personas")
def api_personas_index():
    return jsonify({
        bot: personas.get_files(bot) for bot in personas.list_bots()
    })


@app.route("/api/personas/<bot>/<slot>", methods=["GET", "PUT"])
def api_personas_item(bot: str, slot: str):
    try:
        personas._resolve(bot, slot)
    except KeyError:
        return jsonify({"ok": False, "error": "unknown bot/slot"}), 404

    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        if "text" not in body:
            return jsonify({"ok": False, "error": "missing 'text'"}), 400
        return jsonify(personas.write_slot(bot, slot, body["text"]))

    return jsonify(personas.read_slot(bot, slot))


# ─────────────────────────── infrastructure inventory ───────────────────────────


@app.route("/inventory")
def inventory_index():
    """Live inventory of hooks/crons/services across configured hosts.

    Probes whatever transports `inventory.gather()` sets up — by default
    just the local host. Edit `inventory.py` to add SSH-based probes for
    other hosts. Cached server-side ~30s so reloading doesn't hammer.
    """
    data = inventory.gather()
    return render_template("inventory.html", data=data)


@app.route("/api/inventory")
def api_inventory():
    return jsonify(inventory.gather())


# ─────────────────────────── digest review ───────────────────────────


@app.route("/digest", methods=["GET", "POST"])
def digest_review():
    """Pull-model digest: render last 24h of channel messages for review.

    No LLM. User reads, types a digest paragraph, submits → journal entry
    with type tagged via source="digest:<channel-csv>". Token-less render
    surfaces a setup hint instead of failing.
    """
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        if text:
            channels = request.form.get("channels") or ",".join(digest_mod.DIGEST_CHANNELS.keys())
            actor = (request.form.get("actor", "") or "user").strip()
            tags = _parse_csv(request.form.get("tags", "digest"))
            if "digest" not in tags:
                tags.append("digest")
            e = store.add_journal(
                text,
                source=f"digest:{channels}",
                actor=actor,
                tags=tags,
            )
            return redirect(url_for("journal_detail", entry_id=e["id"]))
        # empty submit — fall through and re-render
    try:
        hours = int(request.args.get("hours", digest_mod.DEFAULT_HOURS))
    except ValueError:
        hours = digest_mod.DEFAULT_HOURS
    hours = max(1, min(hours, 168))  # clamp 1h..7d

    msgs = digest_mod.fetch_window(hours=hours)
    s = digest_mod.stats(msgs)
    token_present = digest_mod._load_token() is not None
    return render_template(
        "digest.html",
        msgs=msgs,
        stats=s,
        hours=hours,
        token_present=token_present,
        channels=digest_mod.DIGEST_CHANNELS,
    )


# Global cooldown for the summarize endpoint. Single-user infra so per-IP
# isn't worth the complexity; one timestamp gate is enough to prevent
# accidental rapid-fire clicks (each call is ~5-15K input tokens on flash).
_LAST_SUMMARIZE_TS: dict[str, float] = {"t": 0.0}
SUMMARIZE_COOLDOWN_SEC = 30.0


@app.route("/digest/summarize", methods=["POST"])
def digest_summarize():
    """Generate an LLM summary of the current digest window for the textarea.

    Returns JSON {text: "..."} on success, {error: "..."} on failure with a
    non-200 status. Pinned to gemini-3-flash-preview (see digest.py); no
    persona/voice — pure factual recap. Rate-limited to once per
    SUMMARIZE_COOLDOWN_SEC to bound accidental burn.
    """
    import time as _time
    now = _time.monotonic()
    elapsed = now - _LAST_SUMMARIZE_TS["t"]
    if elapsed < SUMMARIZE_COOLDOWN_SEC:
        retry_in = int(SUMMARIZE_COOLDOWN_SEC - elapsed) + 1
        return {
            "error": f"cooldown — try again in {retry_in}s",
            "retry_after_sec": retry_in,
        }, 429
    try:
        hours = int(request.form.get("hours") or request.args.get("hours") or digest_mod.DEFAULT_HOURS)
    except ValueError:
        hours = digest_mod.DEFAULT_HOURS
    hours = max(1, min(hours, 168))
    msgs = digest_mod.fetch_window(hours=hours)
    if not msgs:
        return {"error": "no messages in window"}, 400
    # Stamp the timestamp BEFORE calling the API so a slow gemini call still
    # blocks the next concurrent click. If summarize fails the cooldown still
    # applies; the user can retry on next window.
    _LAST_SUMMARIZE_TS["t"] = now
    try:
        text = digest_mod.summarize_messages(msgs)
    except RuntimeError as e:
        return {"error": str(e)}, 500
    return {"text": text, "msg_count": len(msgs)}


@app.route("/healthz")
def healthz():
    return "ok", 200


# ─────────────────────────── files ───────────────────────────


def _file_summary(rec: dict) -> dict:
    """Metadata-only view of a file record (drops inline content from list
    responses so a listing isn't bloated by file bodies)."""
    return {k: v for k, v in rec.items() if k != "content"}


@app.route("/api/files", methods=["GET", "POST"])
def api_files_collection():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "missing name"}), 400
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            rec = files_store.add_file(
                name,
                content=body.get("content"),
                type=body.get("type", "") or "",
                # Security: do NOT trust a client-supplied mime — an attacker
                # could set text/html / image/svg+xml to weaponize inline
                # rendering. Always derive it server-side from the name's
                # extension (files_store._mime_for does this when mime=None).
                mime=None,
                tags=tags,
                about=body.get("about") or [],
                bot=body.get("bot"),
                actor=body.get("actor", "") or "api",
                storage=body.get("storage"),
            )
        except files_store.FileTooLarge as e:
            return jsonify({"ok": False, "error": str(e)}), 413
        except files_store.StoreFull as e:
            return jsonify({"ok": False, "error": str(e)}), 507
        _post_card({"kind": "file_added", "entry": _file_summary(rec)})
        return jsonify({"ok": True, "file": _file_summary(rec)}), 201

    q = (request.args.get("q") or "").strip()
    entries = files_store.search_files(q) if q else files_store.load_files()
    return jsonify({
        "ok": True,
        "files": [_file_summary(f) for f in entries],
        "count": len(entries),
    })


@app.route("/api/files/<int:file_id>", methods=["GET", "PUT", "DELETE"])
def api_file_item(file_id: int):
    if request.method == "GET":
        rec = files_store.get_file(file_id)
        if not rec:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "file": rec})
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        try:
            ok = files_store.edit_file(
                file_id,
                name=body.get("name"),
                content=body.get("content"),
                type=body.get("type"),
                mime=None,  # never accept a client-supplied mime (see POST)
                tags=body.get("tags"),
                about=body.get("about"),
                bot=body.get("bot"),
            )
        except files_store.FileTooLarge as e:
            return jsonify({"ok": False, "error": str(e)}), 413
        return jsonify({"ok": ok}), (200 if ok else 404)
    ok = files_store.remove_file(file_id)
    if ok:
        _post_card({"kind": "file_deleted", "id": file_id})
    return jsonify({"ok": ok}), (200 if ok else 404)


@app.route("/api/files/<int:file_id>/raw")
def api_file_raw(file_id: int):
    """Raw bytes of a file (inline or blob), with its recorded mime type."""
    import re
    from flask import Response
    rec = files_store.get_file(file_id)
    if not rec:
        abort(404)
    data = files_store.read_file_content(file_id)
    if data is None:
        abort(404)
    # Security: this serves user-uploaded bytes on the app origin. Serving
    # untrusted content inline with an attacker-influenced content-type is
    # stored XSS (an uploaded .html/.svg executing JS in the app origin).
    # Defenses:
    #   - only a small allowlist of provably-INERT types may render inline;
    #     everything else (notably text/html, image/svg+xml — both can script)
    #     is forced to octet-stream + attachment
    #   - inline disposition is opt-in via ?inline=1 (the preview panel asks
    #     for it); the default stays attachment so a bare link always downloads
    #   - nosniff so the browser can't second-guess the type back to html
    #
    # NB: SVG is deliberately NOT inline-safe — it's an XML document that can
    # carry <script>/onload. PDF/raster/audio/video are inert renderers.
    INLINE_SAFE_MIMES = {
        "text/plain", "application/json", "image/png", "image/jpeg",
        "image/gif", "image/webp", "application/pdf",
        "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4",
        "video/mp4", "video/webm", "video/quicktime",
    }
    mime = rec.get("mime") or "application/octet-stream"
    want_inline = request.args.get("inline") in ("1", "true", "yes")
    inline_ok = want_inline and mime in INLINE_SAFE_MIMES
    if not inline_ok:
        mime = "application/octet-stream"  # defang to a download
    safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", rec.get("name") or "file")
    disposition = "inline" if inline_ok else "attachment"
    resp = Response(data, mimetype=mime)
    resp.headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # CSP by path. `sandbox` sandboxes the resource into an opaque origin, which
    # BLOCKS rendering it as an <img> — so inline-safe inert types get a
    # no-sandbox CSP (still loads nothing of its own). The defang/attachment
    # path keeps the full sandboxed CSP, where the XSS risk actually lives.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'" if inline_ok else "default-src 'none'; sandbox"
    )
    return resp


@app.route("/files")
def files_index():
    q = (request.args.get("q") or "").strip()
    entries = files_store.search_files(q) if q else files_store.load_files()
    entries = sorted(entries, key=lambda f: f.get("id", 0), reverse=True)
    # Grouping (by folder/type/date) is done CLIENT-SIDE from the per-card
    # data-* attributes, so the server just emits the flat pool with the keys.
    for f in entries:
        f["category"] = files_store.file_category(f.get("mime"), f.get("name"))
        f["folder"] = files_store.folder_of(f.get("name") or "")
        f["basename"] = files_store.basename_of(f.get("name") or "")
    return render_template("files.html", files=entries, q=q,
                           categories=files_store.FILE_CATEGORIES,
                           total=len(files_store.load_files()))


@app.route("/files/new", methods=["GET", "POST"])
def file_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        upload = request.files.get("upload")
        content = request.form.get("content")
        if upload and upload.filename:
            raw = upload.read()
            if not name:
                name = upload.filename
            if files_store._is_text_ext(name):  # text inline, binary → blob
                try:
                    content = raw.decode("utf-8")
                    blob = None
                except UnicodeDecodeError:
                    content, blob = None, raw
            else:
                content, blob = None, raw
        else:
            blob = None
        if not name:
            abort(400)
        try:
            rec = files_store.add_file(
                name, content=content, blob_bytes=blob,
                tags=_parse_csv(request.form.get("tags", "")),
                about=_parse_csv(request.form.get("about", "")),
                bot=_parse_csv(request.form.get("bot", "")) or None,
                actor="web",
            )
        except files_store.FileTooLarge:
            abort(413)
        except files_store.StoreFull:
            abort(507)
        _post_card({"kind": "file_added", "entry": _file_summary(rec)})
        return redirect(url_for("file_detail", file_id=rec["id"]))
    return render_template("file_new.html")


@app.route("/files/<int:file_id>", methods=["GET", "POST"])
def file_detail(file_id: int):
    rec = files_store.get_file(file_id)
    if not rec:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "edit")
        if action == "delete":
            files_store.remove_file(file_id)
            _post_card({"kind": "file_deleted", "id": file_id})
            return redirect(url_for("files_index"))
        try:
            files_store.edit_file(
                file_id,
                name=request.form.get("name"),
                content=request.form.get("content"),
                tags=_parse_csv(request.form.get("tags", "")),
                about=_parse_csv(request.form.get("about", "")),
            )
        except files_store.FileTooLarge:
            abort(413)
        return redirect(url_for("file_detail", file_id=file_id))
    rec["category"] = files_store.file_category(rec.get("mime"), rec.get("name"))
    rec["folder"] = files_store.folder_of(rec.get("name") or "")
    rec["basename"] = files_store.basename_of(rec.get("name") or "")
    return render_template("file_detail.html", f=rec)


@app.route("/files/<int:file_id>/delete", methods=["POST"])
def file_delete_form(file_id: int):
    files_store.remove_file(file_id)
    _post_card({"kind": "file_deleted", "id": file_id})
    return redirect(url_for("files_index"))


# ───────────── context (SHARED.md rules doc + per-agent brain files) ─────────────


def _bot_file_cards() -> list[dict]:
    """Per-agent brain-file cards shown on /context below SHARED.md. Each agent
    has one or more slots — its CLAUDE.md / persona.md, configured in
    agents.yaml. Reuses personas.py (the same module /personas uses)."""
    cards = []
    for bot in personas.list_bots():
        slots = []
        for slot in personas.get_files(bot):
            data = personas.read_slot(bot, slot["slot"])
            preview = (data["text"] or "")[:200].strip()
            slots.append({
                **slot,
                "mtime": data["mtime"],
                "preview": preview,
                "missing": not data["text"] and data["mtime"] is None,
            })
        cards.append({"name": bot, "slots": slots})
    return cards


@app.route("/context", methods=["GET", "POST"])
def context_doc():
    """View / edit SHARED.md — the global rulebook injected into every agent at
    session start (see store.format_shared_doc_for_prompt) — plus the per-agent
    brain-file editor cards folded in below it."""
    saved = error = None
    if request.method == "POST":
        try:
            store.write_shared_doc(request.form.get("text", ""))
            saved = True
        except OSError as e:
            error = str(e)
    return render_template(
        "context.html",
        text=store.read_shared_doc(),
        path=store.SHARED_DOC_FILE,
        saved=saved,
        error=error,
        bot_cards=_bot_file_cards(),
    )


@app.route("/context/file/<bot>/<slot>", methods=["GET", "POST"])
def context_file(bot: str, slot: str):
    """Edit one agent's brain file (CLAUDE.md / persona.md). Folded under
    /context; mirrors the standalone /personas/<bot>/<slot> page."""
    try:
        personas._resolve(bot, slot)
    except KeyError:
        abort(404)
    saved = error = None
    if request.method == "POST":
        result = personas.write_slot(bot, slot, request.form.get("text", ""))
        if result.get("error"):
            error = result["error"]
        else:
            saved = {"committed": result.get("committed"), "sha": result.get("sha")}
    data = personas.read_slot(bot, slot)
    return render_template(
        "context_file.html",
        bot=bot, slot=slot, data=data, saved=saved, error=error,
    )


@app.route("/api/context", methods=["GET", "PUT"])
def api_context():
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        if "text" not in body:
            return jsonify({"ok": False, "error": "missing 'text'"}), 400
        try:
            store.write_shared_doc(body["text"])
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
    return jsonify({"text": store.read_shared_doc(), "path": store.SHARED_DOC_FILE})


# ─────────────────────────── helpers ───────────────────────────


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _parse_csv(value)
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _highlight(text: str, query: str | None):
    """Wrap occurrences of query in <mark>. Case-insensitive, HTML-escaped."""
    from markupsafe import Markup, escape
    if not text:
        return ""
    if not query:
        return escape(text)
    import re
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    out = []
    last = 0
    for m in pattern.finditer(text):
        out.append(escape(text[last:m.start()]))
        out.append(Markup("<mark>"))
        out.append(escape(text[m.start():m.end()]))
        out.append(Markup("</mark>"))
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


# Display-only relabel of memory types. Store still uses raw "user"/"feedback"/etc.
# UI shows "profile" instead of "user" because the user-type tag is used for
# profiles of people in general, not the bot's caller.
_TYPE_DISPLAY_LABEL = {
    "user": "profile",
}


def _type_label(value: str | None) -> str:
    return _TYPE_DISPLAY_LABEL.get(value or "", value or "—")


# Make helpers available in templates
app.jinja_env.globals["parse_csv"] = _parse_csv
app.jinja_env.filters["highlight"] = _highlight
app.jinja_env.filters["render_md"] = lambda text: rendering.render_body(text, _url_prefix)
app.jinja_env.filters["type_label"] = _type_label


def main() -> None:
    host = os.environ.get("CCDK_HOST", "127.0.0.1")
    port = int(os.environ.get("CCDK_PORT", "5005"))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()

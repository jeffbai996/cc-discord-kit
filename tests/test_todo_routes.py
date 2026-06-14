"""Route tests for the /journal To-dos view + inline editor (Reminders revamp)."""
from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_DATA_DIR", str(tmp_path))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    for mod in ("store", "history", "server"):
        if mod in sys.modules:
            del sys.modules[mod]
    store = importlib.import_module("store")
    importlib.import_module("history")
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return server.app.test_client(), store


def test_todos_view_renders_switcher_and_fields(client):
    c, store = client
    store.add_todo("rich", priority="high", flag=True, note="the detail")
    html = c.get("/journal?view=todos").get_data(as_text=True)
    assert "To-dos" in html and "Entries" in html
    assert "⚑" in html and "the detail" in html
    assert "todo-editor-" in html
    assert f'maxlength="{store.TODO_NOTE_MAX}"' in html


def test_edit_post_applies_fields(client):
    c, store = client
    t = store.add_todo("orig")
    r = c.post(f"/journal/{t['id']}/todo/edit", data={
        "text": "edited", "note": "n", "priority": "med", "due": "2026-09-01", "flag": "1"})
    assert r.status_code == 302
    got = next(x for x in store.list_todos() if x["id"] == t["id"])
    assert got["text"] == "edited" and got["note"] == "n"
    assert got["priority"] == "med" and got["flag"] is True


def test_edit_partial_preserves(client):
    c, store = client
    t = store.add_todo("keep", note="keep note", priority="high")
    c.post(f"/journal/{t['id']}/todo/edit", data={"text": "keep", "flag": "1"})
    got = next(x for x in store.list_todos() if x["id"] == t["id"])
    assert got["flag"] is True and got["note"] == "keep note" and got["priority"] == "high"


def test_edit_kind_safe(client):
    c, store = client
    store.add_journal("a moment")     # id 1
    store.add_todo("a todo")          # todo id 1
    c.post("/journal/1/todo/edit", data={"text": "a todo", "note": "todo note"})
    moment = next(e for e in store.load_journal() if e["id"] == 1 and e.get("kind") != "todo")
    assert "note" not in moment

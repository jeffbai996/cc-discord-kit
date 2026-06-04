"""Web-UI surfacing of memory attribution (author/last_editor) + a
forward-compatible lifecycle tier dot.

This (public) build of the store has NO tier backend — no set_tier, no
VALID_TIERS, no tier eviction. So the UI work here is limited to what the
data model actually supports:

  - author / last_editor byline on the list + detail views
  - a tier dot rendered defensively from `m.get('tier') or 'Live'`, so a
    memory carrying a `tier` field (e.g. synced from a tier-aware build)
    colors correctly, and an absent tier renders Live (green)

The interactive tier control + the dedicated set_tier endpoint live only in
the tier-aware build; there is nothing to wire them to here.

Generic agent names only (public repo: no personal data in tests).
"""

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
    for mod in ("store", "history", "rendering", "server"):
        if mod in sys.modules:
            del sys.modules[mod]
    store = importlib.import_module("store")
    importlib.import_module("history")
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return server.app.test_client(), store


def test_detail_page_shows_author_byline(client):
    c, store = client
    m = store.save_memory("body text", name="m", author="agent-1")
    store.edit_memory(m["id"], text="body v2", editor="agent-2")
    r = c.get(f"/memory/{m['id']}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "agent-1" in html      # author
    assert "agent-2" in html      # last_editor (differs)


def test_detail_page_renders_tier_pill(client):
    c, store = client
    m = store.save_memory("body text", name="m", author="agent-1")
    r = c.get(f"/memory/{m['id']}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # tier renders as a pill (absent tier => Live), not a dot
    assert "pill tier-live" in html
    assert "tier-dot" not in html


def test_index_page_renders_tier_pill(client):
    c, store = client
    store.save_memory("body text", name="m", author="agent-1")
    r = c.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "pill tier-live" in html
    assert "tier-dot" not in html


def test_index_byline_shows_author(client):
    c, store = client
    store.save_memory("body text", name="m", author="agent-1")
    r = c.get("/")
    assert r.status_code == 200
    assert "agent-1" in r.get_data(as_text=True)


def test_api_search_slim_includes_author(client):
    c, store = client
    store.save_memory("findme body", name="findme", author="agent-1")
    r = c.get("/api/search?q=findme")
    entry = next(e for e in r.get_json()["entries"] if e["name"] == "findme")
    assert entry["author"] == "agent-1"

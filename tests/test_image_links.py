"""Image-linking layer (#107): a memory/journal entry can declare
`images: [file_id]`; the linked files surface when the entry is shown. Storage
+ HTTP serving already exist in files_store — this is the link layer on top."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_DATA_DIR", str(tmp_path))
    import importlib
    for m in ("store", "files_store"):
        if m in sys.modules:
            del sys.modules[m]
    store = importlib.import_module("store")
    fs = importlib.import_module("files_store")
    return store, fs


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bf6cba000000"
    "0049454e44ae426082")


def _img(fs, name="pic.png"):
    return fs.add_file(name, blob_bytes=PNG, actor="test")


# ─── link / unlink on memories ───

def test_link_image_to_memory(stores):
    store, fs = stores
    m = store.save_memory("a memory", name="m")
    f = _img(fs)
    assert store.link_image("memory", m["id"], f["id"]) is True
    got = next(x for x in store.load_memories() if x["id"] == m["id"])
    assert got["images"] == [f["id"]]


def test_link_image_dedups(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    f = _img(fs)
    store.link_image("memory", m["id"], f["id"])
    store.link_image("memory", m["id"], f["id"])  # again → no dup
    got = next(x for x in store.load_memories() if x["id"] == m["id"])
    assert got["images"] == [f["id"]]


def test_link_rejects_missing_file(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    assert store.link_image("memory", m["id"], 9999) is False  # no such file
    got = next(x for x in store.load_memories() if x["id"] == m["id"])
    assert got.get("images", []) == []


def test_link_rejects_missing_memory(stores):
    store, fs = stores
    f = _img(fs)
    assert store.link_image("memory", 9999, f["id"]) is False


def test_unlink_image(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    f1, f2 = _img(fs, "a.png"), _img(fs, "b.png")
    store.link_image("memory", m["id"], f1["id"])
    store.link_image("memory", m["id"], f2["id"])
    assert store.unlink_image("memory", m["id"], f1["id"]) is True
    got = next(x for x in store.load_memories() if x["id"] == m["id"])
    assert got["images"] == [f2["id"]]


def test_unlink_absent_is_false(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    assert store.unlink_image("memory", m["id"], 1) is False


# ─── link on journal ───

def test_link_image_to_journal(stores):
    store, fs = stores
    j = store.add_journal("a moment")
    f = _img(fs)
    assert store.link_image("journal", j["id"], f["id"]) is True
    got = next(x for x in store.load_journal() if x["id"] == j["id"])
    assert got["images"] == [f["id"]]


def test_bad_kind_rejected(stores):
    store, fs = stores
    f = _img(fs)
    assert store.link_image("nope", 1, f["id"]) is False


# ─── resolve helper: ids → file records for rendering ───

def test_resolve_linked_images(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    f = _img(fs, "cat.png")
    store.link_image("memory", m["id"], f["id"])
    recs = store.resolve_linked_images("memory", m["id"])
    assert len(recs) == 1
    assert recs[0]["id"] == f["id"] and recs[0]["name"] == "cat.png"
    assert recs[0]["mime"] == "image/png"


def test_resolve_skips_deleted_file(stores):
    store, fs = stores
    m = store.save_memory("m", name="m")
    f = _img(fs)
    store.link_image("memory", m["id"], f["id"])
    fs.remove_file(f["id"])  # file gone, link dangling
    # resolve drops the dangling id rather than returning a None/broken rec
    assert store.resolve_linked_images("memory", m["id"]) == []


# ─── web: link/unlink + gallery render on the memory detail page ───

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_DATA_DIR", str(tmp_path))
    import importlib
    for m in ("store", "files_store", "history", "server"):
        if m in sys.modules:
            del sys.modules[m]
    store = importlib.import_module("store")
    fs = importlib.import_module("files_store")
    importlib.import_module("history")
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return server.app.test_client(), store, fs


def test_web_link_unlink_and_gallery(client):
    c, store, fs = client
    m = store.save_memory("hike", name="hike", type="reference")
    f = fs.add_file("pic.png", blob_bytes=PNG, actor="t")
    # link via the detail POST (the Mac/HTTP path)
    c.post(f"/memory/{m['id']}", data={"action": "link-image", "file_id": f["id"]})
    html = c.get(f"/memory/{m['id']}").get_data(as_text=True)
    assert '<section class="linked-images">' in html      # the SECTION, not the CSS
    assert f"/api/files/{f['id']}/raw" in html and "<img" in html
    # unlink
    c.post(f"/memory/{m['id']}", data={"action": "unlink-image", "file_id": f["id"]})
    html2 = c.get(f"/memory/{m['id']}").get_data(as_text=True)
    assert "<img" not in html2                              # thumbnail gone
    assert next(x for x in store.load_memories() if x["id"] == m["id"]).get("images") == []


def test_web_link_picker_available_and_links(client):
    c, store, fs = client
    m = store.save_memory("m", name="m", type="reference")
    f = fs.add_file("pic.png", blob_bytes=PNG, actor="t")
    import re
    html = c.get(f"/memory/{m['id']}").get_data(as_text=True)
    assert re.search(r'<form[^>]*class="img-link-form"', html)
    assert f'value="{f["id"]}"' in html
    c.post(f"/memory/{m['id']}", data={"action": "link-image", "file_id": f["id"]})
    html2 = c.get(f"/memory/{m['id']}").get_data(as_text=True)
    assert not re.search(r'<form[^>]*class="img-link-form"', html2)
    assert "<img" in html2

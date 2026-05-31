"""Tests for the files store + browser routes.

Covers the file-category mapping (drives pills + preview dispatch), and the
raw-bytes endpoint's inline-vs-attachment disposition, which is an XSS
boundary, not cosmetics:
  - inline rendering is allowed ONLY for provably-inert types (raster images,
    pdf, audio, video) and ONLY when ?inline=1 is requested
  - active types (svg can script; html obviously) are NEVER inline — forced to
    octet-stream + attachment even with ?inline=1
  - inline images carry a no-sandbox CSP (the `sandbox` directive blocked <img>
    rendering); the defang/attachment path keeps the strict sandboxed CSP
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def fresh_files(tmp_path, monkeypatch):
    """Reload files_store + server pointed at a clean tmp dir."""
    monkeypatch.setenv("CCDK_DATA_DIR", str(tmp_path))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    for mod in ("store", "files_store", "rendering", "server"):
        if mod in sys.modules:
            del sys.modules[mod]
    importlib.import_module("store")
    fs = importlib.import_module("files_store")
    server = importlib.import_module("server")
    server.app.config["TESTING"] = True
    return fs, server.app.test_client()


def _add(fs, name, content=None, blob=None):
    return fs.add_file(name, content=content, blob_bytes=blob, actor="test")


# ─────────────── file_category (pill + preview dispatch) ───────────────

def test_category_images(fresh_files):
    fs, _ = fresh_files
    for name in ("a.png", "b.jpg", "c.jpeg", "d.gif", "e.webp", "f.svg"):
        assert fs.file_category(fs._mime_for(name), name) == "image", name


def test_category_document_code_data_media_other(fresh_files):
    fs, _ = fresh_files
    assert fs.file_category("application/pdf", "x.pdf") == "document"
    assert fs.file_category(fs._mime_for("s.py"), "s.py") == "code"
    assert fs.file_category(fs._mime_for("d.json"), "d.json") == "data"
    assert fs.file_category("audio/mpeg", "s.mp3") == "media"
    assert fs.file_category("application/octet-stream", "x.xyzzy") == "other"


def test_category_mime_beats_misleading_extension(fresh_files):
    fs, _ = fresh_files
    assert fs.file_category("image/png", "actually_image.txt") == "image"


# ─────────────── raw endpoint disposition (XSS boundary) ───────────────

def test_raw_default_is_attachment(fresh_files):
    fs, c = fresh_files
    rec = _add(fs, "notes.txt", content="hello")
    r = c.get(f"/api/files/{rec['id']}/raw")
    assert "attachment" in r.headers["Content-Disposition"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_raw_inline_image_renders_without_sandbox(fresh_files):
    """Inline raster must NOT carry `sandbox` (it blocked <img> rendering)."""
    fs, c = fresh_files
    rec = _add(fs, "pic.jpg", blob=b"\xff\xd8\xff\xe0fake")
    r = c.get(f"/api/files/{rec['id']}/raw?inline=1")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/jpeg")
    assert r.headers["Content-Disposition"].startswith("inline")
    assert "sandbox" not in r.headers.get("Content-Security-Policy", "")


def test_raw_inline_svg_is_defanged(fresh_files):
    """SVG can script — never inline, even with the flag."""
    fs, c = fresh_files
    rec = _add(fs, "x.svg", content='<svg onload="alert(1)"></svg>')
    r = c.get(f"/api/files/{rec['id']}/raw?inline=1")
    assert "attachment" in r.headers["Content-Disposition"]
    assert r.headers["Content-Type"].startswith("application/octet-stream")
    assert "sandbox" in r.headers.get("Content-Security-Policy", "")


# ─────────────── browser pages ───────────────

def test_files_index_and_detail_render(fresh_files):
    fs, c = fresh_files
    _add(fs, "a.png", blob=b"\x89PNG")
    rec = _add(fs, "readme.md", content="# Title")
    assert c.get("/files").status_code == 200
    assert c.get(f"/files/{rec['id']}").status_code == 200


def test_preview_image_uses_inline_img(fresh_files):
    fs, c = fresh_files
    rec = _add(fs, "pic.png", blob=b"\x89PNG\r\n")
    body = c.get(f"/files/{rec['id']}").get_data(as_text=True)
    assert "<img" in body and "inline=1" in body


def test_preview_code_uses_vendored_highlight(fresh_files):
    fs, c = fresh_files
    rec = _add(fs, "app.py", content="def f(): return 1")
    body = c.get(f"/files/{rec['id']}").get_data(as_text=True)
    assert "hljs-block" in body
    assert "vendor/highlight.min.js" in body  # vendored, not CDN
    assert "cdnjs" not in body


# ─────────────── context (SHARED.md rules doc) ───────────────

def test_context_doc_renders_and_saves(fresh_files):
    """The /context page renders, and POSTing saves SHARED.md."""
    fs, c = fresh_files
    import store
    assert c.get("/context").status_code == 200
    assert c.post("/context", data={"text": "# Rules\nbe kind"}).status_code == 200
    assert store.read_shared_doc().strip() == "# Rules\nbe kind"


def test_api_context_get_put(fresh_files):
    fs, c = fresh_files
    import store
    r = c.put("/api/context", json={"text": "shared body"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert store.read_shared_doc() == "shared body"
    assert c.get("/api/context").get_json()["text"] == "shared body"


# ─────────────── folders (derived path model + client-side group-by) ───────────────

def test_folder_helpers(fresh_files):
    fs, _ = fresh_files
    assert fs.folder_of("projects/foo/notes.md") == "projects/foo"
    assert fs.folder_of("notes.md") == ""
    assert fs.basename_of("projects/foo/notes.md") == "notes.md"
    rec = fs.add_file("Projects/My Notes/Draft.md", content="x")
    assert rec["slug"] == "projects/my-notes/draft.md"  # slug preserves path


def test_blob_path_safe_with_pathy_name(fresh_files):
    fs, _ = fresh_files
    rec = fs.add_file("a/b/c/pic.png", blob_bytes=b"\x89PNG", actor="test")
    assert "/a/b/c/" not in (rec.get("blob_path") or "")
    assert rec["blob_path"].endswith(f"{rec['id']}.png")


def test_files_index_emits_group_selector_and_data(fresh_files):
    fs, c = fresh_files
    _add(fs, "projects/foo/a.md", content="1")
    body = c.get("/files").get_data(as_text=True)
    assert 'id="group-select"' in body
    assert 'data-folder="projects/foo"' in body
    assert "data-cat=" in body and "data-date=" in body


def test_detail_breadcrumb_for_foldered_file(fresh_files):
    fs, c = fresh_files
    rec = _add(fs, "projects/foo/notes.md", content="# hi")
    body = c.get(f"/files/{rec['id']}").get_data(as_text=True)
    assert "📁 foo" in body and "notes.md" in body

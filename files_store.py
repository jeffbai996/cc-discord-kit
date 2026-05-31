"""Squad files store — shared "project files" the whole squad can read.

A third tier alongside memories (atomic durable facts) and journal (moments):
whole *documents* — references, specs, deep-dives, datasets — too big to be a
memory and not a moment. Think the deep-tier `.md` reference files, but
squad-wide and droppable at runtime, à la claude.ai Projects.

Storage is two-mode behind one record shape:

  - storage="inline"  → text content lives in the JSON record (`content`).
                        Used for .md/.txt/.json/code. Working now (mode A).
  - storage="blob"    → bytes live on disk at files/<id>.<ext>; the record
                        holds only metadata (`blob_path`, `sha256`). Scaffolded
                        now (mode B) so PDFs/images/any-claude.ai-type — and
                        later multimodal access for gemini/gpt — drop in
                        without a rewrite. See TODO(B) markers.

Record shape (superset serving both modes):
  {id, ts, name, slug, type, mime, size, sha256, storage,
   content?, blob_path?, tags[], about[], bot[]?, actor}

Metadata index reuses JsonStore (same IDs/tombstones/cache as memories). The
byte budget is enforced here, not in JsonStore, so a big upload is rejected
loudly rather than silently evicting older files the way a count-cap would.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from store import DATA_DIR, JsonStore  # reuse the same data dir + store engine

log = logging.getLogger("ccdk.files")

FILES_FILE = os.path.join(DATA_DIR, "files.json")
BLOB_DIR = os.path.join(DATA_DIR, "files")  # blobs land next to the JSON index

# Caps: generous, not limiting normal use, but bounded so disk can't blow up.
MAX_FILE_BYTES = 100 * 1024 * 1024          # 100 MB per file
MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024    # 5 GB total store budget
FILES_CAP = 5000                            # generous metadata-row cap

# Anything whose content is meaningfully text is stored inline; everything else
# routes to a blob. We decide by extension + a heuristic, but a caller can force
# storage mode explicitly.
TEXT_EXTS = {
    "md", "markdown", "txt", "text", "json", "yaml", "yml", "toml", "csv",
    "tsv", "html", "htm", "css", "js", "ts", "py", "sh", "bash", "rs", "go",
    "c", "h", "cpp", "java", "rb", "sql", "xml", "ini", "cfg", "conf", "log",
}

_MIME_BY_EXT = {
    # text / markdown
    "md": "text/markdown", "markdown": "text/markdown", "txt": "text/plain",
    "text": "text/plain", "log": "text/plain",
    # data
    "json": "application/json", "yaml": "application/yaml", "yml": "application/yaml",
    "toml": "application/toml", "csv": "text/csv", "tsv": "text/tab-separated-values",
    "xml": "application/xml", "ini": "text/plain", "cfg": "text/plain", "conf": "text/plain",
    # code / markup
    "html": "text/html", "htm": "text/html", "css": "text/css",
    "js": "text/javascript", "ts": "text/x-typescript", "py": "text/x-python",
    "sh": "text/x-shellscript", "bash": "text/x-shellscript",
    "rs": "text/x-rust", "go": "text/x-go", "c": "text/x-c", "h": "text/x-c",
    "cpp": "text/x-c++", "java": "text/x-java", "rb": "text/x-ruby", "sql": "text/x-sql",
    # documents
    "pdf": "application/pdf",
    # images
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "svg": "image/svg+xml",
    # media
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "m4a": "audio/mp4",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
}


_files = JsonStore(FILES_FILE, FILES_CAP)


# ─────────────────────────── helpers ───────────────────────────

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s or "file"


def _ext_of(name: str) -> str:
    base = os.path.basename(name or "")
    _, dot, ext = base.rpartition(".")
    return ext.lower() if dot else ""


def _mime_for(name: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return _MIME_BY_EXT.get(_ext_of(name), "application/octet-stream")


def _is_text_ext(name: str) -> bool:
    return _ext_of(name) in TEXT_EXTS


# Extension sets that pin a category when mime is ambiguous (octet-stream) or
# when an ext is more specific than the mime. mime is authoritative for the
# image/audio/video families; these refine the text-ish middle.
_DATA_EXTS = {"json", "csv", "tsv", "xml", "yaml", "yml", "toml"}
_DOC_EXTS = {"pdf"}

# The five UI categories, in legend display order: (key, label, emoji, css color).
# The template renders one page-level legend from this list and colors each
# pill by its key (.pill.cat-<key>). Single source of truth for both.
FILE_CATEGORIES = [
    ("image", "Images", "🟢", "#16a34a"),
    ("document", "Documents", "🔴", "#dc2626"),
    ("code", "Code/Text", "🔵", "#2563eb"),
    ("data", "Data", "🟣", "#7c3aed"),
    ("media", "Audio/Video", "🟠", "#ea580c"),
    ("other", "Other", "⬜", "#6b7280"),
]


def file_category(mime: str | None, name: str | None = None) -> str:
    """Map a file to one of five UI/preview categories:

        image · document · code · data · media · other

    Drives both the colored pill on the browser and which preview renderer
    the detail page reaches for. mime is AUTHORITATIVE for the binary media
    families (image/audio/video) — a real PNG misnamed `.txt` is still an
    image. For the text-ish middle (code vs data) we lean on the extension,
    since `text/plain` covers both a Python script and a CSV; the
    data-extension set wins there so JSON/CSV/XML get the data treatment.
    """
    m = (mime or "").lower()
    ext = _ext_of(name or "")

    # 1) Binary media families — trust mime first (survives a wrong ext).
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/") or m.startswith("video/"):
        return "media"
    if m == "application/pdf" or ext in _DOC_EXTS:
        return "document"

    # 2) Structured data — JSON/CSV/XML/YAML/TOML, by mime or ext.
    if (
        m in ("application/json", "application/xml", "text/csv",
              "text/tab-separated-values", "application/yaml", "application/toml")
        or ext in _DATA_EXTS
    ):
        return "data"

    # 3) Code / plaintext — anything text-ish that isn't data.
    if m.startswith("text/") or ext in TEXT_EXTS:
        return "code"

    # 4) Everything else (unknown / true binary).
    return "other"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _current_total_bytes() -> int:
    return sum(int(e.get("size") or 0) for e in _files.load())


def load_files() -> list[dict]:
    return _files.load()


def get_file(file_id: int) -> dict | None:
    return next((f for f in _files.load() if f.get("id") == file_id), None)


def read_file_content(file_id: int) -> bytes | None:
    """Return the raw bytes of a file, inline or blob. None if missing.

    Inline → utf-8 encode of the stored text. Blob → read from disk (mode B).
    """
    rec = get_file(file_id)
    if not rec:
        return None
    if rec.get("storage") == "inline":
        return (rec.get("content") or "").encode("utf-8")
    # TODO(B): blob read path — verify sha256 on read, stream for big files.
    blob_path = rec.get("blob_path")
    if not blob_path or not os.path.exists(blob_path):
        log.warning("blob missing for file #%s at %s", file_id, blob_path)
        return None
    try:
        with open(blob_path, "rb") as f:
            return f.read()
    except OSError as e:
        log.warning("blob read failed for #%s: %s", file_id, e)
        return None


class FileTooLarge(Exception):
    pass


class StoreFull(Exception):
    pass


def add_file(
    name: str,
    *,
    content: str | None = None,
    blob_bytes: bytes | None = None,
    type: str = "",
    mime: str | None = None,
    tags: list[str] | None = None,
    about: list[str] | None = None,
    bot: list[str] | None = None,
    actor: str = "",
    storage: str | None = None,
) -> dict:
    """Add a file. Provide EITHER `content` (text → inline) or `blob_bytes`
    (any type → blob, mode B). If neither, an empty inline text file is made.

    `storage` forces the mode; otherwise it's inferred from the extension
    (text exts → inline, everything else → blob).

    Raises FileTooLarge / StoreFull on cap violations (loud, never silent).
    """
    name = (name or "").strip() or "untitled"
    ext = _ext_of(name)
    file_type = (type or ext or "txt").lower()
    file_mime = _mime_for(name, mime)

    # Decide storage mode.
    if storage not in ("inline", "blob", None):
        storage = None
    if storage is None:
        if blob_bytes is not None:
            storage = "blob"
        elif content is not None:
            storage = "inline"
        else:
            storage = "inline" if _is_text_ext(name) else "blob"

    # Compute size + sha256 from whichever payload we have.
    if storage == "inline":
        text = content or ""
        data = text.encode("utf-8")
        size = len(data)
        sha = _sha256_bytes(data)
    else:
        data = blob_bytes if blob_bytes is not None else b""
        size = len(data)
        sha = _sha256_bytes(data)

    # Cap enforcement — loud failures, no silent eviction.
    if size > MAX_FILE_BYTES:
        raise FileTooLarge(
            f"{name} is {size} bytes; per-file cap is {MAX_FILE_BYTES} "
            f"({MAX_FILE_BYTES // (1024*1024)}MB)"
        )
    projected = _current_total_bytes() + size
    if projected > MAX_TOTAL_BYTES:
        log.warning(
            "files store would exceed total budget: %s + %s > %s",
            _current_total_bytes(), size, MAX_TOTAL_BYTES,
        )
        raise StoreFull(
            f"adding {name} ({size} bytes) would exceed the "
            f"{MAX_TOTAL_BYTES // (1024*1024*1024)}GB store budget"
        )

    fields: dict = {
        "name": name,
        "slug": _slugify(name),
        "type": file_type,
        "mime": file_mime,
        "size": size,
        "sha256": sha,
        "storage": storage,
        "tags": list(tags) if tags else [],
        "about": list(about) if about else [],
        "actor": actor or "",
    }
    if storage == "inline":
        fields["content"] = content or ""
    if bot is not None:
        fields["bot"] = list(bot)

    rec = _files.add(fields)

    if storage == "blob":
        # TODO(B): write bytes to BLOB_DIR/<id>.<ext>, fsync, and on failure
        # roll back the metadata row. Multimodal access for gemini/gpt later
        # = handing those bots this path / bytes + the recorded mime.
        os.makedirs(BLOB_DIR, exist_ok=True)
        blob_path = os.path.join(BLOB_DIR, f"{rec['id']}.{ext or 'bin'}")
        if blob_bytes is not None:
            try:
                tmp = blob_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(blob_bytes)
                os.replace(tmp, blob_path)
            except OSError as e:
                log.warning("blob write failed for #%s: %s", rec["id"], e)
        # Record the path even if bytes weren't supplied yet (scaffold upload).
        _files.update(rec["id"], {"blob_path": blob_path})
        rec["blob_path"] = blob_path

    return rec


def edit_file(
    file_id: int,
    *,
    name: str | None = None,
    content: str | None = None,
    type: str | None = None,
    mime: str | None = None,
    tags: list[str] | None = None,
    about: list[str] | None = None,
    bot: list[str] | None = None,
) -> bool:
    """Edit metadata and, for inline files, content. Recomputes size/sha256
    when content changes. (Blob byte-replacement is a TODO(B) — edit metadata
    only for blobs here.)"""
    rec = get_file(file_id)
    if rec is None:
        return False
    fields: dict = {}
    if name is not None:
        fields["name"] = name.strip()
        fields["slug"] = _slugify(name)
        # Re-derive mime from the new name unless one is explicitly given,
        # so a rename can't leave a stale (or attacker-favorable) mime. The
        # API never passes a client mime (mime=None), so this always wins there.
        if mime is None:
            fields["mime"] = _mime_for(name)
    if type is not None:
        fields["type"] = type.strip().lower()
    if mime is not None:
        fields["mime"] = mime.strip()
    if tags is not None:
        fields["tags"] = list(tags)
    if about is not None:
        fields["about"] = list(about)
    if bot is not None:
        fields["bot"] = list(bot)
    if content is not None and rec.get("storage") == "inline":
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLarge(
                f"content is {len(data)} bytes; per-file cap is {MAX_FILE_BYTES}"
            )
        fields["content"] = content
        fields["size"] = len(data)
        fields["sha256"] = _sha256_bytes(data)
    if not fields:
        return False
    return _files.update(file_id, fields)


def remove_file(file_id: int) -> bool:
    """Soft-delete the metadata row (tombstone). The blob (if any) is left on
    disk — TODO(B): a trash-purge sweep can reap orphaned blobs."""
    return _files.remove(file_id)


def search_files(term: str) -> list[dict]:
    t = term.lower()
    out = []
    for f in load_files():
        if (
            t in (f.get("name") or "").lower()
            or t in (f.get("type") or "").lower()
            or any(t in tag.lower() for tag in f.get("tags", []))
            or any(t in a.lower() for a in f.get("about", []))
            or (f.get("storage") == "inline" and t in (f.get("content") or "").lower())
        ):
            out.append(f)
    return out

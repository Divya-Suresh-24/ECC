"""Document library operations used by the faculty side (and the seed script)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import db
from .config import settings
from .ingest import chunk, extract, file_type_for, sha256_bytes

CATEGORIES = [
    "Protocols & SOPs",
    "Hospital policies",
    "Handbook & reference",
    "Imaging & radiology",
    "Lectures & rounds",
    "Other",
]


def guess_category(filename: str) -> str:
    n = filename.lower()
    if n.endswith(".pptx") or any(k in n for k in ("vodcast", "seminar", "rounds_", "intern rounds", "lecture")):
        return "Lectures & rounds"
    if any(k in n for k in ("positioning", "radiograph", "x-ray", "imaging", "vitalrads", "radiology", "ultrasound", "cvis")):
        return "Imaging & radiology"
    if "handbook" in n or "checklist" in n or "info sheet" in n:
        return "Handbook & reference"
    if any(k in n for k in ("policy", "payment", "good samaritan", "surrender", "crisis", "boostlingo", "scrub")):
        return "Hospital policies"
    return "Protocols & SOPs"


def title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[_]+", " ", stem)
    if " " not in stem.strip():
        stem = stem.replace("-", " ")
    stem = re.sub(r"\s*\(\d+\)$", "", stem)        # "file (1)"
    stem = re.sub(r"\s+1$", "", stem)               # "Elbow Positioning 1"
    stem = re.sub(r"\s{2,}", " ", stem).strip(" -")
    return stem or filename


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._ ()&+-]", "_", name).strip() or "document"
    return name[:180]


def _store_file(doc_id: int, version: int, filename: str, data: bytes) -> str:
    folder = settings.files_dir / str(doc_id) / f"v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _safe_name(filename)
    path.write_bytes(data)
    return str(path.relative_to(settings.files_dir))


def _index_version(conn, doc: dict, version_id: int, blocks) -> int:
    # remove old passages for this document from the search index
    old = conn.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],)).fetchall()
    for r in old:
        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
    conn.execute("DELETE FROM chunks WHERE document_id=?", (doc["id"],))
    for seq, b in enumerate(blocks):
        cur = conn.execute(
            "INSERT INTO chunks (document_id, version_id, seq, locator, page, section, text) VALUES (?,?,?,?,?,?,?)",
            (doc["id"], version_id, seq, b.locator, b.page, b.section, b.text),
        )
        search_title = " ".join(x for x in (doc["title"], doc.get("tags") or "") if x)
        conn.execute(
            "INSERT INTO chunks_fts (rowid, title, section, text) VALUES (?,?,?,?)",
            (cur.lastrowid, search_title, b.section, b.text),
        )
    return len(blocks)


def find_duplicate(data: bytes) -> dict | None:
    return db.row(
        """SELECT d.id, d.title, v.version FROM document_versions v JOIN documents d ON d.id=v.document_id
           WHERE v.sha256=? AND v.version=d.current_version""",
        (sha256_bytes(data),),
    )


def add_document(filename: str, data: bytes, *, actor: str | None, title: str | None = None,
                 category: str | None = None, description: str = "", tags: str = "",
                 effective_date: str | None = None, downloadable: bool = True) -> dict:
    kind = file_type_for(filename)
    if not kind:
        raise ValueError(f"Unsupported file type for {filename}. Use PDF, .docx, .pptx, .txt or .md.")
    extracted = extract(filename, data)
    blocks = chunk(extracted)
    ts = db.now()
    with db.transaction() as conn:
        cur = conn.execute(
            """INSERT INTO documents (title, category, description, tags, effective_date, status, downloadable,
                                      current_version, created_at, updated_at, updated_by)
               VALUES (?,?,?,?,?,'active',?,1,?,?,?)""",
            (title or title_from_filename(filename), category or guess_category(filename), description, tags,
             effective_date or None, 1 if downloadable else 0, ts, ts, actor),
        )
        doc_id = cur.lastrowid
        stored = _store_file(doc_id, 1, filename, data)
        vcur = conn.execute(
            """INSERT INTO document_versions (document_id, version, filename, stored_path, file_type, size_bytes,
                                              sha256, page_count, char_count, note, uploaded_by, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, 1, Path(filename).name, stored, kind, len(data), sha256_bytes(data), extracted.page_count,
             extracted.char_count, "Initial upload", actor, ts),
        )
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
        n = _index_version(conn, doc, vcur.lastrowid, blocks)
        conn.execute("INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
                     (actor, "document.add", f"{doc_id}: {doc['title']} ({n} passages)", ts))
    return {"id": doc_id, "title": doc["title"], "passages": n, "warnings": extracted.warnings}


def replace_file(doc_id: int, filename: str, data: bytes, *, actor: str | None, note: str = "") -> dict:
    doc = db.row("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise KeyError("Document not found")
    kind = file_type_for(filename)
    if not kind:
        raise ValueError("Unsupported file type. Use PDF, .docx, .pptx, .txt or .md.")
    extracted = extract(filename, data)
    blocks = chunk(extracted)
    ts = db.now()
    with db.transaction() as conn:
        version = (conn.execute("SELECT MAX(version) m FROM document_versions WHERE document_id=?",
                                (doc_id,)).fetchone()["m"] or 0) + 1
        stored = _store_file(doc_id, version, filename, data)
        vcur = conn.execute(
            """INSERT INTO document_versions (document_id, version, filename, stored_path, file_type, size_bytes,
                                              sha256, page_count, char_count, note, uploaded_by, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, version, Path(filename).name, stored, kind, len(data), sha256_bytes(data),
             extracted.page_count, extracted.char_count, note or "Updated file", actor, ts),
        )
        conn.execute("UPDATE documents SET current_version=?, updated_at=?, updated_by=? WHERE id=?",
                     (version, ts, actor, doc_id))
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
        n = _index_version(conn, doc, vcur.lastrowid, blocks)
        conn.execute("INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
                     (actor, "document.replace", f"{doc_id}: v{version} {filename}", ts))
    return {"id": doc_id, "version": version, "passages": n, "warnings": extracted.warnings}


def update_metadata(doc_id: int, fields: dict, *, actor: str | None) -> None:
    allowed = {"title", "category", "description", "tags", "effective_date", "downloadable", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "status" in sets and sets["status"] not in ("active", "retired"):
        raise ValueError("status must be active or retired")
    if "downloadable" in sets:
        sets["downloadable"] = 1 if sets["downloadable"] else 0
    sets["updated_at"] = db.now()
    sets["updated_by"] = actor
    with db.transaction() as conn:
        conn.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                     (*sets.values(), doc_id))
        if "title" in sets or "tags" in sets:
            doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
            search_title = " ".join(x for x in (doc["title"], doc["tags"]) if x)
            for r in conn.execute("SELECT id, section, text FROM chunks WHERE document_id=?", (doc_id,)).fetchall():
                conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
                conn.execute("INSERT INTO chunks_fts (rowid, title, section, text) VALUES (?,?,?,?)",
                             (r["id"], search_title, r["section"], r["text"]))
        conn.execute("INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
                     (actor, "document.update", f"{doc_id}: {sorted(sets)}", db.now()))


def delete_document(doc_id: int, *, actor: str | None) -> None:
    doc = db.row("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise KeyError("Document not found")
    with db.transaction() as conn:
        for r in conn.execute("SELECT id FROM chunks WHERE document_id=?", (doc_id,)).fetchall():
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.execute("INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
                     (actor, "document.delete", f"{doc_id}: {doc['title']}", db.now()))
    shutil.rmtree(settings.files_dir / str(doc_id), ignore_errors=True)


def reindex_all(actor: str | None = "system") -> int:
    """Re-extract every current file (e.g. after changing CHUNK_CHARS)."""
    total = 0
    for v in db.rows("""SELECT v.*, d.id AS doc_id FROM document_versions v
                        JOIN documents d ON d.id=v.document_id AND v.version=d.current_version"""):
        data = (settings.files_dir / v["stored_path"]).read_bytes()
        blocks = chunk(extract(v["filename"], data))
        with db.transaction() as conn:
            doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (v["doc_id"],)).fetchone())
            total += _index_version(conn, doc, v["id"], blocks)
    return total


def current_version(doc_id: int) -> dict | None:
    return db.row("""SELECT v.* FROM document_versions v JOIN documents d ON d.id=v.document_id
                     WHERE d.id=? AND v.version=d.current_version""", (doc_id,))

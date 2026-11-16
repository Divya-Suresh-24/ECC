"""FastAPI application: web pages + JSON API for the ECC Protocol Assistant."""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, documents, llm, search
from .config import BASE_DIR, settings

STATIC = BASE_DIR / "static"

# The reverse proxy strips ROOT_PATH before requests reach the app (see deploy/nginx.conf), so routes live at "/".
# ROOT_PATH is only used to build browser-facing URLs (<base href>, redirects, cookie path).
@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    auth.bootstrap_admin()
    yield


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ middleware

@app.middleware("http")
async def security(request: Request, call_next):
    # Lightweight CSRF defence: state-changing API calls must come from our own JS (custom header).
    if request.url.path.startswith("/api/"):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("x-requested-with") != "ecc":
            return JSONResponse({"detail": "Bad request origin."}, status_code=400)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'",
    )
    if request.url.path.endswith(".html") or "/api/" in request.url.path:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _page(name: str) -> HTMLResponse:
    html = (STATIC / name).read_text()
    html = html.replace("{{BASE}}", (settings.root_path or "") + "/")
    html = html.replace("{{APP_NAME}}", settings.app_name)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def index(request: Request):
    if not auth.user_from_request(request):
        return RedirectResponse(f"{settings.root_path}/login", status_code=303)
    return _page("index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return _page("login.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    db.row("SELECT 1")
    return {"ok": True}


# ------------------------------------------------------------------ auth

class LoginIn(BaseModel):
    username: str = Field(max_length=120)
    password: str = Field(max_length=256)


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    key = f"{body.username.lower()}|{request.client.host if request.client else ''}"
    auth.throttle_check(key)
    user = db.row("SELECT * FROM users WHERE username=? AND active=1", (body.username.strip().lower(),))
    if not user or not auth.verify_password(body.password, user["pw_hash"]):
        auth.throttle_fail(key)
        raise HTTPException(401, "Incorrect username or password.")
    auth.throttle_clear(key)
    db.get_conn().execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), user["id"]))
    resp = JSONResponse({"user": auth.public_user(user)})
    auth.set_session(resp, user)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    auth.clear_session(resp)
    return resp


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    return {
        "user": auth.public_user(user),
        "app": {"name": settings.app_name, "institution": settings.institution, "mock": settings.mock_llm,
                "categories": documents.CATEGORIES, "max_question_chars": settings.max_question_chars,
                "max_upload_mb": settings.max_upload_mb},
    }


class PasswordIn(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    if not auth.verify_password(body.current, user["pw_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    problem = auth.password_problem(body.new)
    if problem:
        raise HTTPException(400, problem)
    db.get_conn().execute(
        "UPDATE users SET pw_hash=?, must_change_pw=0, session_version=session_version+1 WHERE id=?",
        (auth.hash_password(body.new), user["id"]),
    )
    fresh = db.row("SELECT * FROM users WHERE id=?", (user["id"],))
    resp = JSONResponse({"ok": True})
    auth.set_session(resp, fresh)
    return resp


# ------------------------------------------------------------------ asking questions

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    doc_ids: list[int] | None = None   # optional: restrict to specific documents


def _history(conversation_id: int) -> list[dict]:
    return db.rows(
        "SELECT question, answer_text FROM questions WHERE conversation_id=? ORDER BY id",
        (conversation_id,),
    )


@app.post("/api/ask")
def ask(body: AskIn, user: dict = Depends(auth.current_user)):
    question = body.question.strip()
    if len(question) > settings.max_question_chars:
        raise HTTPException(400, f"Please keep questions under {settings.max_question_chars} characters.")
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    used = db.row("SELECT COUNT(*) n FROM questions WHERE user_id=? AND created_at>=?", (user["id"], since))["n"]
    if used >= settings.daily_question_limit:
        raise HTTPException(429, "Daily question limit reached. Please try again tomorrow.")

    conn = db.get_conn()
    conv_id = body.conversation_id
    history: list[dict] = []
    if conv_id:
        conv = db.row("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"]))
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        history = _history(conv_id)
    else:
        ts = db.now()
        conv_id = conn.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (user["id"], question[:80], ts, ts),
        ).lastrowid

    rewritten = llm.rewrite_query(question, history)
    passages = search.search(rewritten["query"], extra_terms=[question] + rewritten["keywords"],
                             doc_ids=body.doc_ids or None)
    try:
        result = llm.answer(question, history, passages)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc))

    payload = {"segments": result["segments"], "sources": result["sources"], "found": result["found"]}
    qid = conn.execute(
        """INSERT INTO questions (conversation_id, user_id, question, search_query, answer_json, answer_text, found,
                                  model, latency_ms, input_tokens, output_tokens, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (conv_id, user["id"], question,
         json.dumps({"query": rewritten["query"], "keywords": rewritten["keywords"],
                     "passages": [p["chunk_id"] for p in passages]}),
         json.dumps(payload), result["answer_text"], 1 if result["found"] else 0, result.get("model", ""),
         result.get("latency_ms"), result.get("input_tokens"), result.get("output_tokens"), db.now()),
    ).lastrowid
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conv_id))
    return {"question_id": qid, "conversation_id": conv_id, "question": question, **payload,
            "searched": len(passages)}


@app.get("/api/conversations")
def my_conversations(user: dict = Depends(auth.current_user)):
    return db.rows(
        """SELECT c.id, c.title, c.updated_at, COUNT(q.id) AS turns FROM conversations c
           LEFT JOIN questions q ON q.conversation_id=c.id
           WHERE c.user_id=? GROUP BY c.id ORDER BY c.updated_at DESC LIMIT 50""",
        (user["id"],),
    )


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: int, user: dict = Depends(auth.current_user)):
    conv = db.row("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"]))
    if not conv:
        raise HTTPException(404, "Conversation not found.")
    turns = []
    for q in db.rows("SELECT * FROM questions WHERE conversation_id=? ORDER BY id", (conv_id,)):
        a = json.loads(q["answer_json"] or "{}")
        turns.append({"question_id": q["id"], "question": q["question"], "feedback": q["feedback"],
                      "created_at": q["created_at"], **a})
    return {"id": conv_id, "title": conv["title"], "turns": turns}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: dict = Depends(auth.current_user)):
    db.get_conn().execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"]))
    return {"ok": True}


class FeedbackIn(BaseModel):
    value: int = Field(ge=-1, le=1)
    note: str = Field(default="", max_length=2000)


@app.post("/api/questions/{qid}/feedback")
def feedback(qid: int, body: FeedbackIn, user: dict = Depends(auth.current_user)):
    q = db.row("SELECT id FROM questions WHERE id=? AND user_id=?", (qid, user["id"]))
    if not q:
        raise HTTPException(404, "Not found.")
    db.get_conn().execute("UPDATE questions SET feedback=?, feedback_note=?, reviewed=0 WHERE id=?",
                          (body.value or None, body.note.strip() or None, qid))
    return {"ok": True}


# ------------------------------------------------------------------ library (all signed-in users)

@app.get("/api/documents")
def list_documents(q: str = "", category: str = "", all: int = 0, user: dict = Depends(auth.current_user)):
    include_retired = bool(all) and user["role"] == "faculty"
    sql = """SELECT d.id, d.title, d.category, d.description, d.tags, d.effective_date, d.status, d.downloadable,
                    d.updated_at, d.updated_by, d.current_version, v.filename, v.file_type, v.size_bytes,
                    v.page_count, v.char_count,
                    (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS passages
             FROM documents d LEFT JOIN document_versions v ON v.document_id=d.id AND v.version=d.current_version
             WHERE 1=1"""
    params: list = []
    if not include_retired:
        sql += " AND d.status='active'"
    if category:
        sql += " AND d.category=?"
        params.append(category)
    docs = db.rows(sql + " ORDER BY d.category, d.title COLLATE NOCASE", params)
    if q.strip():
        hits = search.library_search(q, limit=60)
        rank = {h["doc_id"]: i for i, h in enumerate(hits)}
        snippet = {h["doc_id"]: h for h in hits}
        ql = q.lower()
        for d in docs:
            if ql in d["title"].lower() and d["id"] not in rank:
                rank[d["id"]] = -1
        docs = [d for d in docs if d["id"] in rank]
        docs.sort(key=lambda d: rank[d["id"]])
        for d in docs:
            h = snippet.get(d["id"])
            if h:
                d["match"] = {"locator": h["locator"], "text": h["text"][:260], "chunk_id": h["chunk_id"]}
    if user["role"] != "faculty":
        for d in docs:
            d.pop("updated_by", None)
    return docs


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: int, user: dict = Depends(auth.current_user)):
    doc = db.row("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc or (doc["status"] != "active" and user["role"] != "faculty"):
        raise HTTPException(404, "Document not found.")
    ver = documents.current_version(doc_id)
    passages = db.rows("SELECT id, seq, locator, page, section, text FROM chunks WHERE document_id=? ORDER BY seq",
                       (doc_id,))
    return {
        "id": doc["id"], "title": doc["title"], "category": doc["category"], "description": doc["description"],
        "effective_date": doc["effective_date"], "updated_at": doc["updated_at"], "status": doc["status"],
        "downloadable": bool(doc["downloadable"]) or user["role"] == "faculty",
        "file_type": ver["file_type"] if ver else None, "filename": ver["filename"] if ver else None,
        "version": doc["current_version"], "passages": passages,
    }


@app.get("/api/chunks/{chunk_id}")
def get_chunk(chunk_id: int, user: dict = Depends(auth.current_user)):
    c = db.row("""SELECT c.id, c.document_id AS doc_id, c.locator, c.page, c.section, c.text, d.title, d.category,
                         d.status, d.downloadable, d.effective_date, d.updated_at, v.file_type
                  FROM chunks c JOIN documents d ON d.id=c.document_id
                  JOIN document_versions v ON v.id=c.version_id WHERE c.id=?""", (chunk_id,))
    if not c:
        raise HTTPException(404, "This passage is no longer in the library (the document was updated or removed).")
    if c["status"] != "active" and user["role"] != "faculty":
        raise HTTPException(404, "This document has been retired.")
    c["downloadable"] = bool(c["downloadable"]) or user["role"] == "faculty"
    return c


def _send_file(ver: dict, inline: bool) -> FileResponse:
    path = settings.files_dir / ver["stored_path"]
    if not path.exists():
        raise HTTPException(404, "File missing on server.")
    media = {"pdf": "application/pdf",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8"}.get(ver["file_type"],
                                                                                          "application/octet-stream")
    disp = "inline" if inline and ver["file_type"] == "pdf" else "attachment"
    return FileResponse(path, media_type=media, filename=ver["filename"], content_disposition_type=disp)


@app.get("/files/{doc_id}")
def download(doc_id: int, inline: int = 0, user: dict = Depends(auth.current_user)):
    doc = db.row("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc or (doc["status"] != "active" and user["role"] != "faculty"):
        raise HTTPException(404, "Document not found.")
    if not doc["downloadable"] and user["role"] != "faculty":
        raise HTTPException(403, "Faculty have made this document view-only.")
    return _send_file(documents.current_version(doc_id), bool(inline))


@app.get("/files/{doc_id}/v/{version}")
def download_version(doc_id: int, version: int, user: dict = Depends(auth.require_faculty)):
    ver = db.row("SELECT * FROM document_versions WHERE document_id=? AND version=?", (doc_id, version))
    if not ver:
        raise HTTPException(404, "Version not found.")
    return _send_file(ver, False)


# ------------------------------------------------------------------ faculty: documents

async def _read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"{f.filename} is larger than {settings.max_upload_mb} MB.")
    if not data:
        raise HTTPException(400, f"{f.filename} is empty.")
    return data


@app.post("/api/admin/documents")
async def upload_documents(
    files: list[UploadFile] = File(...),
    category: str = Form(""),
    downloadable: bool = Form(True),
    effective_date: str = Form(""),
    title: str = Form(""),
    allow_duplicates: bool = Form(False),
    user: dict = Depends(auth.require_faculty),
):
    results = []
    for f in files:
        try:
            data = await _read_upload(f)
            dup = documents.find_duplicate(data)
            if dup and not allow_duplicates:
                results.append({"filename": f.filename, "ok": False,
                                "error": f"Identical file already in library as “{dup['title']}”."})
                continue
            res = documents.add_document(
                f.filename, data, actor=user["username"],
                title=(title.strip() if len(files) == 1 and title.strip() else None),
                category=category or None, downloadable=downloadable, effective_date=effective_date or None)
            results.append({"filename": f.filename, "ok": True, **res})
        except HTTPException as exc:
            results.append({"filename": f.filename, "ok": False, "error": exc.detail})
        except Exception as exc:
            results.append({"filename": f.filename, "ok": False, "error": str(exc)})
    return {"results": results}


@app.get("/api/admin/documents/{doc_id}")
def document_detail(doc_id: int, user: dict = Depends(auth.require_faculty)):
    doc = db.row("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise HTTPException(404, "Document not found.")
    doc["versions"] = db.rows(
        """SELECT version, filename, file_type, size_bytes, page_count, char_count, note, uploaded_by, uploaded_at
           FROM document_versions WHERE document_id=? ORDER BY version DESC""", (doc_id,))
    doc["passages"] = db.row("SELECT COUNT(*) n FROM chunks WHERE document_id=?", (doc_id,))["n"]
    doc["times_cited"] = db.row(
        "SELECT COUNT(*) n FROM questions WHERE answer_json LIKE ?", (f'%"doc_id": {doc_id},%',))["n"]
    return doc


class DocPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    tags: str | None = Field(default=None, max_length=500)
    effective_date: str | None = Field(default=None, max_length=40)
    downloadable: bool | None = None
    status: str | None = None


@app.patch("/api/admin/documents/{doc_id}")
def patch_document(doc_id: int, body: DocPatch, user: dict = Depends(auth.require_faculty)):
    if not db.row("SELECT id FROM documents WHERE id=?", (doc_id,)):
        raise HTTPException(404, "Document not found.")
    fields = body.model_dump(exclude_none=True)
    if "title" in fields and not fields["title"].strip():
        raise HTTPException(400, "Title can't be empty.")
    try:
        documents.update_metadata(doc_id, fields, actor=user["username"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return document_detail(doc_id, user)


@app.post("/api/admin/documents/{doc_id}/file")
async def replace_document_file(doc_id: int, file: UploadFile = File(...), note: str = Form(""),
                                user: dict = Depends(auth.require_faculty)):
    data = await _read_upload(file)
    try:
        res = documents.replace_file(doc_id, file.filename, data, actor=user["username"], note=note)
    except KeyError:
        raise HTTPException(404, "Document not found.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return res


@app.delete("/api/admin/documents/{doc_id}")
def remove_document(doc_id: int, user: dict = Depends(auth.require_faculty)):
    try:
        documents.delete_document(doc_id, actor=user["username"])
    except KeyError:
        raise HTTPException(404, "Document not found.")
    return {"ok": True}


@app.post("/api/admin/reindex")
def reindex(user: dict = Depends(auth.require_faculty)):
    n = documents.reindex_all(actor=user["username"])
    return {"passages": n}


# ------------------------------------------------------------------ faculty: questions & stats

@app.get("/api/admin/stats")
def stats(user: dict = Depends(auth.require_faculty)):
    week = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
    one = lambda sql, p=(): db.row(sql, p)["n"]  # noqa: E731
    top_docs = db.rows("""SELECT d.id, d.title, COUNT(*) AS n FROM questions q, json_each(q.answer_json, '$.sources') s
                          JOIN documents d ON d.id = json_extract(s.value, '$.doc_id')
                          WHERE q.created_at >= ? GROUP BY d.id ORDER BY n DESC LIMIT 8""", (week,))
    return {
        "documents": one("SELECT COUNT(*) n FROM documents WHERE status='active'"),
        "retired": one("SELECT COUNT(*) n FROM documents WHERE status='retired'"),
        "passages": one("SELECT COUNT(*) n FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.status='active'"),
        "users": one("SELECT COUNT(*) n FROM users WHERE active=1"),
        "questions_week": one("SELECT COUNT(*) n FROM questions WHERE created_at>=?", (week,)),
        "not_found_week": one("SELECT COUNT(*) n FROM questions WHERE created_at>=? AND found=0", (week,)),
        "negative_unreviewed": one("SELECT COUNT(*) n FROM questions WHERE feedback=-1 AND reviewed=0"),
        "top_documents_week": top_docs,
    }


@app.get("/api/admin/questions")
def admin_questions(filter: str = "all", q: str = "", limit: int = 100, offset: int = 0,
                    user: dict = Depends(auth.require_faculty)):
    sql = """SELECT q.id, q.question, q.answer_text, q.answer_json, q.found, q.feedback, q.feedback_note, q.reviewed,
                    q.created_at, q.model, q.latency_ms, u.username, u.display_name, u.role
             FROM questions q LEFT JOIN users u ON u.id=q.user_id WHERE 1=1"""
    params: list = []
    if filter == "notfound":
        sql += " AND q.found=0"
    elif filter == "negative":
        sql += " AND q.feedback=-1"
    elif filter == "unreviewed":
        sql += " AND q.reviewed=0 AND (q.found=0 OR q.feedback=-1)"
    if q.strip():
        sql += " AND (q.question LIKE ? OR q.answer_text LIKE ?)"
        params += [f"%{q.strip()}%"] * 2
    sql += " ORDER BY q.id DESC LIMIT ? OFFSET ?"
    params += [min(max(limit, 1), 500), max(offset, 0)]
    out = []
    for r in db.rows(sql, params):
        a = json.loads(r.pop("answer_json") or "{}")
        r["segments"] = a.get("segments", [])
        r["sources"] = a.get("sources", [])
        out.append(r)
    return out


@app.post("/api/admin/questions/{qid}/reviewed")
def mark_reviewed(qid: int, user: dict = Depends(auth.require_faculty)):
    db.get_conn().execute("UPDATE questions SET reviewed=1 WHERE id=?", (qid,))
    return {"ok": True}


@app.get("/api/admin/questions.csv")
def export_questions(user: dict = Depends(auth.require_faculty)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "username", "role", "question", "answer", "found", "feedback",
                "feedback_note", "sources"])
    for r in db.rows("""SELECT q.*, u.username, u.role FROM questions q LEFT JOIN users u ON u.id=q.user_id
                        ORDER BY q.id"""):
        srcs = json.loads(r["answer_json"] or "{}").get("sources", [])
        w.writerow([r["id"], r["created_at"], r["username"], r["role"], r["question"], r["answer_text"],
                    r["found"], r["feedback"], r["feedback_note"] or "",
                    "; ".join(f"{s['title']} ({s['locator']})" for s in srcs)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="ecc-questions.csv"'})


@app.get("/api/admin/audit")
def audit_log(user: dict = Depends(auth.require_faculty)):
    return db.rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")


# ------------------------------------------------------------------ faculty: users

class UserIn(BaseModel):
    username: str = Field(max_length=80)
    display_name: str = Field(default="", max_length=120)
    role: str = "student"
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=256)


@app.get("/api/admin/users")
def list_users(user: dict = Depends(auth.require_faculty)):
    return db.rows("""SELECT u.id, u.username, u.display_name, u.email, u.role, u.active, u.must_change_pw,
                             u.created_at, u.last_login,
                             (SELECT COUNT(*) FROM questions q WHERE q.user_id=u.id) AS questions
                      FROM users u ORDER BY u.role DESC, u.username""")


@app.post("/api/admin/users")
def add_user(body: UserIn, user: dict = Depends(auth.require_faculty)):
    if body.password and (p := auth.password_problem(body.password)):
        raise HTTPException(400, p)
    try:
        new, pw = auth.create_user(body.username, body.display_name or body.username, body.role,
                                   body.password or None, body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.audit(user["username"], "user.add", f"{new['username']} ({new['role']})")
    return {"user": auth.public_user(new), "temporary_password": None if body.password else pw}


class BulkUsersIn(BaseModel):
    lines: str = Field(max_length=50000)
    role: str = "student"


@app.post("/api/admin/users/bulk")
def bulk_users(body: BulkUsersIn, user: dict = Depends(auth.require_faculty)):
    """One user per line: `username, Display Name` (display name optional)."""
    created, errors = [], []
    for line in body.lines.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        try:
            new, pw = auth.create_user(parts[0], parts[1] if len(parts) > 1 and parts[1] else parts[0], body.role)
            created.append({"username": new["username"], "display_name": new["display_name"], "temporary_password": pw})
        except ValueError as exc:
            errors.append({"line": line, "error": str(exc)})
    db.audit(user["username"], "user.bulk_add", f"{len(created)} created")
    return {"created": created, "errors": errors}


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    role: str | None = None
    active: bool | None = None
    email: str | None = Field(default=None, max_length=200)


@app.patch("/api/admin/users/{uid}")
def patch_user(uid: int, body: UserPatch, user: dict = Depends(auth.require_faculty)):
    target = db.row("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "User not found.")
    fields = body.model_dump(exclude_none=True)
    if "role" in fields and fields["role"] not in ("student", "faculty"):
        raise HTTPException(400, "Role must be student or faculty.")
    if uid == user["id"] and (fields.get("active") is False or fields.get("role") == "student"):
        raise HTTPException(400, "You can't deactivate or demote your own account.")
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.get_conn().execute(f"UPDATE users SET {sets}, session_version=session_version+1 WHERE id=?",
                              (*fields.values(), uid))
        db.audit(user["username"], "user.update", f"{target['username']}: {sorted(fields)}")
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reset-password")
def reset_password(uid: int, user: dict = Depends(auth.require_faculty)):
    target = db.row("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "User not found.")
    pw = auth.temp_password()
    db.get_conn().execute(
        "UPDATE users SET pw_hash=?, must_change_pw=1, session_version=session_version+1 WHERE id=?",
        (auth.hash_password(pw), uid))
    db.audit(user["username"], "user.reset_password", target["username"])
    return {"temporary_password": pw}


@app.exception_handler(404)
async def not_found(request: Request, exc):
    detail = getattr(exc, "detail", "Not found")
    return JSONResponse({"detail": detail}, status_code=404)

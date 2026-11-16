"""Run with:  MOCK_LLM=1 python -m pytest -q"""
import io
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ecc-test-"))
os.environ["MOCK_LLM"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, documents, llm, search  # noqa: E402
from app.main import app  # noqa: E402

H = {"X-Requested-With": "ecc"}

PROTOCOL = b"""# Feline urethral obstruction
- Give a full mu agonist and acepromazine IM.
- Calcium gluconate 1 mL/kg of 10% solution, diluted, with ECG monitoring.

# Rabies bites
Any dog that bites a person must be reported. Quarantine is 10 days.
"""


def setup_module():
    db.init_db()
    if not db.row("SELECT id FROM users WHERE username='fac'"):
        auth.create_user("fac", "Faculty", "faculty", "Password-123", must_change=False)
        auth.create_user("stu", "Student", "student", "Password-123", must_change=False)
    if not db.row("SELECT id FROM documents"):
        documents.add_document("UO and rabies.md", PROTOCOL, actor="test")


def test_password_hashing():
    h = auth.hash_password("correct horse 1")
    assert auth.verify_password("correct horse 1", h)
    assert not auth.verify_password("wrong", h)
    assert auth.password_problem("short") and auth.password_problem("onlyletterslong") and not auth.password_problem("letters-and-1s")


def test_citation_blocks_roundtrip():
    text = "\n".join(f"Line {i} " + "x" * (i * 13 % 90) for i in range(40))
    blocks = llm.citation_blocks(text)
    assert "\n".join(blocks) == text and len(blocks) > 3


def test_search_finds_abbreviation():
    hits = search.search("blocked cat calcium dose")
    assert hits and "urethral" in hits[0]["text"].lower()
    assert search.search("UO")  # abbreviation expansion


def test_parse_response_with_sdk_like_objects():
    hits = search.search("rabies quarantine")
    p = hits[0]
    blocks = llm.citation_blocks(p["text"])
    content = [
        SimpleNamespace(type="text", text="Quarantine is 10 days", citations=[SimpleNamespace(
            type="search_result_location", source=f"chunk:{p['chunk_id']}", title=p["title"],
            cited_text=blocks[0], search_result_index=0, start_block_index=0, end_block_index=0)]),
        SimpleNamespace(type="text", text=".", citations=None),
    ]
    out = llm.parse_response(content, hits)
    assert out["found"] and out["sources"][0]["n"] == 1
    assert out["segments"][0]["cites"] == [1]
    assert out["sources"][0]["cited_texts"][0] in p["text"]


def test_not_found_flag():
    out = llm.parse_response([{"type": "text", "text": llm.NOT_FOUND_PREFIX + "."}], [])
    assert not out["found"]


def test_api_flow_and_permissions():
    with TestClient(app) as c:
        assert c.post("/api/login", json={"username": "stu", "password": "Password-123"}).status_code == 400  # CSRF header
        assert c.post("/api/login", json={"username": "stu", "password": "Password-123"}, headers=H).status_code == 200
        r = c.post("/api/ask", json={"question": "How long is rabies quarantine?"}, headers=H)
        assert r.status_code == 200 and r.json()["sources"]
        assert c.get("/api/admin/users").status_code == 403
        doc_id = c.get("/api/documents").json()[0]["id"]
        assert c.get(f"/files/{doc_id}").status_code == 200
        c.post("/api/logout", headers=H)
        assert c.post("/api/login", json={"username": "fac", "password": "Password-123"}, headers=H).status_code == 200
        assert c.patch(f"/api/admin/documents/{doc_id}", json={"downloadable": False}, headers=H).status_code == 200
        r = c.post(f"/api/admin/documents/{doc_id}/file", headers=H,
                   files={"file": ("v2.md", io.BytesIO(PROTOCOL + b"\nUpdated 2026."))})
        assert r.status_code == 200 and r.json()["version"] == 2
        c.post("/api/logout", headers=H)
        c.post("/api/login", json={"username": "stu", "password": "Password-123"}, headers=H)
        assert c.get(f"/files/{doc_id}").status_code == 403  # view-only now


def test_docx_and_pptx_extraction():
    import docx
    from pptx import Presentation
    from pptx.util import Inches

    from app.ingest import chunk, extract

    d = docx.Document()
    d.add_heading("Overnight guidelines", 1)
    d.add_paragraph("Call the backup intern if the wait exceeds 90 minutes.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Drug", "Dose"
    t.cell(1, 0).text, t.cell(1, 1).text = "Methadone", "0.2 mg/kg IV"
    buf = io.BytesIO(); d.save(buf)
    ex = extract("x.docx", buf.getvalue())
    text = " ".join(b.text for b in chunk(ex))
    assert "90 minutes" in text and "Methadone | 0.2 mg/kg IV" in text and ex.blocks[0].section == "Overnight guidelines"

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Pericardiocentesis"
    s.placeholders[1].text = "Tap on the right side"
    s.notes_slide.notes_text_frame.text = "Use a 14-16 gauge catheter"
    s.shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1)).text_frame.text = "Monitor ECG"
    buf = io.BytesIO(); prs.save(buf)
    ex = extract("x.pptx", buf.getvalue())
    assert ex.blocks[0].locator == "Slide 1" and "Speaker notes: Use a 14-16 gauge" in ex.blocks[0].text and "Monitor ECG" in ex.blocks[0].text

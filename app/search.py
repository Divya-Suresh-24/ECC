"""Passage retrieval over the document library (SQLite FTS5 / BM25).

Lexical search works well for protocol documents (drug names, doses, acronyms) and needs no
extra model or GPU on the server. Recall is improved by (1) a built-in ECC abbreviation map and
(2) Claude rewriting the question into search keywords (see llm.rewrite_query).
"""
from __future__ import annotations

import re

from . import db
from .config import settings

STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
few for from further had hadn't has hasn't have haven't having he her here hers herself him himself his how
i i'm if in into is isn't it it's its itself let's me more most mustn't my myself no nor not of off on once
only or other ought our ours ourselves out over own same she should shouldn't so some such than that the their
theirs them themselves then there these they this those through to too under until up very was wasn't we were
weren't what when where which while who whom why with won't would wouldn't you your yours yourself yourselves
please tell know want need explain show give get got also us isu ecc iowa state vet veterinary hospital
patient patients anything something someone thing things like just really
""".split())

# Expansions for common ECC shorthand → words that appear in the documents.
ABBREVIATIONS: dict[str, str] = {
    "uo": "urethral obstruction blocked",
    "blocked": "urethral obstruction",
    "gdv": "gastric dilatation volvulus bloat",
    "bloat": "gdv gastric dilatation",
    "ivdd": "intervertebral disc disease thoracolumbar",
    "tl": "thoracolumbar",
    "dka": "diabetic ketoacidosis",
    "cpr": "cpcr cardiopulmonary resuscitation arrest",
    "cpcr": "cpr cardiopulmonary resuscitation",
    "cpa": "cardiopulmonary arrest cpr",
    "chf": "congestive heart failure",
    "fate": "feline arterial thromboembolism",
    "hbc": "hit by car trauma",
    "fb": "foreign body",
    "imha": "immune mediated hemolytic anemia",
    "itp": "immune thrombocytopenia vincristine",
    "aki": "acute kidney injury",
    "tbi": "traumatic brain injury",
    "hsa": "hemangiosarcoma",
    "fast": "afast tfast ultrasound",
    "pocus": "ultrasound fast",
    "ipass": "handoff transfer",
    "cri": "constant rate infusion",
    "abx": "antibiotics antimicrobial",
    "tx": "treatment",
    "dx": "diagnosis",
    "sx": "surgery",
    "ho": "house officer",
    "rdvm": "referring veterinarian",
    "ace": "acepromazine",
    "torb": "butorphanol",
    "hydro": "hydromorphone",
    "dexdom": "dexmedetomidine",
    "ng": "nasogastric",
    "ne": "nasoesophageal",
    "ama": "against medical advice",
    "rads": "radiographs",
    "xray": "radiographs",
    "xrays": "radiographs",
    "aus": "abdominal ultrasound",
    "ppe": "personal protective equipment",
    "dnr": "do not resuscitate",
    "doa": "dead on arrival",
    "pyo": "pyometra",
    "htn": "hypertension",
    "bp": "blood pressure",
    "ecg": "electrocardiogram",
    "ekg": "ecg electrocardiogram",
    "vpc": "ventricular premature complexes",
    "vtach": "ventricular tachycardia",
    "afib": "atrial fibrillation",
    "ptx": "pneumothorax",
    "hts": "hypertonic saline",
    "ffp": "fresh frozen plasma",
    "prbc": "packed red blood cells transfusion",
    "onc": "oncology",
    "ophtho": "ophthalmology eye",
    "neuro": "neurology neurologic",
    "therio": "theriogenology reproduction",
    "icu": "intensive care",
    "er": "emergency",
    "overnight": "overnight afterhours",
    "afterhours": "after hours overnight",
}

_token = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")


def terms_for(text: str) -> list[str]:
    out: list[str] = []
    for raw in _token.findall(text.lower()):
        t = raw.strip("'-")
        if not t or t in STOPWORDS:
            continue
        if len(t) == 1 and not t.isdigit():
            continue
        out.append(t)
        if t in ABBREVIATIONS:
            out.extend(ABBREVIATIONS[t].split())
        if "-" in t:
            out.extend(p for p in t.split("-") if len(p) > 1 and p not in STOPWORDS)
    seen, uniq = set(), []
    for t in out:
        t = t.replace("'", "")
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:40]


def fts_query(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms if t)


def search(query: str, extra_terms: list[str] | None = None, k: int | None = None,
           per_doc: int = 3, doc_ids: list[int] | None = None) -> list[dict]:
    k = k or settings.top_k
    terms = terms_for(query)
    for t in extra_terms or []:
        for tt in terms_for(t):
            if tt not in terms:
                terms.append(tt)
    if not terms:
        return []
    q = fts_query(terms[:48])
    params: list = [q]
    doc_filter = ""
    if doc_ids:
        doc_filter = f" AND d.id IN ({','.join('?' * len(doc_ids))})"
        params += doc_ids
    params.append(k * 6)
    sql = f"""
        SELECT c.id AS chunk_id, c.document_id AS doc_id, c.locator, c.page, c.section, c.text, c.seq,
               d.title, d.category, d.effective_date, d.updated_at, d.downloadable,
               v.file_type, v.version,
               bm25(chunks_fts, 6.0, 2.5, 1.0) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.document_id AND d.status = 'active'
        JOIN document_versions v ON v.id = c.version_id AND v.version = d.current_version
        WHERE chunks_fts MATCH ? {doc_filter}
        ORDER BY score
        LIMIT ?
    """
    try:
        candidates = db.rows(sql, params)
    except Exception:
        return []

    # Re-rank: reward passages that contain more distinct query terms (BM25 can over-reward one rare word).
    core = [t for t in terms_for(query)]
    for c in candidates:
        low = (c["title"] + " " + c["text"]).lower()
        hits = sum(1 for t in core if t in low)
        c["coverage"] = hits / max(1, len(core))
        c["rank_score"] = -c["score"] * (0.5 + c["coverage"])

    candidates.sort(key=lambda c: c["rank_score"], reverse=True)
    picked: list[dict] = []
    counts: dict[int, int] = {}
    for c in candidates:
        if counts.get(c["doc_id"], 0) >= per_doc:
            continue
        counts[c["doc_id"]] = counts.get(c["doc_id"], 0) + 1
        picked.append(c)
        if len(picked) >= k:
            break
    return picked


def library_search(query: str, limit: int = 30) -> list[dict]:
    """Search used by the document library page: best passage per document."""
    hits = search(query, k=limit, per_doc=1)
    return hits

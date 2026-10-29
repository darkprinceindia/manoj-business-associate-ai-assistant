"""Document intake, permission-aware retrieval, and answer generation."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import vector_store as vectors

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("APP_DB", ROOT / "data" / "app.db"))
STOP = set("a an and are as at be by can do does for from how i in is it of on or our the their to was what when where which who will with you your".split())
WORD = re.compile(r"[a-z0-9][a-z0-9_-]*", re.I)
DEPARTMENTS = ("General", "Products", "Orders", "Support")
CLASSIFICATIONS = ("Public", "Admin only")
IFB_REFERENCES = (
    ("ifb-3-star-ac-warranty.md", "IFB 3-star AC warranty - CI1832D223GN1"),
    ("ifb-5-star-ac-warranty.md", "IFB 5-star AC warranty - CI1852D223GN1"),
    ("ifb-ac-super-warranty-conditions.md", "IFB AC Super Warranty - conditions"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db_path: Path | None = None, seed: bool = True) -> None:
    with connect(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
          department TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, department TEXT NOT NULL,
          classification TEXT NOT NULL, owner TEXT NOT NULL, document_type TEXT NOT NULL,
          effective_date TEXT, expiry_date TEXT, source_system TEXT NOT NULL,
          filename TEXT NOT NULL, content TEXT NOT NULL, checksum TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(status, classification, department);
        CREATE TABLE IF NOT EXISTS chunks (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          section TEXT NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL,
          vector TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text, tokenize='porter unicode61');
        CREATE TABLE IF NOT EXISTS queries (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, question TEXT NOT NULL,
          department TEXT NOT NULL, route TEXT NOT NULL, answer TEXT NOT NULL,
          no_answer INTEGER NOT NULL, confidence REAL NOT NULL, latency_ms INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS query_sources (
          query_id TEXT NOT NULL REFERENCES queries(id), chunk_id TEXT NOT NULL REFERENCES chunks(id),
          citation_number INTEGER NOT NULL, PRIMARY KEY(query_id,chunk_id)
        );
        CREATE TABLE IF NOT EXISTS feedback (
          id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES queries(id), user_id TEXT NOT NULL,
          rating INTEGER NOT NULL, issue TEXT, comment TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, event TEXT NOT NULL,
          target_id TEXT, details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS connector_calls (
          id TEXT PRIMARY KEY, query_id TEXT, connector TEXT NOT NULL, status TEXT NOT NULL,
          result_count INTEGER NOT NULL, latency_ms INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS answer_reviews (
          query_id TEXT PRIMARY KEY REFERENCES queries(id), reviewer_id TEXT NOT NULL,
          verdict TEXT NOT NULL CHECK(verdict IN ('correct','incorrect')),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_meta (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(queries)")}
        if "chat_id" not in columns:
            db.execute("ALTER TABLE queries ADD COLUMN chat_id TEXT REFERENCES chats(id)")
    if seed:
        seed_demo(db_path)
        seed_ifb_references(db_path)
    sync_vector_index(db_path)


def sync_vector_index(db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
    with connect(path) as db:
        rows = db.execute("""SELECT c.id,c.document_id,c.section,c.text,d.department,d.classification
                             FROM chunks c JOIN documents d ON d.id=c.document_id
                             WHERE d.status='active'""").fetchall()
    missing = vectors.missing_chunks([dict(row) for row in rows], path)
    vectors.index_chunks(missing, path)


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 220_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    import hmac
    salt, expected = stored.split(":", 1)
    actual = password_hash(password, bytes.fromhex(salt)).split(":", 1)[1]
    return hmac.compare_digest(actual, expected)


def store_session(token_hash: str, user_id: str, expires_at: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(datetime.now(timezone.utc).timestamp()),))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash, user_id, expires_at, utcnow()))


def session_user(token_hash: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
                         (token_hash, int(datetime.now(timezone.utc).timestamp()))).fetchone()
    return row["user_id"] if row else None


def revoke_session(token_hash: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def list_chats(user: dict) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id,title,created_at,updated_at FROM chats WHERE user_id=? ORDER BY updated_at DESC LIMIT 30",
                          (user["id"],)).fetchall()
    return [dict(row) for row in rows]


def get_chat(chat_id: str, user: dict) -> dict | None:
    with connect() as db:
        chat = db.execute("SELECT id,title,created_at,updated_at FROM chats WHERE id=? AND user_id=?",
                          (chat_id, user["id"])).fetchone()
        if not chat:
            return None
        queries = db.execute("""SELECT id,question,answer,department,route,no_answer,created_at
                                FROM queries WHERE chat_id=? ORDER BY created_at,id""", (chat_id,)).fetchall()
        messages = []
        for query in queries:
            citations = db.execute("""SELECT qs.citation_number number,d.id document_id,d.title,c.section
                                      FROM query_sources qs JOIN chunks c ON c.id=qs.chunk_id
                                      JOIN documents d ON d.id=c.document_id
                                      WHERE qs.query_id=? ORDER BY qs.citation_number""", (query["id"],)).fetchall()
            messages.append({**dict(query), "citations": [dict(row) for row in citations]})
    return {"chat": dict(chat), "messages": messages}


def seed_demo(db_path: Path | None = None) -> None:
    admin_password = os.environ.get("ADMIN_PASSWORD") or os.environ.get("DEMO_PASSWORD", "Almax!23")
    customer_password = os.environ.get("CUSTOMER_PASSWORD", "Customer123!")
    users = [
        ("admin", "Shop Admin", "admin@demo.local", "General", "admin", admin_password),
        ("customer", "Customer", "customer@demo.local", "General", "customer", customer_password),
    ]
    with connect(db_path) as db:
        if not db.execute("SELECT 1 FROM app_meta WHERE key='shop_cutover_at'").fetchone():
            db.execute("INSERT INTO app_meta VALUES ('shop_cutover_at',?)", (utcnow(),))
        for user_id, name, email, department, role, password in users:
            hashed = password_hash(password)
            db.execute("""INSERT INTO users VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,email=excluded.email,department=excluded.department,
                       role=excluded.role,password_hash=excluded.password_hash""",
                       (user_id, name, email, department, role, hashed))
        db.execute("DELETE FROM users WHERE id IN ('delivery','hr','engineering','sales')")
        # Clear the former demo corpus once. Later Admin uploads remain active on restart.
        if not db.execute("SELECT 1 FROM app_meta WHERE key='knowledge_reset_v1'").fetchone():
            for row in db.execute("SELECT id FROM documents").fetchall():
                vectors.remove_document(row["id"], db_path or DB_PATH)
            db.execute("UPDATE documents SET status='archived' WHERE status='active'")
            db.execute("INSERT INTO app_meta VALUES ('knowledge_reset_v1',?)", (utcnow(),))


def seed_ifb_references(db_path: Path | None = None) -> None:
    """Index the shipped manufacturer references once, preserving later Admin changes."""
    path = db_path or DB_PATH
    with connect(path) as db:
        if db.execute("SELECT 1 FROM app_meta WHERE key='ifb_reference_seed_v1'").fetchone():
            return
    for filename, title in IFB_REFERENCES:
        content = (ROOT / "data" / "ifb_reference" / filename).read_text(encoding="utf-8")
        checksum = hashlib.sha256(content.strip().encode()).hexdigest()
        with connect(path) as db:
            exists = db.execute("SELECT 1 FROM documents WHERE checksum=? AND status='active'", (checksum,)).fetchone()
        if not exists:
            create_document({"title": title, "department": "Support", "classification": "Public",
                             "owner": "IFB Industries Ltd", "document_type": "Manufacturer reference",
                             "filename": filename, "content": content,
                             "source_system": "IFB official manufacturer pages"}, "admin", path)
    with connect(path) as db:
        db.execute("INSERT OR IGNORE INTO app_meta VALUES ('ifb_reference_seed_v1',?)", (utcnow(),))


def tokenize(text: str) -> list[str]:
    return [w.lower() for w in WORD.findall(text) if w.lower() not in STOP and len(w) > 1]


def vectorize(text: str) -> dict[str, float]:
    """Legacy lexical signature kept for the existing SQL schema; search uses Qdrant."""
    terms = tokenize(text)
    counts = Counter(terms + [a + "_" + b for a, b in zip(terms, terms[1:])])
    weights: dict[str, float] = {}
    for term, count in counts.items():
        key = str(int.from_bytes(hashlib.blake2b(term.encode(), digest_size=4).digest(), "big") % 256)
        weights[key] = weights.get(key, 0.0) + (1 + math.log(count))
    norm = math.sqrt(sum(v * v for v in weights.values())) or 1
    return {k: round(v / norm, 6) for k, v in weights.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    return sum(v * b.get(k, 0) for k, v in a.items())


def split_sections(content: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = "Overview"
    buffer: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^#{1,4}\s+(.+)$", line.strip())
        if match:
            if " ".join(buffer).strip():
                sections.append((heading, " ".join(buffer).strip()))
            heading, buffer = match.group(1).strip(), []
        elif line.strip():
            buffer.append(line.strip())
    if " ".join(buffer).strip():
        sections.append((heading, " ".join(buffer).strip()))
    if not sections and content.strip():
        sections = [("Content", content.strip())]
    chunks: list[tuple[str, str]] = []
    for section, body in sections:
        words = body.split()
        for start in range(0, len(words), 180):
            chunks.append((section, " ".join(words[start:start + 220])))
            if start + 220 >= len(words):
                break
    return chunks


def create_document(data: dict, actor_id: str, db_path: Path | None = None) -> dict:
    required = ("title", "department", "classification", "owner", "document_type", "filename", "content")
    if any(not str(data.get(k, "")).strip() for k in required):
        raise ValueError("Title, department, classification, owner, type, filename and readable content are required.")
    if data["department"] not in DEPARTMENTS or data["classification"] not in CLASSIFICATIONS:
        raise ValueError("Invalid department or classification.")
    content = str(data["content"]).strip()
    if len(content) < 60:
        raise ValueError("Document contains too little readable text.")
    checksum = hashlib.sha256(content.encode()).hexdigest()
    chunks = split_sections(content)
    document_id = uuid.uuid4().hex
    path = db_path or DB_PATH
    with connect(path) as db:
        if db.execute("SELECT 1 FROM documents WHERE checksum=? AND status='active'", (checksum,)).fetchone():
            raise ValueError("This document is already indexed.")
        db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (document_id, data["title"].strip(), data["department"], data["classification"],
                    data["owner"].strip(), data["document_type"].strip(), data.get("effective_date") or None,
                    data.get("expiry_date") or None, data.get("source_system") or "Manual upload",
                    data["filename"], content, checksum, "active", utcnow()))
        vector_chunks = []
        for position, (section, body) in enumerate(chunks):
            chunk_id = uuid.uuid4().hex
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                       (chunk_id, document_id, section, position, body, json.dumps(vectorize(section + " " + body))))
            db.execute("INSERT INTO chunks_fts(chunk_id,text) VALUES (?,?)", (chunk_id, section + " " + body))
            vector_chunks.append({"id": chunk_id, "document_id": document_id, "section": section,
                                  "text": body, "department": data["department"],
                                  "classification": data["classification"]})
        vectors.index_chunks(vector_chunks, path)
        audit(db, actor_id, "document_indexed", document_id, {"chunks": len(chunks)})
    return {"id": document_id, "title": data["title"], "chunks": len(chunks), "status": "active"}


def extract_document(filename: str, raw: bytes) -> str:
    extension = Path(filename).suffix.lower()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Maximum file size is 5 MB.")
    if extension in (".txt", ".md"):
        return raw.decode("utf-8-sig", errors="replace")
    if extension == ".docx":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        return "\n".join("".join(node.itertext()) for node in root.iter(namespace + "p"))
    if extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF support requires: pip install -r requirements.txt") from exc
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
    raise ValueError("Supported file types are .txt, .md, .docx and .pdf.")


def can_access(user: dict | sqlite3.Row, row: dict | sqlite3.Row) -> bool:
    if user["role"] == "admin":
        return True
    return user["role"] == "customer" and row["classification"] == "Public"


def list_documents(user: dict | sqlite3.Row, db_path: Path | None = None) -> list[dict]:
    with connect(db_path) as db:
        rows = db.execute("SELECT id,title,department,classification,owner,document_type,effective_date,expiry_date,source_system,filename,status,created_at FROM documents WHERE status='active' ORDER BY created_at DESC,title").fetchall()
    return [dict(row) for row in rows if can_access(user, row)]


def get_document(document_id: str, user: dict | sqlite3.Row, db_path: Path | None = None) -> dict | None:
    with connect(db_path) as db:
        row = db.execute("SELECT * FROM documents WHERE id=? AND status='active'", (document_id,)).fetchone()
    if not row or not can_access(user, row):
        return None
    return dict(row)


def archive_document(document_id: str, actor_id: str, db_path: Path | None = None) -> bool:
    with connect(db_path) as db:
        changed = db.execute("UPDATE documents SET status='archived' WHERE id=? AND status='active'", (document_id,)).rowcount
        if changed:
            vectors.remove_document(document_id, db_path or DB_PATH)
            audit(db, actor_id, "document_archived", document_id, {})
    return bool(changed)


def vector_candidates(question: str, user: dict | sqlite3.Row, department: str | None = None,
                      db_path: Path | None = None) -> list[dict]:
    return vectors.candidates(question, user["role"], department, db_path or DB_PATH)


def search(question: str, user: dict | sqlite3.Row, department: str | None = None,
           db_path: Path | None = None, limit: int = 10,
           candidates: list[dict] | None = None) -> list[dict]:
    terms = tokenize(question)
    if not terms:
        return []
    hits = candidates if candidates is not None else vector_candidates(question, user, department, db_path)
    if not hits:
        return []
    scores = {hit["id"]: hit["score"] for hit in hits}
    with connect(db_path) as db:
        placeholders = ",".join("?" for _ in scores)
        rows = db.execute(f"""SELECT c.id,c.document_id,c.section,c.position,c.text,
          d.title,d.department,d.classification,d.owner,d.effective_date,d.expiry_date,d.status
          FROM chunks c JOIN documents d ON d.id=c.document_id
          WHERE d.status='active' AND c.id IN ({placeholders})""", tuple(scores)).fetchall()
        expression = " OR ".join('"' + term.replace('"', '') + '"' for term in set(terms))
        fts_rows = db.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT 100",
                              (expression,)).fetchall()
        fts_ranks = {row["chunk_id"]: rank for rank, row in enumerate(fts_rows)}
    today = datetime.now(timezone.utc).date().isoformat()
    ranked = []
    for row in rows:
        if not can_access(user, row) or (department and department != "All" and row["department"] != department):
            continue
        if row["expiry_date"] and row["expiry_date"] < today:
            continue
        searchable = row["section"] + " " + row["title"] + " " + row["text"]
        words = Counter(tokenize(searchable))
        overlap = sum(min(words[t], 3) for t in set(terms)) / max(len(set(terms)), 1)
        semantic = max(0.0, scores[row["id"]])
        keyword = 1 / (1 + fts_ranks[row["id"]]) if row["id"] in fts_ranks else 0
        score = round(0.70 * semantic + 0.20 * min(overlap, 1) + 0.10 * keyword, 4)
        if score >= 0.45:
            item = dict(row)
            item["score"] = score
            item["semantic_score"] = round(semantic, 4)
            ranked.append(item)
    ranked.sort(key=lambda x: (x["score"], x["effective_date"] or ""), reverse=True)
    return ranked[:limit]


def classify_query(question: str, requested: str | None = None) -> str:
    if requested and requested != "All":
        return requested
    q = set(tokenize(question))
    hints = {
        "Products": {"product", "products", "electronics", "phone", "laptop", "television", "tv", "appliance", "accessory"},
        "Orders": {"order", "orders", "purchase", "payment", "invoice", "delivery", "shipping"},
        "Support": {"warranty", "repair", "return", "refund", "service", "help", "support"},
    }
    scores = {department: len(q & words) for department, words in hints.items()}
    winner = max(scores, key=scores.get)
    return winner if scores[winner] else "All"


def extractive_answer(question: str, sources: list[dict]) -> str:
    q = set(tokenize(question))
    pieces = []
    for index, source in enumerate(sources[:3], 1):
        sentences = re.split(r"(?<=[.!?])\s+", source["text"])
        ranked = sorted(enumerate(sentences), key=lambda item: len(q & set(tokenize(item[1]))), reverse=True)
        chosen = sorted(i for i, sentence in ranked[:(2 if index == 1 else 1)] if sentence.strip())
        for sentence_index in chosen:
            pieces.append(sentences[sentence_index].strip().rstrip(".") + f". [{index}]")
    return " ".join(pieces) if pieces else "I could not find enough evidence in the approved sources to answer this question."


def gemini_answer(question: str, sources: list[dict]) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    context = "\n\n".join(f"[{i}] {s['title']} / {s['section']} (effective {s['effective_date'] or 'unknown'}): {s['text']}"
                           for i, s in enumerate(sources, 1))
    template = (ROOT / "prompts" / "grounded_answer.txt").read_text(encoding="utf-8")
    prompt = template.format(question=question, context=context)
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600}}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.load(response)
        answer = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        citations = [int(x) for x in re.findall(r"\[(\d+)\]", answer)]
        if citations and all(1 <= n <= len(sources) for n in citations):
            return answer
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError):
        pass
    return None


def audit(db: sqlite3.Connection, actor_id: str, event: str, target_id: str | None, details: dict) -> None:
    db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)",
               (uuid.uuid4().hex, actor_id, event, target_id, json.dumps(details), utcnow()))


def review_answer(query_id: str, reviewer: dict, verdict: str, db_path: Path | None = None) -> None:
    if reviewer["role"] != "admin":
        raise PermissionError("Administrator access required.")
    if verdict not in ("correct", "incorrect"):
        raise ValueError("Review must be correct or incorrect.")
    with connect(db_path) as db:
        cutover = db.execute("SELECT value FROM app_meta WHERE key='shop_cutover_at'").fetchone()[0]
        query = db.execute("SELECT no_answer FROM queries WHERE id=? AND created_at>=?", (query_id, cutover)).fetchone()
        if not query or query["no_answer"]:
            raise ValueError("Choose an answered question to review.")
        db.execute("""INSERT INTO answer_reviews VALUES (?,?,?,?)
                   ON CONFLICT(query_id) DO UPDATE SET reviewer_id=excluded.reviewer_id,
                   verdict=excluded.verdict,created_at=excluded.created_at""",
                   (query_id, reviewer["id"], verdict, utcnow()))
        audit(db, reviewer["id"], "answer_reviewed", query_id, {"verdict": verdict})


def analytics(user: dict, db_path: Path | None = None) -> dict:
    if user["role"] != "admin":
        raise PermissionError("Administrator access required.")
    with connect(db_path) as db:
        cutover = db.execute("SELECT value FROM app_meta WHERE key='shop_cutover_at'").fetchone()[0]
        stats = db.execute("""SELECT COUNT(*) queries,COALESCE(AVG(latency_ms),0) avg_latency_ms,
          COALESCE(SUM(no_answer),0) no_answers FROM queries WHERE created_at>=?""", (cutover,)).fetchone()
        docs = db.execute("SELECT department,COUNT(*) count FROM documents WHERE status='active' GROUP BY department").fetchall()
        routes = db.execute("SELECT route,COUNT(*) count FROM queries WHERE created_at>=? GROUP BY route", (cutover,)).fetchall()
        recent = db.execute("""SELECT q.id,q.question,q.department,q.route,q.answer,q.no_answer,q.created_at,
          r.verdict, (SELECT GROUP_CONCAT(DISTINCT d.title) FROM query_sources qs
          JOIN chunks c ON c.id=qs.chunk_id JOIN documents d ON d.id=c.document_id
          WHERE qs.query_id=q.id) sources
          FROM queries q LEFT JOIN answer_reviews r ON r.query_id=q.id
          WHERE q.created_at>=? ORDER BY q.created_at DESC LIMIT 12""", (cutover,)).fetchall()
        feedback = db.execute("""SELECT COUNT(*) count,
          COALESCE(SUM(CASE WHEN rating>=4 THEN 1 ELSE 0 END),0) positive
          FROM feedback f JOIN queries q ON q.id=f.query_id WHERE q.created_at>=?
          AND f.rowid=(SELECT MAX(f2.rowid) FROM feedback f2
          WHERE f2.query_id=f.query_id AND f2.user_id=f.user_id)""", (cutover,)).fetchone()
        reviews = db.execute("""SELECT COUNT(*) reviewed,
          COALESCE(SUM(CASE WHEN verdict='correct' THEN 1 ELSE 0 END),0) correct
          FROM answer_reviews r JOIN queries q ON q.id=r.query_id WHERE q.created_at>=?""", (cutover,)).fetchone()
        connector = db.execute("SELECT connector,status,result_count,latency_ms,created_at FROM connector_calls WHERE created_at>=? ORDER BY created_at DESC LIMIT 10", (cutover,)).fetchall()
    return {"queries": stats["queries"], "answered": stats["queries"] - stats["no_answers"],
            "avg_latency_ms": round(stats["avg_latency_ms"]),
            "no_answer_rate": round(stats["no_answers"] / stats["queries"] * 100, 1) if stats["queries"] else 0,
            "citation_coverage": round((stats["queries"] - stats["no_answers"]) / stats["queries"] * 100, 1) if stats["queries"] else 0,
            "documents_by_department": [dict(x) for x in docs], "routes": [dict(x) for x in routes],
            "recent_queries": [dict(x) for x in recent], "feedback_count": feedback["count"],
            "positive_feedback_pct": round(feedback["positive"] / feedback["count"] * 100, 1) if feedback["count"] else None,
            "reviewed_answers": reviews["reviewed"],
            "reviewed_accuracy_pct": round(reviews["correct"] / reviews["reviewed"] * 100, 1) if reviews["reviewed"] else None,
            "connector_calls": [dict(x) for x in connector]}

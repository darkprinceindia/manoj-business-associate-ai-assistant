"""HTTP application for the Manoj Business Associate AI assistant."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()
import knowledge as k
SECRET = os.environ.get("APP_SECRET") or secrets.token_urlsafe(32)


def sign_session(user_id: str) -> str:
    payload = {"uid": user_id, "exp": int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    token = encoded + "." + signature
    k.store_session(hashlib.sha256(token.encode()).hexdigest(), user_id, payload["exp"])
    return token


def read_session(token: str) -> str | None:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["exp"] <= time.time():
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return payload["uid"] if k.session_user(token_hash) == payload["uid"] else None
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        return None


def mcp_search(question: str, user: dict, department: str) -> tuple[list[dict], str | None, int]:
    """Call the bundled read-only MCP server over stdio JSON-RPC."""
    start = time.monotonic()
    try:
        # The parent owns the embedded Qdrant client; its local files cannot be
        # opened by a second process. The MCP tool receives candidate IDs and
        # reranks them against SQL after checking the user's permissions.
        candidates = k.vector_candidates(question, user, department)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "manoj-business-assistant", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_curated_knowledge", "arguments": {"question": question, "user_id": user["id"], "department": department, "candidates": candidates}}},
        ]
        process = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")],
                                 input="\n".join(json.dumps(x) for x in requests) + "\n",
                                 text=True, capture_output=True, timeout=10, cwd=ROOT)
        if process.returncode:
            raise RuntimeError(process.stderr[:200])
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        output = next(x for x in responses if x.get("id") == 2)
        if "error" in output:
            raise RuntimeError(output["error"]["message"])
        matches = json.loads(output["result"]["content"][0]["text"])["matches"]
        # Enforce authorization again after crossing the connector boundary.
        matches = [s for s in matches if k.can_access(user, s)]
        return matches, None, int((time.monotonic() - start) * 1000)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired, RuntimeError) as exc:
        return [], str(exc)[:200], int((time.monotonic() - start) * 1000)


def answer_query(question: str, user: dict, requested_department: str = "All",
                 chat_id: str | None = None) -> dict:
    if not question.strip() or len(question) > 2000:
        raise ValueError("Enter a question up to 2,000 characters.")
    if requested_department not in (*k.DEPARTMENTS, "All"):
        raise ValueError("Invalid department filter.")
    if chat_id and not k.get_chat(chat_id, user):
        raise PermissionError("Chat not found or not yours.")
    start = time.monotonic()
    department = k.classify_query(question, requested_department)
    route = "MCP product knowledge" if department == "Products" else "Local hybrid index"
    connector_error = None
    connector_ms = 0
    if route.startswith("MCP"):
        sources, connector_error, connector_ms = mcp_search(question, user, department)
        if connector_error:
            sources = k.search(question, user, department)
            route = "Local fallback after MCP error"
    else:
        sources = k.search(question, user, department if department != "All" else None)
    if not sources and department != "All" and requested_department == "All":
        sources = k.search(question, user)
    requested_models = set(re.findall(r"\b(?:CI|HI)[A-Z0-9]{8,}\b", question.upper()))
    if requested_models:
        def matches_requested_model(source):
            source_models = set(re.findall(r"\b(?:CI|HI)[A-Z0-9]{8,}\b",
                                           (source["title"] + " " + source["text"]).upper()))
            return not source_models or bool(source_models & requested_models)
        sources = [source for source in sources if matches_requested_model(source)]
    if sources:
        terms = set(k.tokenize(question))
        def evidence_overlap(source):
            sentences = re.split(r"(?<=[.!?])\s+", source["text"])
            return max((len(terms & set(k.tokenize(sentence))) for sentence in sentences), default=0)
        strongest_overlap = evidence_overlap(sources[0])
        minimum_overlap = 2 if strongest_overlap >= 2 and len(terms) >= 3 else 1
        sources = [source for source in sources
                   if evidence_overlap(source) >= minimum_overlap
                   and len(terms & set(k.tokenize(source["section"] + " " + source["text"]))) / len(terms) >= 0.34][:3]
    no_answer = not sources or sources[0]["score"] < 0.19
    if no_answer:
        sources = []
        answer = "I could not find enough information in the shop documents to answer that. Please contact the shop for confirmation."
        confidence = 0.0
        synthesis = "No answer"
    else:
        model_answer = k.gemini_answer(question, sources)
        answer = model_answer or k.extractive_answer(question, sources)
        synthesis = "Gemini" if model_answer else "Local extractive"
        confidence = min(0.99, round(sources[0]["score"], 2))
    query_id = uuid.uuid4().hex
    latency_ms = int((time.monotonic() - start) * 1000)
    with k.connect() as db:
        now = k.utcnow()
        if not chat_id:
            chat_id = uuid.uuid4().hex
            db.execute("INSERT INTO chats VALUES (?,?,?,?,?)",
                       (chat_id, user["id"], question.strip()[:72], now, now))
        else:
            db.execute("UPDATE chats SET updated_at=? WHERE id=? AND user_id=?", (now, chat_id, user["id"]))
        db.execute("""INSERT INTO queries
                   (id,user_id,question,department,route,answer,no_answer,confidence,latency_ms,created_at,chat_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (query_id, user["id"], question.strip(), department, route, answer,
                    int(no_answer), confidence, latency_ms, now, chat_id))
        for number, source in enumerate(sources, 1):
            db.execute("INSERT INTO query_sources VALUES (?,?,?)", (query_id, source["id"], number))
        if route.startswith("MCP") or connector_error:
            db.execute("INSERT INTO connector_calls VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, query_id, "product-knowledge", "error" if connector_error else "success",
                        len(sources), connector_ms, connector_error, k.utcnow()))
        k.audit(db, user["id"], "query", query_id, {"route": route, "sources": len(sources), "no_answer": no_answer})
    citations = [{"number": i, "document_id": s["document_id"], "title": s["title"],
                  "section": s["section"], "snippet": s["text"], "effective_date": s["effective_date"],
                  "owner": s["owner"], "classification": s["classification"], "score": s["score"]}
                 for i, s in enumerate(sources, 1)]
    return {"id": query_id, "chat_id": chat_id, "answer": answer, "citations": citations, "route": route,
            "department": department, "confidence": confidence, "no_answer": no_answer,
            "latency_ms": latency_ms, "model": synthesis}


class Handler(BaseHTTPRequestHandler):
    server_version = "ManojBusinessAssistant/1.0"

    def send_json(self, value: object, status: int = 200, cookie: str | None = None) -> None:
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 8 * 1024 * 1024:
            raise ValueError("Request too large.")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object expected.")
        return value

    def user(self) -> dict | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["session"].value
        except Exception:
            return None
        user_id = read_session(token)
        if not user_id:
            return None
        with k.connect() as db:
            row = db.execute("SELECT id,name,email,department,role FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def require_user(self) -> dict:
        user = self.user()
        if not user:
            raise PermissionError("Sign in to continue.")
        return user

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/me":
                return self.send_json({"user": self.user()})
            if path == "/api/chats":
                return self.send_json({"chats": k.list_chats(self.require_user())})
            if path.startswith("/api/chats/"):
                chat = k.get_chat(path.rsplit("/", 1)[-1], self.require_user())
                return self.send_json(chat or {"error": "Chat not found"}, 200 if chat else 404)
            if path == "/api/documents":
                return self.send_json({"documents": k.list_documents(self.require_user())})
            if path.startswith("/api/documents/"):
                doc = k.get_document(path.rsplit("/", 1)[-1], self.require_user())
                return self.send_json({"document": doc}, 200 if doc else 404)
            if path == "/api/analytics":
                return self.send_json(k.analytics(self.require_user()))
            if path == "/api/connectors":
                self.require_user()
                return self.send_json({"connectors": [{"name": "product-knowledge", "protocol": "MCP stdio JSON-RPC",
                    "status": "ready", "tools": ["search_curated_knowledge"], "scope": "permission-filtered curated index"}]})
            if path == "/" or path.startswith("/static/"):
                filename = "index.html" if path == "/" else path[len("/static/"):]
                target = (STATIC / filename).resolve()
                if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                    return self.send_json({"error": "Not found"}, 404)
                raw = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(raw)
            return self.send_json({"error": "Not found"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.send_json({"error": "Cross-origin request denied."}, 403)
            data = self.body()
            if path == "/api/login":
                if not isinstance(data.get("password"), str):
                    raise ValueError("Password is required.")
                with k.connect() as db:
                    row = db.execute("SELECT * FROM users WHERE email=?", (data.get("email", ""),)).fetchone()
                if not row or not k.verify_password(data.get("password", ""), row["password_hash"]):
                    return self.send_json({"error": "Invalid email or password."}, 401)
                token = sign_session(row["id"])
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200{secure}"
                return self.send_json({"user": {field: row[field] for field in ("id", "name", "email", "department", "role")}}, cookie=cookie)
            if path == "/api/logout":
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                    k.revoke_session(hashlib.sha256(cookie["session"].value.encode()).hexdigest())
                except KeyError:
                    pass
                return self.send_json({"ok": True}, cookie="session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            user = self.require_user()
            if path == "/api/query":
                return self.send_json(answer_query(data.get("question", ""), user,
                                                   data.get("department", "All"), data.get("chat_id")))
            if path == "/api/analytics/review":
                k.review_answer(data.get("query_id", ""), user, data.get("verdict", ""))
                return self.send_json({"saved": True})
            if path == "/api/documents/upload":
                if user["role"] != "admin":
                    raise PermissionError("Administrator access required to upload documents.")
                raw = base64.b64decode(data.get("file_base64", ""), validate=True)
                payload = {key: data.get(key) for key in ("title", "department", "classification", "owner", "document_type", "effective_date", "expiry_date", "source_system", "filename")}
                payload["content"] = k.extract_document(payload.get("filename") or "", raw)
                return self.send_json({"document": k.create_document(payload, user["id"])}, 201)
            if path == "/api/documents/archive":
                if user["role"] != "admin":
                    raise PermissionError("Administrator access required to archive documents.")
                return self.send_json({"archived": k.archive_document(data.get("document_id", ""), user["id"])})
            if path == "/api/feedback":
                rating = int(data.get("rating", 0))
                if rating not in (1, 2, 3, 4, 5):
                    raise ValueError("Rating must be 1 to 5.")
                with k.connect() as db:
                    query = db.execute("SELECT user_id FROM queries WHERE id=?", (data.get("query_id"),)).fetchone()
                    if not query or (query["user_id"] != user["id"] and user["role"] != "admin"):
                        raise PermissionError("Query not found or not yours.")
                    db.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex, data["query_id"], user["id"], rating,
                                data.get("issue"), str(data.get("comment", ""))[:500], k.utcnow()))
                    k.audit(db, user["id"], "feedback", data["query_id"], {"rating": rating})
                return self.send_json({"saved": True}, 201)
            return self.send_json({"error": "Not found"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, 403)
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not all(os.environ.get(name) for name in ("APP_SECRET", "ADMIN_PASSWORD", "CUSTOMER_PASSWORD")):
            raise SystemExit("Set APP_SECRET, ADMIN_PASSWORD, and CUSTOMER_PASSWORD before binding to a public interface.")
    k.init_db()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Manoj Business Associate AI assistant is running at http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

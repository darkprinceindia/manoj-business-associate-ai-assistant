"""Exercise login, upload, query, preview, feedback, analytics and access over HTTP."""

import base64
import http.cookiejar
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with tempfile.TemporaryDirectory() as directory:
    os.environ["APP_DB"] = str(Path(directory) / "http.db")
    os.environ["ADMIN_PASSWORD"] = "test-admin-password"
    os.environ["CUSTOMER_PASSWORD"] = "test-customer-password"
    os.environ["GEMINI_API_KEY"] = ""
    import knowledge as k
    import app

    k.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def client():
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(opener, route, payload=None):
        request = urllib.request.Request(base + route,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"} if payload is not None else {})
        with opener.open(request) as response:
            return json.load(response)

    try:
        admin = client()
        try:
            call(admin, "/api/login", {"email": "admin@demo.local", "password": "test-customer-password"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Customer password unlocked the admin account")
        call(admin, "/api/login", {"email": "admin@demo.local", "password": "test-admin-password"})
        assert len(call(admin, "/api/documents")["documents"]) == 3
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 50 700 Td (Admin PDF upload confirms readable policy text is extracted and indexed for later customer questions.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        pdf = io.BytesIO(); writer.write(pdf)
        pdf_doc = call(admin, "/api/documents/upload", {"title": "PDF policy upload test", "department": "General",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Policy",
            "filename": "policy.pdf", "file_base64": base64.b64encode(pdf.getvalue()).decode()})["document"]
        assert pdf_doc["chunks"] >= 1
        docx = io.BytesIO()
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Admin DOCX upload confirms a readable product guide is extracted and indexed for later customer questions.</w:t></w:r></w:p></w:body></w:document>')
        docx_doc = call(admin, "/api/documents/upload", {"title": "DOCX guide upload test", "department": "General",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Guide",
            "filename": "guide.docx", "file_base64": base64.b64encode(docx.getvalue()).decode()})["document"]
        assert docx_doc["chunks"] >= 1
        text = b"# Sample product guide\n## Desk lamp\nThe sample desk lamp has three brightness settings and a USB-C power connection. Customers should ask the shop to confirm the current price and availability."
        uploaded = call(admin, "/api/documents/upload", {"title": "Sample product guide", "department": "Products",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Guide",
            "filename": "sample-product.md", "file_base64": base64.b64encode(text).decode()})["document"]
        assert uploaded["chunks"] >= 1
        answer = call(admin, "/api/query", {"question": "How many brightness settings does the sample product desk lamp have?"})
        assert not answer["no_answer"] and any(x["title"] == "Sample product guide" for x in answer["citations"])
        assert "MCP" in answer["route"]
        assert any(chat["id"] == answer["chat_id"] for chat in call(admin, "/api/chats")["chats"])
        assert len(call(admin, f"/api/chats/{answer['chat_id']}")["messages"]) == 1
        followup = call(admin, "/api/query", {"question": "Does the sample desk lamp use USB-C?",
            "chat_id": answer["chat_id"]})
        assert followup["chat_id"] == answer["chat_id"]
        assert len(call(admin, f"/api/chats/{answer['chat_id']}")["messages"]) == 2
        assert call(admin, f"/api/documents/{uploaded['id']}")["document"]["content"]
        assert call(admin, "/api/feedback", {"query_id": answer["id"], "rating": 5})["saved"]
        assert call(admin, "/api/analytics")["feedback_count"] == 1
        assert call(admin, "/api/analytics/review", {"query_id": answer["id"], "verdict": "correct"})["saved"]
        assert call(admin, "/api/analytics")["reviewed_accuracy_pct"] == 100
        private_text = b"# Private stock plan\nShop admin planning details stay private. These stock notes and supplier arrangements must not be shown to customer accounts or used in customer answers."
        private = call(admin, "/api/documents/upload", {"title": "Private stock plan", "department": "Products",
            "classification": "Admin only", "owner": "Shop Admin", "document_type": "Note",
            "filename": "private-stock.md", "file_base64": base64.b64encode(private_text).decode()})["document"]
        customer = client()
        call(customer, "/api/login", {"email": "customer@demo.local", "password": "test-customer-password"})
        assert all(doc["id"] != private["id"] for doc in call(customer, "/api/documents")["documents"])
        try:
            call(customer, f"/api/documents/{private['id']}")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Admin-only document was visible")
        try:
            call(customer, f"/api/chats/{answer['chat_id']}")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Customer could read Admin chat history")
        for route, payload in (("/api/analytics", None), ("/api/analytics/review", {"query_id": answer["id"], "verdict": "incorrect"}),
                               ("/api/documents/upload", {"title": "Not allowed"})):
            try:
                call(customer, route, payload)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            else:
                raise AssertionError(f"Customer reached admin API: {route}")
        assert call(admin, "/api/logout", {})["ok"]
        try:
            call(admin, "/api/documents")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Revoked Admin session remained valid")
        print("HTTP smoke test passed: roles, vector retrieval, MCP, SQL sessions and chats, feedback, review, access denial")
    finally:
        server.shutdown()
        server.server_close()
        k.vectors.close_all()

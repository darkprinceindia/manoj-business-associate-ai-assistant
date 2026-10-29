"""End-to-end service checks using an isolated SQLite database."""

import os
import io
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

TEMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = str(Path(TEMP.name) / "test.db")
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["CUSTOMER_PASSWORD"] = "test-customer-password"
os.environ["GEMINI_API_KEY"] = ""

import knowledge as k  # noqa: E402
import app  # noqa: E402

BASE_TEXT = ("# About Manoj Business Associates\n## The business\n"
             "Manoj Business Associates is a shop that deals in electronics items. "
             "Customers can ask this assistant about information the shop adds to its knowledge bank.")


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        k.init_db()
        with k.connect() as db:
            cls.initial_document_count = db.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0]
        k.create_document({"title": "About Manoj Business Associates", "department": "General",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Guide",
            "filename": "test-shop.md", "content": BASE_TEXT}, "admin")

    @classmethod
    def tearDownClass(cls):
        k.vectors.close_all()
        TEMP.cleanup()

    def user(self, user_id):
        with k.connect() as db:
            return dict(db.execute("SELECT id,name,email,department,role FROM users WHERE id=?", (user_id,)).fetchone())

    def test_archiving_removes_document_vectors(self):
        customer = self.user("customer")
        doc = k.create_document({"title": "Camera cable guide", "department": "Products",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Guide",
            "filename": "camera-cable.md", "content": "# Camera cable guide\nThis example camera cable uses a USB-C connection for charging. The shop should verify compatibility with each camera before a customer buys it."}, "admin")
        with k.connect() as db:
            chunk_id = db.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],)).fetchone()[0]
        self.assertIn(chunk_id, [hit["id"] for hit in k.vector_candidates("USB-C camera charging cable", customer)])
        self.assertTrue(k.archive_document(doc["id"], "admin"))
        self.assertNotIn(chunk_id, [hit["id"] for hit in k.vector_candidates("USB-C camera charging cable", customer)])

    def test_ifb_seed_and_permission_boundary(self):
        admin, customer = self.user("admin"), self.user("customer")
        self.assertEqual(self.initial_document_count, 3)
        self.assertEqual(len([doc for doc in k.list_documents(customer) if doc["title"].startswith("IFB ")]), 3)
        with k.connect() as db:
            self.assertEqual({row["role"] for row in db.execute("SELECT role FROM users")}, {"admin", "customer"})
        self.assertTrue(any(doc["title"] == "About Manoj Business Associates" for doc in k.list_documents(customer)))
        private = k.create_document({"title": "Private stock note", "department": "Products",
            "classification": "Admin only", "owner": "Shop Admin", "document_type": "Note",
            "filename": "private-stock.md", "content": "# Private stock note\nInternal stock planning notes are for the shop admin only. Customers must not see these private planning details in the knowledge bank."}, "admin")
        self.assertIsNone(k.get_document(private["id"], customer))
        self.assertIsNotNone(k.get_document(private["id"], admin))
        self.assertFalse(any(doc["id"] == private["id"] for doc in k.list_documents(customer)))

    def test_cited_answer_and_mcp_route(self):
        k.create_document({"title": "Sample product guide", "department": "Products",
            "classification": "Public", "owner": "Shop Admin", "document_type": "Guide",
            "filename": "sample-product.md", "content": "# Sample product guide\n## Example item\nThe sample desk lamp has three brightness settings and a USB-C power connection. Ask the shop to confirm current price and availability."}, "admin")
        result = app.answer_query("How many brightness settings does the sample product desk lamp have?", self.user("customer"))
        self.assertFalse(result["no_answer"])
        self.assertIn("MCP", result["route"])
        self.assertEqual(result["model"], "Local extractive")
        self.assertTrue(result["citations"])
        self.assertIn("[1]", result["answer"])
        self.assertEqual(result["citations"][0]["classification"], "Public")

    def test_exact_ifb_model_omits_other_model_source(self):
        result = app.answer_query("What warranty does IFB CI1832D223GN1 3-star AC have?", self.user("customer"))
        self.assertFalse(result["no_answer"])
        self.assertTrue(any("CI1832D223GN1" in citation["title"] for citation in result["citations"]))
        self.assertFalse(any("CI1852D223GN1" in citation["title"] for citation in result["citations"]))

    def test_z_archived_ifb_reference_stays_archived_after_restart(self):
        with k.connect() as db:
            row = db.execute("SELECT id FROM documents WHERE title=? AND status='active'",
                ("IFB 5-star AC warranty - CI1852D223GN1",)).fetchone()
        self.assertTrue(k.archive_document(row["id"], "admin"))
        k.init_db()
        with k.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM documents WHERE id=?", (row["id"],)).fetchone()[0], "archived")

    def test_restricted_source_never_retrieved(self):
        result = app.answer_query("What are the private stock planning details?", self.user("customer"))
        self.assertFalse(any(source["title"] == "Private stock note" for source in result["citations"]))
        self.assertNotIn("Internal stock planning", result["answer"])

    def test_unknown_question_uses_no_answer_path(self):
        result = app.answer_query("What is the cafeteria breakfast menu on Mars?", self.user("admin"))
        self.assertTrue(result["no_answer"])
        self.assertEqual(result["citations"], [])

    def test_duplicate_and_invalid_intake(self):
        with self.assertRaisesRegex(ValueError, "already indexed"):
            k.create_document({"title": "Duplicate", "department": "General", "classification": "Public", "owner": "Test",
                               "document_type": "Guide", "filename": "copy.md",
                               "content": BASE_TEXT}, "admin")
        with self.assertRaisesRegex(ValueError, "Supported file types"):
            k.extract_document("bad.exe", b"ignored")

    def test_docx_and_pdf_text_extraction(self):
        document = io.BytesIO()
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX approval steps</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn("DOCX approval steps", k.extract_document("steps.docx", document.getvalue()))

        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 50 700 Td (PDF escalation guide) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        self.assertIn("PDF escalation guide", k.extract_document("guide.pdf", output.getvalue()))

    def test_analytics_record_activity(self):
        result = app.answer_query("What kind of business is Manoj Business Associates?", self.user("customer"))
        report = k.analytics(self.user("admin"))
        self.assertGreaterEqual(report["queries"], 1)
        self.assertGreaterEqual(report["answered"], 1)
        self.assertEqual(report["reviewed_accuracy_pct"], None)
        k.review_answer(result["id"], self.user("admin"), "correct")
        self.assertEqual(k.analytics(self.user("admin"))["reviewed_accuracy_pct"], 100)
        with self.assertRaises(PermissionError):
            k.review_answer(result["id"], self.user("customer"), "incorrect")

    def test_existing_enterprise_demo_migrates_without_deleting_history(self):
        db_path = Path(TEMP.name) / "migration.db"
        k.init_db(db_path, seed=False)
        with k.connect(db_path) as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                       ("hr", "HR Employee", "hr@demo.local", "HR", "employee", k.password_hash("old")))
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("legacy-doc", "Old HR sample", "HR", "Internal", "Old demo", "Policy",
                        None, None, "Old seed", "old.md", "Old enterprise demo content", "old-checksum",
                        "active", k.utcnow()))
        k.init_db(db_path)
        with k.connect(db_path) as db:
            self.assertEqual({row["id"] for row in db.execute("SELECT id FROM users")}, {"admin", "customer"})
            self.assertEqual(db.execute("SELECT status FROM documents WHERE id='legacy-doc'").fetchone()[0], "archived")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0], 3)

    def test_first_start_clears_existing_document_vectors_once(self):
        db_path = Path(TEMP.name) / "reset.db"
        k.init_db(db_path, seed=False)
        doc = k.create_document({"title": "Old shop guide", "department": "General",
            "classification": "Public", "owner": "Old demo", "document_type": "Guide",
            "filename": "old-shop.md", "content": BASE_TEXT}, "system", db_path)
        with k.connect(db_path) as db:
            chunk_id = db.execute("SELECT id FROM chunks WHERE document_id=?", (doc["id"],)).fetchone()[0]
        customer = {"role": "customer"}
        self.assertIn(chunk_id, [hit["id"] for hit in k.vector_candidates("electronics shop", customer, db_path=db_path)])
        k.init_db(db_path)
        self.assertNotIn(chunk_id, [hit["id"] for hit in k.vector_candidates("electronics shop", customer, db_path=db_path)])
        with k.connect(db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0], 3)

    def test_uploaded_document_survives_restart_seed(self):
        k.init_db()
        self.assertTrue(any(doc["title"] == "About Manoj Business Associates"
                            for doc in k.list_documents(self.user("customer"))))

    def test_sql_sessions_chat_history_and_vector_candidates(self):
        customer = self.user("customer")
        token = app.sign_session(customer["id"])
        self.assertEqual(app.read_session(token), customer["id"])
        result = app.answer_query("What kind of business is Manoj Business Associates?", customer)
        self.assertTrue(result["chat_id"])
        self.assertEqual(len(k.get_chat(result["chat_id"], customer)["messages"]), 1)
        second = app.answer_query("What does Manoj Business Associates deal in?", customer,
                                  chat_id=result["chat_id"])
        self.assertEqual(second["chat_id"], result["chat_id"])
        self.assertEqual(len(k.get_chat(result["chat_id"], customer)["messages"]), 2)
        self.assertTrue(k.vector_candidates("electronics shop", customer))
        with self.assertRaises(PermissionError):
            app.answer_query("What is this business?", self.user("admin"), chat_id=result["chat_id"])
        k.revoke_session(hashlib.sha256(token.encode()).hexdigest())
        self.assertIsNone(app.read_session(token))


if __name__ == "__main__":
    unittest.main()

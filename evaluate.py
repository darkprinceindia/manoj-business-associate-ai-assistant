"""Run the curated evaluation questions against a temporary demo database."""

import csv
import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    os.environ["APP_DB"] = str(Path(directory) / "evaluation.db")
    import knowledge as k
    import app

    k.init_db()
    with k.connect() as db:
        customer = dict(db.execute("SELECT id,name,email,department,role FROM users WHERE id='customer'").fetchone())
    if k.list_documents(customer) or not app.answer_query("What kind of business is Manoj Business Associates?", customer)["no_answer"]:
        raise SystemExit("Empty knowledge bank did not take the no-answer path")
    k.create_document({"title": "About Manoj Business Associates", "department": "General",
                       "classification": "Public", "owner": "Evaluation", "document_type": "Guide",
                       "filename": "evaluation-shop.md",
                       "content": "# About Manoj Business Associates\n## The business\nManoj Business Associates is a shop that deals in electronics items. Customers can ask this assistant about information the shop adds to its knowledge bank.\n## Details to confirm\nThis guide does not list specific brands, models, prices, current stock, or return rules. Ask the shop to confirm details that are not published."}, "system")
    with (k.ROOT / "docs" / "evaluation_queries.csv").open(newline="", encoding="utf-8") as file:
        cases = list(csv.DictReader(file))
    passed = 0
    for case in cases:
        with k.connect() as db:
            user = dict(db.execute("SELECT id,name,email,department,role FROM users WHERE id=?", (case["role"],)).fetchone())
        result = app.answer_query(case["question"], user)
        titles = [source["title"] for source in result["citations"]]
        expected_no_answer = case["expected_behavior"].startswith("no answer")
        source_ok = case["expected_source"] == "none" or case["expected_source"] in titles
        outcome = result["no_answer"] == expected_no_answer and source_ok
        passed += outcome
        print(f"{'PASS' if outcome else 'FAIL'} {case['role']}: {case['question']}")
        print(f"  route={result['route']} sources={titles} answer={result['answer'][:180]}")
    print(f"\n{passed}/{len(cases)} evaluation cases passed")
    k.vectors.close_all()
    raise SystemExit(0 if passed == len(cases) else 1)

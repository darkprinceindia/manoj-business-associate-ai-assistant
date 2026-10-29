# Security and production boundaries

This application demonstrates shop-document retrieval with one Admin and one shared Customer demo account. A fresh knowledge bank indexes three IFB manufacturer references; Admin can add or archive documents afterward. Verify all product and shop policy content before upload. The customer profile form is a client-side preview and does not create or store an account.

Implemented controls:

- Passwords are salted and derived with PBKDF2-SHA256; session tokens are HMAC-signed, stored as hashes in SQLite, revocable on logout, and expire after 12 hours.
- Cookies are `HttpOnly` and `SameSite=Lax`; cross-origin POSTs are rejected. Set TLS termination correctly so cookies receive `Secure` in hosted use.
- Admin and Customer use separate passwords. Chat history is limited to its owner. Queries, source previews, Qdrant candidates, and MCP results are filtered by role and document visibility; SQLite rechecks final access. Admin-only source text is absent from Customer results.
- Model keys remain server-side. Model calls receive only authorized retrieved passages.
- Uploads limit size and extension; filenames are not used as server filesystem paths.
- Audits record indexing, archiving, queries, feedback, and Admin answer reviews.

Required before production use:

1. Replace shared demo accounts with an identity flow and Admin protection suitable for a public shop, including account lifecycle and stronger sign-in controls.
2. Use an approved database, encryption at rest, secure object storage, secret manager, and retention/deletion policy.
3. Add malware scanning, richer parser isolation, MIME validation, rate limits, and request size limits at the reverse proxy.
4. Implement robust citation verification, prompt-injection tests, redaction, human review for sensitive answers, and calibrated evaluation thresholds.
5. Evaluate the embedding and vector search on real shop documents, including irrelevant-question and private-document cases; tune retrieval thresholds from those results.
6. Review every external connector for scope, credential handling, auditability, and data sharing.
7. Review privacy and shop ownership before indexing real customer or business documents.

The login page does not reveal passwords. The README openly lists demo credentials; use different credentials for any shared deployment with real data. Public binding requires explicit `APP_SECRET`, `ADMIN_PASSWORD`, and `CUSTOMER_PASSWORD`, but those settings alone do not make the app production-ready. The Dashboard's reviewed accuracy is an Admin-labeled subset, not an automatic model-accuracy test.

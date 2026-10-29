# API reference

All API endpoints use JSON. Authenticated routes rely on an `HttpOnly`, `SameSite=Lax` session cookie. Cross-origin POST requests are rejected.

| Method | Route | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/login` | Public | Email and password; sets session cookie. |
| POST | `/api/logout` | Authenticated | Revokes the SQL session and clears its cookie. |
| GET | `/api/me` | Public | Current user or `null`. |
| GET | `/api/chats` | Authenticated | List the signed-in user's recent chat threads. |
| GET | `/api/chats/{id}` | Chat owner | Return that thread's saved questions, answers, and citation references. |
| GET | `/api/documents` | Authenticated | List documents visible to the user. |
| GET | `/api/documents/{id}` | Authenticated | Full permitted document for preview. |
| POST | `/api/documents/upload` | Admin | Upload and index a base64-encoded file with metadata. |
| POST | `/api/documents/archive` | Admin | Remove a document from active retrieval. |
| POST | `/api/query` | Authenticated | Classify, route, retrieve, synthesize, and return citations. |
| POST | `/api/feedback` | Query owner/admin | Submit a 1–5 rating and optional issue/comment. |
| GET | `/api/connectors` | Authenticated | List configured MCP tool metadata. |
| GET | `/api/analytics` | Admin | Dashboard totals, feedback, reviewed accuracy, and recent answers. |
| POST | `/api/analytics/review` | Admin | Mark an answered question `correct` or `incorrect`. |

Example query request:

```json
{"question":"What kind of business is Manoj Business Associates?","department":"All","chat_id":null}
```

Example response fields: `id`, `chat_id`, `answer`, `citations` (document ID, title, section, passage, effective date, owner, classification, ranking score), `route`, `department`, `confidence`, `no_answer`, and `latency_ms`. Omit `chat_id` or send `null` to start a thread; send a thread ID returned earlier to append a turn. A thread belongs to one signed-in user. Saved turns are displayed as history; each question is retrieved independently.

For upload and query requests, `department` names the topic: `General`, `Products`, `Orders`, or `Support`. Uploaded documents use `classification: "Public"` for customer-visible content or `"Admin only"` for private Admin material. The two seeded accounts have `admin` and `customer` roles and separate passwords.

Admin review request: `{"query_id":"<answer id>","verdict":"correct"}`. The Dashboard calculates reviewed accuracy from answers marked Correct or Incorrect; it excludes unreviewed and no-answer queries. Customer feedback is reported separately.

Failures use `{"error":"..."}` with HTTP 400 for invalid input, 401 for incorrect credentials, 403 for missing authorization, and 404 for absent resources. The UI caps upload size at 5 MB; the server also enforces the limit.

# Architecture

```text
Browser UI
  ├─ Customer question → authenticated /api/query
  │   ├─ simple topic classification → Products, Orders, Support, General, or All
  │   ├─ question embedding → Qdrant nearest chunk vectors
  │   ├─ Products route → MCP stdio search_curated_knowledge tool
  │   └─ SQLite FTS5 and SQL metadata → hybrid rank and access check
  │       └─ Public/Admin-only permission check + freshness filter
  │           └─ cited synthesis (Gemini if configured; extractive fallback)
  │               └─ SQL chat, query, source, connector, and audit records
  ├─ Admin upload → validation → text extraction → section chunks
  │   └─ FastEmbed embeddings → Qdrant; text + FTS5 → SQLite
  └─ Admin Dashboard → usage, customer feedback, answer reviews
```

FastEmbed's `BAAI/bge-small-en-v1.5` model creates 384-dimensional semantic embeddings for both document chunks and questions. Qdrant stores chunk vectors and filters candidates by collection and visibility. SQLite stores users, hashed session tokens, chat threads, document metadata and text, chunks, FTS5 terms, queries, citations, feedback, answer reviews, connector calls, and audit events. The old hashed lexical-vector SQL column remains only for compatibility. SQL is the final authority for document status, expiry, role permissions, and source text. At startup, active SQL chunks missing from Qdrant are backfilled.

## Access boundary

`Public` documents are available to signed-in Customers and Admins. `Admin only` documents are available only to Admins. The backend enforces this on listings, search, MCP results, and source previews. Expired and archived documents are omitted from retrieval. The old enterprise demo documents are archived when an existing database is migrated. A cutover marker excludes the old query activity from the new Dashboard while retaining it in the database.

## MCP route

`mcp_server.py` implements JSON-RPC `initialize`, `tools/list`, and `tools/call` over standard input/output. The advertised read-only tool is `search_curated_knowledge`. Product questions demonstrate the connector path. The parent process queries Qdrant and sends candidate chunk IDs to the tool, which reranks them against SQLite; this avoids opening local Qdrant storage twice. Connector failures are recorded and fall back to the local index. The backend owns the identity context and rechecks tool results.

## Model behavior

The optional Gemini prompt includes only permitted, retrieved passages and asks for citation markers. The response is accepted only when its citation numbers refer to supplied passages. When the model is unavailable or omits valid citations, extractive synthesis uses the cited source text. Weak evidence takes the no-answer path. Saved chat turns are available for display, but each question currently retrieves evidence independently. The retrieval signal is a ranking score, **not a calibrated probability of correctness**. Dashboard reviewed accuracy comes from Admin labels on answered questions, not the retrieval signal or customer ratings.

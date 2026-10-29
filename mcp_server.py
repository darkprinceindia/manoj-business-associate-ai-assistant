"""Minimal read-only MCP stdio connector for shop product knowledge.

The backend remains the final authorization boundary. This tool receives the
authenticated user's identity and applies the same access rules in search().
"""

import json
import sys
from knowledge import connect, search


def respond(request: dict) -> dict | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    result = None
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "product-knowledge", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "search_curated_knowledge",
                             "description": "Search indexed shop product documents permitted for this account.",
                             "inputSchema": {"type": "object", "properties": {
                                 "question": {"type": "string"}, "user_id": {"type": "string"},
                                 "department": {"type": "string"},
                                 "candidates": {"type": "array"}},
                                 "required": ["question", "user_id", "candidates"]}}]}
    elif method == "tools/call":
        if request.get("params", {}).get("name") != "search_curated_knowledge":
            raise ValueError("Unknown tool")
        args = request["params"].get("arguments", {})
        with connect() as db:
            user = db.execute("SELECT id,name,email,department,role FROM users WHERE id=?", (args.get("user_id"),)).fetchone()
        if not user:
            raise ValueError("Unknown user")
        matches = search(args.get("question", ""), user, args.get("department"),
                         limit=5, candidates=args.get("candidates", []))
        result = {"content": [{"type": "text", "text": json.dumps({"matches": matches})}], "isError": False}
    else:
        raise ValueError("Unknown method")
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


if __name__ == "__main__":
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            response = respond(request)
            if response is not None:
                print(json.dumps(response), flush=True)
        except Exception as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                              "error": {"code": -32603, "message": str(exc)}}), flush=True)

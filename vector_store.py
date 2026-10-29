"""Dense chunk embeddings in Qdrant; SQL remains the authority for documents and access."""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

ROOT = Path(__file__).resolve().parent
MODEL = "BAAI/bge-small-en-v1.5"
COLLECTION = os.environ.get("QDRANT_COLLECTION", "shop_chunks_bge_small_v1")
DIMENSIONS = 384
_lock = threading.RLock()
_clients: dict[str, QdrantClient] = {}
_model: TextEmbedding | None = None


def _embedding_model() -> TextEmbedding:
    global _model
    if _model is None:
        cache = ROOT / "data" / "model-cache"
        cache.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(model_name=MODEL, cache_dir=str(cache))
    return _model


def _client(db_path: Path) -> QdrantClient:
    url = os.environ.get("QDRANT_URL")
    location = url or os.environ.get("QDRANT_PATH") or str(db_path.with_suffix(".qdrant"))
    if location not in _clients:
        if url:
            client = QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY"))
        else:
            path = Path(location)
            path.mkdir(parents=True, exist_ok=True)
            client = QdrantClient(path=str(path))
        if not client.collection_exists(COLLECTION):
            client.create_collection(COLLECTION, vectors_config=models.VectorParams(
                size=DIMENSIONS, distance=models.Distance.COSINE))
        _clients[location] = client
    return _clients[location]


def _point_id(chunk_id: str) -> str:
    return str(uuid.UUID(hex=chunk_id))


def index_chunks(chunks: list[dict], db_path: Path) -> None:
    """Embed passages and upsert their vectors with minimal filter metadata."""
    if not chunks:
        return
    with _lock:
        vectors = list(_embedding_model().embed([c["section"] + " " + c["text"] for c in chunks]))
        points = [models.PointStruct(id=_point_id(c["id"]), vector=vector.tolist(),
                  payload={key: c[key] for key in ("id", "document_id", "department", "classification")})
                  for c, vector in zip(chunks, vectors)]
        _client(db_path).upsert(collection_name=COLLECTION, points=points, wait=True)


def missing_chunks(chunks: list[dict], db_path: Path) -> list[dict]:
    """Return SQL chunks absent from the vector index, for migration or recovery."""
    if not chunks:
        return []
    with _lock:
        found = _client(db_path).retrieve(collection_name=COLLECTION,
                                          ids=[_point_id(c["id"]) for c in chunks],
                                          with_payload=False, with_vectors=False)
    present = {str(point.id) for point in found}
    return [c for c in chunks if _point_id(c["id"]) not in present]


def candidates(question: str, role: str, department: str | None, db_path: Path,
               limit: int = 40) -> list[dict]:
    """Return semantic candidate IDs and cosine scores; SQL checks final access."""
    conditions = []
    if role != "admin":
        conditions.append(models.FieldCondition(key="classification", match=models.MatchValue(value="Public")))
    if department and department != "All":
        conditions.append(models.FieldCondition(key="department", match=models.MatchValue(value=department)))
    with _lock:
        vector = list(_embedding_model().embed([question]))[0].tolist()
        result = _client(db_path).query_points(collection_name=COLLECTION, query=vector,
                                                query_filter=models.Filter(must=conditions) if conditions else None,
                                                limit=limit, with_payload=True).points
    return [{"id": point.payload["id"], "score": float(point.score)} for point in result]


def remove_document(document_id: str, db_path: Path) -> None:
    with _lock:
        _client(db_path).delete(collection_name=COLLECTION,
                                points_selector=models.FilterSelector(filter=models.Filter(must=[
                                    models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))])),
                                wait=True)


def close_all() -> None:
    with _lock:
        for client in _clients.values():
            client.close()
        _clients.clear()

"""Lightweight vector store for semantic search.

Uses SQLite for persistence and fastembed (ONNX Runtime) for embeddings.
No external API calls at query time, no GPU, no torch.

Embedding strategy: BAAI/bge-small-zh-v1.5 via fastembed
- True Chinese semantic embeddings (512-dim, L2-normalized)
- ONNX Runtime backend (~95MB model, no torch/1.5GB dependency)
- Fully offline once the model is cached locally
- Replaces the legacy sklearn TF-IDF + TruncatedSVD(64) approach, which
  degraded to whole-sentence exact matching on CJK text (no semantics).

Model bootstrap:
- Cached under MODEL_CACHE_DIR (persistent, survives restarts).
- First-ever load downloads the ONNX model from HuggingFace. HF is blocked
  in CN, so we route through the local mihomo proxy (PROXY_URL) automatically
  when the model is missing. hf-mirror.com is rate-limited to a crawl for the
  large onnx blob, so the proxy path is preferred.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ..models import KnowledgeEntry

logger = logging.getLogger("layered_memory_mcp.storage.vector")

# --- Embedding model config ---
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512  # bge-small-zh-v1.5 output dimension
MODEL_CACHE_DIR = os.environ.get(
    "LAYERED_MEMORY_MODEL_DIR",
    str(Path.home() / ".layered-memory" / "models"),
)
# Local mihomo (Clash) mixed-proxy port. Used only to bootstrap-download the
# model from HuggingFace when it is not yet cached. Override via env if needed.
PROXY_URL = os.environ.get("LAYERED_MEMORY_HF_PROXY", "http://127.0.0.1:20172")

# Module-level singleton embedder (heavy to construct; share across stores).
_embedder = None
_embedder_lock = threading.Lock()


def _get_embedder():
    """Lazily construct and cache the fastembed TextEmbedding singleton.

    Tries an offline load first (model already cached). If that fails because
    the model isn't downloaded yet, retries with the local proxy exported so
    fastembed can fetch it from HuggingFace.
    """
    global _embedder
    if _embedder is not None:
        return _embedder
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        from fastembed import TextEmbedding

        Path(MODEL_CACHE_DIR).mkdir(parents=True, exist_ok=True)

        # Attempt 1: offline (model already cached) — the normal hot path.
        try:
            _embedder = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
            logger.info("Loaded embedding model %s (offline cache)", MODEL_NAME)
            return _embedder
        except Exception as offline_err:
            logger.warning(
                "Offline model load failed (%s); attempting download via proxy %s",
                offline_err,
                PROXY_URL,
            )

        # Attempt 2: download via local proxy (HF is geo-blocked in CN).
        old_https = os.environ.get("HTTPS_PROXY")
        old_http = os.environ.get("HTTP_PROXY")
        old_all = os.environ.get("ALL_PROXY")
        try:
            os.environ["HTTPS_PROXY"] = PROXY_URL
            os.environ["HTTP_PROXY"] = PROXY_URL
            os.environ["ALL_PROXY"] = PROXY_URL
            _embedder = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
            logger.info("Downloaded + loaded embedding model %s via proxy", MODEL_NAME)
            return _embedder
        finally:
            # Restore prior proxy env so we don't leak it to other code.
            for key, val in (
                ("HTTPS_PROXY", old_https),
                ("HTTP_PROXY", old_http),
                ("ALL_PROXY", old_all),
            ):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


def _embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a list of texts into L2-normalized 512-dim vectors."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    model = _get_embedder()
    vecs = list(model.embed(texts))
    return np.array(vecs, dtype=np.float32)


class VectorStore:
    """SQLite-backed vector store for semantic search.

    Each entry stores:
      - id: unique identifier
      - domain: knowledge domain
      - text: searchable text (summary + content)
      - vector: JSON-serialized 512-dim float array (bge-small-zh-v1.5)
      - metadata: JSON dict
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if not exists."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vectors (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    text TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vectors_domain ON vectors(domain)
            """)
            conn.commit()

    def add(self, entry: "KnowledgeEntry") -> None:
        """Add or update a knowledge entry in the vector store."""
        text = f"{entry.summary}\n{entry.content}".strip()

        # Exact (domain, text) dedup guard. A verbatim duplicate is a no-op.
        # (Historically a fresh random uuid per call defeated INSERT OR REPLACE
        # and let vectors.db balloon to 4449 rows from ~120 unique entries.)
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT 1 FROM vectors WHERE domain = ? AND text = ? LIMIT 1",
                (entry.domain, text),
            ).fetchone()
        if existing:
            return

        # bge embeds each text independently — no corpus refit needed. This is
        # a major simplification over the old TF-IDF path, which had to refit
        # the whole vocabulary on every single add.
        vector = _embed_texts([text])[0]
        vector_json = json.dumps(vector.tolist())

        metadata = {
            "type": entry.type.value,
            "confidence": entry.confidence,
            "tags": entry.tags,
            "section": entry.section,
        }

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO vectors (id, domain, text, vector, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry.id, entry.domain, text, vector_json, json.dumps(metadata)),
            )
            conn.commit()

    def delete(self, entry_id: str) -> None:
        """Remove an entry from the vector store."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM vectors WHERE id = ?", (entry_id,))
            conn.commit()

    def search(
        self,
        query: str,
        top_n: int = 5,
        domain: str | None = None,
    ) -> list[dict]:
        """Semantic search by query string.

        Returns list of {id, domain, text, metadata, score} sorted by score.
        Scores are cosine similarity in [-1, 1]; bge vectors are L2-normalized
        so this reduces to a dot product.
        """
        with sqlite3.connect(self.db_path) as conn:
            if domain:
                rows = conn.execute(
                    "SELECT id, domain, text, vector, metadata FROM vectors WHERE domain = ?",
                    (domain,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, domain, text, vector, metadata FROM vectors"
                ).fetchall()

        if not rows:
            return []

        ids, texts, domains, metadatas = [], [], [], []
        vectors = []
        for row in rows:
            vec = np.array(json.loads(row[3]), dtype=np.float32)
            # Skip legacy-dimension rows defensively. After rebuild() all rows
            # are 512-dim; this guards against a half-migrated DB.
            if vec.shape[0] != EMBED_DIM:
                continue
            ids.append(row[0])
            domains.append(row[1])
            texts.append(row[2])
            vectors.append(vec)
            metadatas.append(json.loads(row[4]) if row[4] else {})

        if not vectors:
            return []

        query_vec = _embed_texts([query])[0]
        matrix = np.vstack(vectors)
        # Vectors are L2-normalized → cosine == dot product.
        sims = matrix @ query_vec

        order = np.argsort(sims)[::-1]
        results = []
        for idx in order[:top_n]:
            score = float(sims[idx])
            if score <= 0:
                continue
            text = texts[idx]
            results.append({
                "id": ids[idx],
                "domain": domains[idx],
                "text": text[:200] + "..." if len(text) > 200 else text,
                "metadata": metadatas[idx],
                "score": round(score, 4),
            })
        return results

    def rebuild(self) -> dict:
        """Re-embed every stored entry with the current model.

        Required after switching embedding models (e.g. TF-IDF-64 →
        bge-small-zh-512), since old vectors live in an incompatible space.
        Reads each row's `text`, recomputes its vector in place. Idempotent.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT id, text FROM vectors").fetchall()

        if not rows:
            return {"rebuilt": 0, "dim": EMBED_DIM}

        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        # Batch-embed all texts in one model call.
        vectors = _embed_texts(texts)

        with sqlite3.connect(self.db_path) as conn:
            for entry_id, vec in zip(ids, vectors):
                conn.execute(
                    "UPDATE vectors SET vector = ? WHERE id = ?",
                    (json.dumps(vec.tolist()), entry_id),
                )
            conn.commit()

        return {"rebuilt": len(ids), "dim": EMBED_DIM}

    def stats(self) -> dict:
        """Return store statistics."""
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            domains = conn.execute(
                "SELECT domain, COUNT(*) FROM vectors GROUP BY domain"
            ).fetchall()

        return {
            "total_entries": count,
            "domains": {d: c for d, c in domains},
            "model": MODEL_NAME,
            "dim": EMBED_DIM,
            # bge has no "fit" step — the store is ready whenever it has rows.
            # Kept for backward compat with the dashboard's `is_fitted` checks.
            "is_fitted": count > 0,
            "db_path": str(self.db_path),
        }

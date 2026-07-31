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
- First-ever load downloads the ONNX model from HuggingFace. In networks where
  HuggingFace is unreachable, set LAYERED_MEMORY_HF_PROXY to a local HTTP proxy
  and it will be used automatically to fetch the model when it is missing.
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
# Optional local HTTP proxy used ONLY to bootstrap-download the model from
# HuggingFace when it is not yet cached (e.g. on networks where HF is
# unreachable). Unset by default — set LAYERED_MEMORY_HF_PROXY to enable.
PROXY_URL = os.environ.get("LAYERED_MEMORY_HF_PROXY", "")

# Module-level singleton embedder (heavy to construct; share across stores).
_embedder = None
_embedder_lock = threading.Lock()


# Common local HTTP proxy ports to probe as a last resort when a direct
# HuggingFace download fails and no proxy was explicitly configured. Covers the
# defaults of the most common desktop proxy tools.
_COMMON_PROXY_PORTS = (7890, 7897, 8080, 1087, 8889)


def _detect_local_proxy() -> str | None:
    """Return a reachable local proxy URL, or None if none is listening.

    Probes 127.0.0.1 on a few common proxy ports with a short TCP connect.
    Used only as an opt-in fallback for bootstrapping the model download on
    networks where HuggingFace isn't directly reachable.
    """
    import socket

    for port in _COMMON_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                url = f"http://127.0.0.1:{port}"
                logger.info("Auto-detected local proxy at %s", url)
                return url
        except OSError:
            continue
    return None


def _load_via_proxy(proxy_url: str):
    """Load TextEmbedding with proxy env temporarily exported, then restore."""
    from fastembed import TextEmbedding

    saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")}
    try:
        for k in saved:
            os.environ[k] = proxy_url
        model = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
        logger.info("Downloaded + loaded embedding model %s via proxy", MODEL_NAME)
        return model
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _get_embedder():
    """Lazily construct and cache the fastembed TextEmbedding singleton.

    Load order:
      1. Offline (model already cached) — the normal hot path.
      2. Direct download from HuggingFace.
      3. Proxy download: an explicit LAYERED_MEMORY_HF_PROXY if set, otherwise
         an auto-detected local proxy (common desktop proxy ports).
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
                "Offline model load failed (%s); attempting download", offline_err
            )

        # Attempt 2: direct download from HuggingFace.
        try:
            _embedder = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
            logger.info("Downloaded + loaded embedding model %s (direct)", MODEL_NAME)
            return _embedder
        except Exception as direct_err:
            logger.warning("Direct download failed (%s); trying proxy", direct_err)

        # Attempt 3: proxy download. Explicit env wins; otherwise auto-detect.
        proxy_url = PROXY_URL or _detect_local_proxy()
        if not proxy_url:
            raise RuntimeError(
                "Could not load or download the embedding model. HuggingFace is "
                "unreachable and no local proxy was found. Set "
                "LAYERED_MEMORY_HF_PROXY to a working HTTP proxy and retry."
            )
        _embedder = _load_via_proxy(proxy_url)
        return _embedder


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
        # In-memory vector cache. The whole table's vectors are parsed once and
        # held as a single (N, EMBED_DIM) float32 matrix so repeated searches
        # skip JSON deserialization + vstack on every call. Invalidated on any
        # add/delete/rebuild. Guarded by a lock for thread-safe rebuilds.
        self._cache_lock = threading.Lock()
        self._cache_valid = False
        self._cache_ids: list[str] = []
        self._cache_domains: list[str] = []
        self._cache_texts: list[str] = []
        self._cache_metas: list[dict] = []
        self._cache_matrix: np.ndarray | None = None

    def _invalidate_cache(self) -> None:
        """Drop the in-memory vector cache; next search rebuilds it lazily."""
        with self._cache_lock:
            self._cache_valid = False
            self._cache_matrix = None
            self._cache_ids = []
            self._cache_domains = []
            self._cache_texts = []
            self._cache_metas = []

    def _ensure_cache(self) -> None:
        """Populate the in-memory vector cache from SQLite if stale.

        Parses every row's JSON vector exactly once per invalidation, instead
        of on every search call. Legacy-dimension rows are skipped defensively.
        """
        if self._cache_valid:
            return
        with self._cache_lock:
            if self._cache_valid:  # double-checked under lock
                return
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, domain, text, vector, metadata FROM vectors"
                ).fetchall()

            ids, domains, texts, metas, vectors = [], [], [], [], []
            for row in rows:
                vec = np.array(json.loads(row[3]), dtype=np.float32)
                if vec.shape[0] != EMBED_DIM:
                    continue
                ids.append(row[0])
                domains.append(row[1])
                texts.append(row[2])
                vectors.append(vec)
                metas.append(json.loads(row[4]) if row[4] else {})

            self._cache_ids = ids
            self._cache_domains = domains
            self._cache_texts = texts
            self._cache_metas = metas
            self._cache_matrix = (
                np.vstack(vectors) if vectors else np.zeros((0, EMBED_DIM), np.float32)
            )
            self._cache_valid = True

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
        self._invalidate_cache()

    def delete(self, entry_id: str) -> None:
        """Remove an entry from the vector store."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM vectors WHERE id = ?", (entry_id,))
            conn.commit()
        self._invalidate_cache()

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

        Uses an in-memory vector matrix cached across calls (see _ensure_cache),
        so repeated searches avoid re-parsing every row's JSON vector.
        """
        self._ensure_cache()
        if self._cache_matrix is None or self._cache_matrix.shape[0] == 0:
            return []

        # Restrict to a domain by masking the cached arrays (cheap vs a DB hit).
        if domain is not None:
            sel = [i for i, d in enumerate(self._cache_domains) if d == domain]
            if not sel:
                return []
            matrix = self._cache_matrix[sel]
            ids = [self._cache_ids[i] for i in sel]
            domains = [self._cache_domains[i] for i in sel]
            texts = [self._cache_texts[i] for i in sel]
            metadatas = [self._cache_metas[i] for i in sel]
        else:
            matrix = self._cache_matrix
            ids = self._cache_ids
            domains = self._cache_domains
            texts = self._cache_texts
            metadatas = self._cache_metas

        query_vec = _embed_texts([query])[0]
        # Vectors are L2-normalized → cosine == dot product.
        sims = matrix @ query_vec

        # Top-n by score. argpartition avoids a full sort of large corpora,
        # then we sort only the candidate slice. score<=0 (semantically
        # opposite or unrelated) is dropped — bge unrelated pairs sit well
        # above 0, so this only trims genuine non-matches.
        n = sims.shape[0]
        k = min(top_n, n)
        cand = np.argpartition(sims, n - k)[-k:]
        order = cand[np.argsort(sims[cand])[::-1]]
        results = []
        for idx in order:
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

        self._invalidate_cache()
        return {"rebuilt": len(ids), "dim": EMBED_DIM}

    def warmup(self) -> bool:
        """Eagerly load the embedding model so the first search isn't slow.

        The first-ever model load may download ~55MB (and on a throttled
        network can take many minutes), during which a blocking search would
        appear to hang. Call this once at startup, ideally from a background
        thread, so the model is hot before any user-facing query arrives.

        Returns True if the model is ready, False if loading failed (the store
        still works for add/delete; only semantic search needs the model).
        """
        try:
            _get_embedder()
            return True
        except Exception as err:  # noqa: BLE001 - surface as a soft failure
            logger.warning("Embedding model warmup failed: %s", err)
            return False

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

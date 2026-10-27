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
from contextlib import closing, contextmanager
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
      3. Proxy download through the explicitly configured
         LAYERED_MEMORY_HF_PROXY, when set.
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

        # Attempt 3: proxy download only when explicitly configured. Never probe
        # local ports: a library must not guess the operator's network topology.
        proxy_url = PROXY_URL
        if not proxy_url:
            raise RuntimeError(
                "Could not load or download the embedding model. HuggingFace is "
                "unreachable and no proxy was configured. Set "
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


def entry_embedding_text(entry: "KnowledgeEntry") -> str:
    """Normalize the heading only; keep a distinct authored summary intact."""
    from ..heading import embedding_heading

    heading = embedding_heading(entry.section)
    parts = [heading]
    if entry.summary.strip() not in ("", entry.section.strip(), heading):
        parts.append(entry.summary.strip())
    parts.append(entry.content.strip())
    return "\n".join(parts).strip()


def entry_metadata(entry: "KnowledgeEntry") -> dict:
    """The metadata one stored row carries: display fields plus graph identity.

    One producer for both write paths (``add`` and ``replace_entries``), because
    two inline dicts are how the two paths would start disagreeing about what a
    row says about itself.

    v3.6.0 Batch I-A adds the canonical ``graph_node_id`` beside the existing
    keys — additive, so every existing consumer of ``type``/``confidence``/
    ``tags``/``section`` is unaffected. An entry whose identity cannot produce a
    stable node ID simply has no such key, which the recall seam already reports
    as an unmapped row rather than treating as a fabricated one.

    The import is function-local on purpose: this module sits on the write hot
    path and the graph layer is not a dependency of embedding or search.
    """
    from ..graph_seed_metadata import merge_graph_seed_metadata

    return merge_graph_seed_metadata(
        {
            "type": entry.type.value,
            "confidence": entry.confidence,
            "tags": entry.tags,
            "section": entry.section,
        },
        entry,
    )


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
        self._closed = False
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
        # data_version is connection-relative. Keep a read-only observer so a
        # long-lived reader sees commits from maintenance and other processes.
        self._cache_observer = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False,
        )
        self._cache_version: int | None = None

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("vector store is closed")

    @contextmanager
    def _connect(self):
        """Close transient connections as well as committing/rolling back them."""
        self._check_open()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                yield conn

    def close(self) -> None:
        """Release the persistent cache observer; safe to call repeatedly."""
        with self._cache_lock:
            if not self._closed:
                self._cache_observer.close()
                self._closed = True
                self._cache_valid = False
                self._cache_matrix = None

    def __enter__(self) -> "VectorStore":
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

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
        with self._cache_lock:
            self._check_open()
            version = self._cache_observer.execute("PRAGMA data_version").fetchone()[0]
            if self._cache_valid and self._cache_version == version:
                return
            with self._connect() as conn:
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
            self._cache_version = version

    def _init_db(self) -> None:
        """Create tables if not exists."""
        with self._connect() as conn:
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

    def add(self, entry: "KnowledgeEntry", *, validate_source=None) -> None:
        """Prepare and atomically publish an entry unless its snapshot is stale.

        ``validate_source`` may reject publication if an external source changed
        during preparation. File-backed domain writers should use injector sync,
        which rereads and validates the authoritative file, including its ID.
        """
        expected = self.snapshot()
        entry = entry.model_copy(deep=True)
        text = entry_embedding_text(entry)

        # Exact (domain, text) dedup guard. A verbatim duplicate is a no-op.
        # (Historically a fresh random uuid per call defeated INSERT OR REPLACE
        # and let vectors.db balloon to 4449 rows from ~120 unique entries.)
        matches = [row for row in expected if row[1] == entry.domain and row[2] == text]
        existing = next((row for row in matches if row[0] == entry.id),
                        matches[0] if matches else None)

        # bge embeds each text independently — no corpus refit needed. This is
        # a major simplification over the old TF-IDF path, which had to refit
        # the whole vocabulary on every single add.
        # A heading date/tag-only edit can leave embedding text unchanged.
        # Reuse the vector while still refreshing this entry's metadata.
        if existing:
            vector_json = existing[3]
        else:
            vectors = _embed_texts([text])
            if vectors.shape != (1, EMBED_DIM) or not np.isfinite(vectors).all():
                raise ValueError("embedding returned invalid shape or non-finite values")
            vector_json = json.dumps(vectors[0].tolist())

        metadata = entry_metadata(entry)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_publication(conn, expected, validate_source)
            if existing and existing[0] != entry.id:
                return
            conn.execute(
                """
                INSERT OR REPLACE INTO vectors (id, domain, text, vector, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry.id, entry.domain, text, vector_json, json.dumps(metadata)),
            )
            conn.commit()
        self._invalidate_cache()

    def snapshot(self) -> list[tuple]:
        """Committed rows for optimistic publication; no embedding or writes."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT id, domain, text, vector, metadata FROM vectors ORDER BY id"
            ).fetchall()

    @staticmethod
    def _validate_publication(conn, expected: list[tuple], validate_source=None) -> None:
        """Check both snapshots while the SQLite publication transaction is held."""
        current = conn.execute(
            "SELECT id, domain, text, vector, metadata FROM vectors ORDER BY id"
        ).fetchall()
        if current != expected:
            raise RuntimeError("vector store changed during preparation; retry required")
        if validate_source is not None:
            validate_source()

    def replace_entries(
        self,
        entries: list["KnowledgeEntry"],
        *,
        domains: list[str] | None = None,
        expected: list[tuple],
        validate_source=None,
    ) -> None:
        """Prepare first, then publish all replacements in one transaction.

        Refuse publication if another writer changed the store or the caller's
        source snapshot changed during embedding. Readers keep the previous
        committed rows on any failure, including a failure halfway through SQL.
        ``domains=None`` replaces the corpus; a list replaces just those domains.
        """
        self._check_open()
        texts = [entry_embedding_text(entry) for entry in entries]
        vectors = _embed_texts(texts)
        if vectors.shape != (len(entries), EMBED_DIM) or not np.isfinite(vectors).all():
            raise ValueError("embedding returned invalid shape or non-finite values")
        replacement = [
            (entry.id, entry.domain, text, json.dumps(vector.tolist()),
             json.dumps(entry_metadata(entry)))
            for entry, text, vector in zip(entries, texts, vectors)
        ]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_publication(conn, expected, validate_source)
            if domains is None:
                conn.execute("DELETE FROM vectors")
            else:
                conn.executemany("DELETE FROM vectors WHERE domain = ?", [(d,) for d in domains])
            conn.executemany(
                "INSERT INTO vectors (id, domain, text, vector, metadata) VALUES (?, ?, ?, ?, ?)",
                replacement,
            )
        self._invalidate_cache()

    def delete(self, entry_id: str) -> None:
        """Remove an entry from the vector store."""
        with self._connect() as conn:
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
        with self._connect() as conn:
            rows = conn.execute("SELECT id, text FROM vectors").fetchall()

        if not rows:
            return {"rebuilt": 0, "dim": EMBED_DIM}

        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        # Batch-embed all texts in one model call.
        vectors = _embed_texts(texts)

        with self._connect() as conn:
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
        with self._connect() as conn:
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

"""Lightweight vector store for semantic search.

Uses SQLite for persistence and numpy/scikit-learn for embeddings.
No external API calls, no GPU required.

Embedding strategy: TF-IDF + TruncatedSVD
- Fast to compute (no neural network)
- No model download required
- Good enough for knowledge retrieval
- Fully offline
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

if TYPE_CHECKING:
    from ..models import KnowledgeEntry

logger = logging.getLogger("layered_memory_mcp.storage.vector")

DEFAULT_DIM = 64


class VectorStore:
    """SQLite-backed vector store for semantic search.

    Each entry stores:
      - id: unique identifier
      - domain: knowledge domain
      - text: searchable text (summary + content)
      - vector: JSON-serialized numpy array
      - metadata: JSON dict
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # Embedding components (fitted on demand)
        self._vectorizer: TfidfVectorizer | None = None
        self._svd: TruncatedSVD | None = None
        self._is_fitted = False

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

    def _fit(self, texts: list[str]) -> None:
        """Fit the embedding model on the given texts."""
        if len(texts) == 0:
            self._is_fitted = False
            return

        self._vectorizer = TfidfVectorizer(
            max_features=1000,
            min_df=1,
            stop_words="english",
        )
        X = self._vectorizer.fit_transform(texts)

        n_features = X.shape[1]
        n_components = min(DEFAULT_DIM, max(n_features - 1, 1))

        self._svd = TruncatedSVD(n_components=n_components)
        self._svd.fit(X)
        self._is_fitted = True

    def _embed(self, texts: list[str]) -> np.ndarray:
        """Embed texts into vectors."""
        if not self._is_fitted or self._vectorizer is None or self._svd is None:
            # Return zero vectors if not fitted
            return np.zeros((len(texts), DEFAULT_DIM))

        X = self._vectorizer.transform(texts)
        return self._svd.transform(X)

    def add(self, entry: "KnowledgeEntry") -> None:
        """Add or update a knowledge entry in the vector store."""
        text = f"{entry.summary}\n{entry.content}".strip()

        # Get all existing texts to refit
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT text FROM vectors WHERE id != ?", (entry.id,)
            ).fetchall()

        all_texts = [r[0] for r in rows] + [text]
        self._fit(all_texts)

        # Embed
        vector = self._embed([text])[0]
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
        """
        if not self._is_fitted:
            return []

        # Get candidate vectors
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

        ids = []
        texts = []
        domains = []
        metadatas = []
        vectors = []

        for row in rows:
            ids.append(row[0])
            domains.append(row[1])
            texts.append(row[2])
            vec = json.loads(row[3])
            vectors.append(np.array(vec, dtype=np.float32))
            metadatas.append(json.loads(row[4]) if row[4] else {})

        # Embed query
        query_vec = self._embed([query])

        # Compute similarities - handle different vector dimensions
        similarities = []
        for vec in vectors:
            # Ensure same dimension
            if len(vec) != len(query_vec[0]):
                # Pad or truncate
                target_len = len(query_vec[0])
                if len(vec) < target_len:
                    vec = np.pad(vec, (0, target_len - len(vec)))
                else:
                    vec = vec[:target_len]
            sim = cosine_similarity(query_vec.reshape(1, -1), vec.reshape(1, -1))[0][0]
            similarities.append(sim)

        similarities = np.array(similarities)

        # Sort by similarity
        indexed = list(enumerate(similarities))
        indexed.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in indexed[:top_n]:
            if score <= 0:
                continue
            results.append({
                "id": ids[idx],
                "domain": domains[idx],
                "text": texts[idx][:200] + "..." if len(texts[idx]) > 200 else texts[idx],
                "metadata": metadatas[idx],
                "score": round(float(score), 4),
            })

        return results

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
            "is_fitted": self._is_fitted,
            "db_path": str(self.db_path),
        }

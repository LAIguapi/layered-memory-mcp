"""The single embedding call site must forward one bounded batch size.

fastembed's default batch (256) sizes the ONNX Runtime arena for the largest
batch the process ever ran, so a one-off reindex keeps that memory for the rest
of the service's life. Batching only groups forward passes, so the guarantee
this file protects is that bounding it changes *nothing* a caller can observe:
the same texts, in the same order, with the same vectors.
"""

from __future__ import annotations

import numpy as np
import pytest

from layered_memory_mcp.storage import vector_store


class _RecordingModel:
    """Stand-in embedder that records how fastembed would have been called."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, **kwargs):
        materialized = list(texts)
        self.calls.append((materialized, kwargs))
        for index, _ in enumerate(materialized):
            yield np.full(vector_store.EMBED_DIM, float(index), dtype=np.float32)


@pytest.fixture
def recording(monkeypatch):
    model = _RecordingModel()
    monkeypatch.setattr(vector_store, "_get_embedder", lambda: model)
    return model


def test_the_bounded_batch_size_is_a_small_centralized_constant():
    assert vector_store.EMBED_BATCH_SIZE == 16


def test_embedding_forwards_the_bounded_batch_size(recording):
    vector_store._embed_texts(["alpha", "beta"])
    assert len(recording.calls) == 1
    _, kwargs = recording.calls[0]
    assert kwargs == {"batch_size": vector_store.EMBED_BATCH_SIZE}


@pytest.mark.parametrize("count", [1, 15, 16, 17, 40])
def test_every_text_is_embedded_once_in_the_caller_order(recording, count):
    texts = [f"text-{index}" for index in range(count)]
    vectors = vector_store._embed_texts(texts)
    assert [call[0] for call in recording.calls] == [texts]
    assert vectors.shape == (count, vector_store.EMBED_DIM)
    # Row i carries value i, so this proves order was preserved, not just count.
    assert [float(row[0]) for row in vectors] == [float(i) for i in range(count)]


def test_an_empty_request_still_never_constructs_the_model(monkeypatch):
    def forbidden():
        pytest.fail("an empty embed request must not load the model")

    monkeypatch.setattr(vector_store, "_get_embedder", forbidden)
    assert vector_store._embed_texts([]).shape == (0, vector_store.EMBED_DIM)


def test_the_module_has_exactly_one_model_embed_call_site():
    """A second call site is how an unbounded batch would come back."""
    from pathlib import Path

    source = Path(vector_store.__file__).read_text(encoding="utf-8")
    call_sites = [line for line in source.splitlines() if "model.embed(" in line]
    assert call_sites == ["    vecs = list(model.embed(texts, batch_size=EMBED_BATCH_SIZE))"]

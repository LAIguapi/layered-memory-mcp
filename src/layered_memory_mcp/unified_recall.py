"""Opt-in unified hybrid retrieval.

The legacy recall functions remain the compatibility path.  This module owns
the new read pipeline so MCP, REST, and isolated callers use the same stage
ordering, identity rules, fusion, and diagnostics.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .storage.knowledge_reader import KnowledgeReadError, read_knowledge_text


@dataclass
class _Candidate:
    entry_id: str
    source: str
    rank: int
    score: float
    payload: dict[str, Any]
    provenance: set[str] = field(default_factory=set)
    source_ranks: dict[str, int] = field(default_factory=dict)
    fused_score: float = 0.0


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) else 0.0


def _identity(row: Mapping[str, Any]) -> str | None:
    for key in ("entry_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("entry_id", "id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = row.get("file")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_reason(stage: str, exc: BaseException) -> str:
    return f"{stage}_failed:{type(exc).__name__}"


def _invoke_search(channel: Any, query: str, top_n: int, domain: str | None) -> Any:
    if channel is None:
        return []
    method = getattr(channel, "search", None)
    if method is None:
        if callable(channel):
            return channel(query, top_n=top_n, domain=domain)
        raise TypeError("channel has no search operation")
    try:
        return method(query, top_n=top_n, domain=domain)
    except TypeError:
        try:
            return method(query, top_n)
        except TypeError:
            return method(query)


def _serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serializable(item) for item in value]
    return type(value).__name__


class UnifiedRecallEngine:
    """Run the explicit L0 → channels → graph → L1 → fusion pipeline.

    Adapters are deliberately duck-typed.  A channel provides ``search``; the
    router provides ``route``; graph provides ``filter_and_expand``; and L1
    provides ``hydrate``.  This keeps the engine usable with temporary fakes in
    contract tests and with the repository's existing stores.
    """

    def __init__(
        self,
        *,
        vector: Any = None,
        bm25: Any = None,
        router: Any = None,
        graph: Any = None,
        l1: Any = None,
        rrf_k: int = 60,
    ) -> None:
        self.vector = vector
        self.bm25 = bm25
        self.router = router
        self.graph = graph
        self.l1 = l1
        self.rrf_k = max(1, int(rrf_k))

    def retrieve(
        self,
        query: str,
        *,
        top_n: int = 5,
        domain: str | None = None,
    ) -> dict[str, Any]:
        top_n = max(1, int(top_n))
        reasons: list[str] = []
        stages: dict[str, dict[str, Any]] = {}
        stage_order: list[str] = []

        route = self._route(query, stages, stage_order, reasons)
        vector_rows = self._channel(
            "vector", self.vector, query, top_n, domain, stages, stage_order, reasons
        )
        bm25_rows = self._channel(
            "bm25", self.bm25, query, top_n, domain, stages, stage_order, reasons
        )
        merged = self._merge(vector_rows, bm25_rows, stages, stage_order)
        graph_rows = self._graph(
            merged, query, route, stages, stage_order, reasons
        )
        hydrated = self._hydrate(graph_rows, stages, stage_order, reasons)
        results = self._fuse(hydrated, top_n, stages, stage_order)

        trace = {
            "stage_order": stage_order,
            "stages": stages,
            "degraded": bool(reasons),
            "fallback_reasons": sorted(set(reasons)),
        }
        payload = {
            "success": True,
            "query": query,
            "results": results,
            "metadata": {"trace": trace},
            "trace": trace,
        }
        json.dumps(payload)
        return payload

    def _route(self, query, stages, order, reasons):
        order.append("route")
        if self.router is None:
            stages["route"] = {
                "occurred": False,
                "degraded": False,
                "route_count": 0,
                "reason": "no_router",
            }
            return None
        try:
            route_method = getattr(self.router, "route", self.router)
            route = route_method(query)
            count = len(route) if isinstance(route, (list, tuple, set, dict)) else int(route is not None)
            degraded = route is None or route == [] or route == {}
            if degraded:
                reasons.append("route_empty")
            stages["route"] = {
                "occurred": True,
                "degraded": degraded,
                "route_count": count,
                "reason": "empty_route" if degraded else "ok",
            }
            return route
        except Exception as exc:  # noqa: BLE001 - routing is explicitly soft
            reason = _safe_reason("route", exc)
            reasons.append(reason)
            stages["route"] = {
                "occurred": True,
                "degraded": True,
                "route_count": 0,
                "reason": reason,
            }
            return None

    def _channel(self, name, channel, query, top_n, domain, stages, order, reasons):
        order.append(name)
        if channel is None:
            stages[name] = {
                "occurred": False,
                "degraded": True,
                "candidate_count": 0,
                "reason": f"no_{name}_channel",
            }
            reasons.append(f"{name}_unavailable")
            return []
        try:
            raw_rows = _invoke_search(channel, query, top_n, domain) or []
            if isinstance(raw_rows, Mapping):
                raw_rows = raw_rows.get("results", [])
            rows = list(raw_rows)
            candidates: list[_Candidate] = []
            for rank, raw in enumerate(rows, start=1):
                if not isinstance(raw, Mapping):
                    continue
                entry_id = _identity(raw)
                if entry_id is None:
                    continue
                candidates.append(
                    _Candidate(
                        entry_id=entry_id,
                        source=name,
                        rank=rank,
                        score=_number(raw.get("score")),
                        payload=dict(raw),
                        provenance={name},
                        source_ranks={name: rank},
                    )
                )
            stages[name] = {
                "occurred": True,
                "degraded": False,
                "candidate_count": len(candidates),
                "raw_count": len(rows),
            }
            return candidates
        except Exception as exc:  # noqa: BLE001 - one channel must not erase the other
            reason = _safe_reason(name, exc)
            reasons.append(reason)
            stages[name] = {
                "occurred": True,
                "degraded": True,
                "candidate_count": 0,
                "reason": reason,
            }
            return []

    def _merge(self, vector_rows, bm25_rows, stages, order):
        order.append("identity_merge")
        merged: dict[str, _Candidate] = {}
        duplicate_count = 0
        for candidate in [*vector_rows, *bm25_rows]:
            existing = merged.get(candidate.entry_id)
            if existing is None:
                merged[candidate.entry_id] = candidate
                continue
            duplicate_count += 1
            existing.provenance.update(candidate.provenance)
            existing.source_ranks.update(candidate.source_ranks)
            if (candidate.score, -candidate.rank, candidate.source) > (
                existing.score, -existing.rank, existing.source
            ):
                existing.payload = candidate.payload
                existing.score = candidate.score
                existing.rank = candidate.rank
        for candidate in merged.values():
            candidate.fused_score = sum(
                1.0 / (self.rrf_k + rank)
                for rank in candidate.source_ranks.values()
            )
        stages["identity_merge"] = {
            "occurred": True,
            "degraded": False,
            "input_count": len(vector_rows) + len(bm25_rows),
            "candidate_count": len(merged),
            "duplicate_count": duplicate_count,
            "provenance": {
                entry_id: sorted(candidate.provenance)
                for entry_id, candidate in sorted(merged.items())
            },
            "source_ranks": {
                entry_id: dict(sorted(candidate.source_ranks.items()))
                for entry_id, candidate in sorted(merged.items())
            },
        }
        return list(merged.values())

    def _graph(self, candidates, query, route, stages, order, reasons):
        order.append("graph")
        if self.graph is None:
            stages["graph"] = {
                "occurred": False,
                "degraded": True,
                "input_count": len(candidates),
                "candidate_count": len(candidates),
                "reason": "no_graph_adapter",
            }
            reasons.append("graph_unavailable")
            return candidates
        try:
            method = getattr(self.graph, "filter_and_expand", None)
            if method is None:
                method = getattr(self.graph, "guard", None)
            if method is None:
                raise TypeError("graph has no guard operation")
            graph_input = [
                {
                    "entry_id": candidate.entry_id,
                    "id": candidate.entry_id,
                    "score": candidate.score,
                    "metadata": candidate.payload.get("metadata", {}),
                    "provenance": sorted(candidate.provenance),
                }
                for candidate in candidates
            ]
            raw = method(graph_input, query=query, route=route)
            expanded = []
            if isinstance(raw, Mapping):
                kept = raw.get("candidates", raw.get("results", []))
                expanded = raw.get("expanded", []) or []
                graph_reason = raw.get("reason")
            else:
                kept = raw or []
                graph_reason = None
            result = self._graph_candidates(kept, candidates)
            result.extend(self._graph_candidates(expanded, candidates, source="graph"))
            deduped: dict[str, _Candidate] = {}
            for candidate in result:
                if candidate.entry_id in deduped:
                    deduped[candidate.entry_id].provenance.update(candidate.provenance)
                else:
                    deduped[candidate.entry_id] = candidate
            result = list(deduped.values())
            if not result and candidates:
                reasons.append("graph_rejected_all")
            if graph_reason:
                reasons.append(str(graph_reason))
            stages["graph"] = {
                "occurred": True,
                "degraded": bool(graph_reason),
                "input_count": len(candidates),
                "candidate_count": len(result),
                "expanded_count": len(expanded),
                "reason": graph_reason or ("rejected_all" if not result and candidates else "ok"),
            }
            return result
        except Exception as exc:  # noqa: BLE001 - graph outage is observable fallback
            reason = _safe_reason("graph", exc)
            reasons.append(reason)
            stages["graph"] = {
                "occurred": True,
                "degraded": True,
                "input_count": len(candidates),
                "candidate_count": len(candidates),
                "reason": reason,
            }
            return candidates

    @staticmethod
    def _graph_candidates(rows, originals, source=None):
        by_id = {candidate.entry_id: candidate for candidate in originals}
        converted = []
        for row in rows:
            if isinstance(row, _Candidate):
                converted.append(row)
                continue
            if not isinstance(row, Mapping):
                continue
            entry_id = _identity(row)
            if entry_id is None:
                continue
            original = by_id.get(entry_id)
            if original is not None:
                original.provenance.update({source} if source else set())
                converted.append(original)
            elif source:
                converted.append(
                    _Candidate(entry_id, source, len(converted) + 1, _number(row.get("score")), dict(row), {source})
                )
        return converted

    def _hydrate(self, candidates, stages, order, reasons):
        order.append("hydration")
        if not candidates:
            stages["hydration"] = {
                "occurred": False,
                "degraded": False,
                "input_count": 0,
                "hydrated_count": 0,
                "missing_count": 0,
            }
            return []
        if self.l1 is None:
            reason = "no_l1_adapter"
            reasons.append(reason)
            stages["hydration"] = {
                "occurred": False,
                "degraded": True,
                "input_count": len(candidates),
                "hydrated_count": 0,
                "missing_count": len(candidates),
                "reason": reason,
            }
            return []
        entry_ids = [candidate.entry_id for candidate in candidates]
        try:
            method = getattr(self.l1, "hydrate", None)
            if method is None:
                method = getattr(self.l1, "get", None)
            if method is None:
                raise TypeError("l1 has no hydrate operation")
            try:
                raw = method(entry_ids, candidates=candidates)
            except TypeError:
                raw = method(entry_ids)
            if isinstance(raw, Mapping):
                hydrated = raw
            else:
                hydrated = {
                    _identity(row): row
                    for row in (raw or [])
                    if isinstance(row, Mapping) and _identity(row) is not None
                }
            result = []
            missing = 0
            for candidate in candidates:
                row = hydrated.get(candidate.entry_id)
                if not isinstance(row, Mapping):
                    missing += 1
                    continue
                item = dict(row)
                item["entry_id"] = candidate.entry_id
                item["provenance"] = sorted(candidate.provenance)
                item["source_ranks"] = dict(sorted(candidate.source_ranks.items()))
                item["_candidate"] = candidate
                result.append(item)
            if missing:
                reasons.append("l1_identity_missing")
            stages["hydration"] = {
                "occurred": True,
                "degraded": bool(missing),
                "input_count": len(candidates),
                "hydrated_count": len(result),
                "missing_count": missing,
            }
            return result
        except Exception as exc:  # noqa: BLE001 - hydration is a soft read failure
            reason = _safe_reason("l1", exc)
            reasons.append(reason)
            stages["hydration"] = {
                "occurred": True,
                "degraded": True,
                "input_count": len(candidates),
                "hydrated_count": 0,
                "missing_count": len(candidates),
                "reason": reason,
            }
            return []

    @staticmethod
    def _fuse(rows, top_n, stages, order):
        order.append("fusion")
        clean = []
        for row in rows:
            candidate = row.pop("_candidate", None)
            if candidate is None:
                continue
            row["score"] = round(candidate.fused_score, 8)
            clean.append(row)
        clean.sort(key=lambda row: (-_number(row.get("score")), str(row.get("entry_id", ""))))
        result = [_serializable(row) for row in clean[:top_n]]
        stages["fusion"] = {
            "occurred": True,
            "degraded": False,
            "input_count": len(rows),
            "result_count": len(result),
        }
        return result


class VectorStoreBm25Channel:
    """Lexical channel over committed vector rows, without an L1 scan."""

    def __init__(self, vector_store: Any):
        self.vector_store = vector_store

    def search(self, query: str, top_n: int = 5, domain: str | None = None) -> list[dict]:
        from .recall import bm25_relevance

        rows = self.vector_store.snapshot()
        documents = [row for row in rows if domain is None or row[1] == domain]
        avg_dl = sum(len(str(row[2]).split()) for row in documents) / len(documents) if documents else 100.0
        ranked = []
        for row in documents:
            score = bm25_relevance(query, str(row[2]), str(row[1]), avg_dl=avg_dl, doc_count=len(documents) or 1)
            if score > 0:
                metadata = json.loads(row[4]) if row[4] else {}
                ranked.append({
                    "entry_id": row[0],
                    "id": row[0],
                    "domain": row[1],
                    "text": row[2],
                    "metadata": metadata,
                    "score": round(score, 8),
                })
        ranked.sort(key=lambda row: (-row["score"], row["entry_id"]))
        return ranked[:top_n]


class L0RouteAdapter:
    """Read-only soft router over the configured L0 index surface."""

    def __init__(self, index_file: Any = None):
        self.index_file = index_file

    def route(self, query: str) -> list[dict[str, Any]]:
        if self.index_file is None:
            return []
        try:
            content = read_knowledge_text(self.index_file)
        except (KnowledgeReadError, OSError):
            return []
        query_tokens = {
            token.casefold()
            for token in self._tokens(str(query))
            if token.strip()
        }
        if not query_tokens:
            return []
        routes = []
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            haystack = line.casefold()
            matched = sum(token in haystack for token in query_tokens)
            if matched:
                routes.append({"line": line, "score": matched / len(query_tokens)})
        routes.sort(key=lambda route: (-route["score"], route["line"]))
        return routes

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Return whitespace words plus adjacent CJK bigrams.

        L0 entries are compact keyword lines rather than prose.  Keeping ASCII
        words intact preserves the original router behavior, while CJK bigrams
        let a query such as ``向量检索`` match an entry that mentions ``向量``
        and ``检索`` without requiring whitespace that Chinese text normally
        does not contain.
        """
        import re

        tokens: list[str] = []
        for part in text.split():
            cjk_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", part)
            ascii_parts = re.findall(r"[^\u3400-\u4dbf\u4e00-\u9fff]+", part)
            tokens.extend(item for item in ascii_parts if item.strip())
            for run in cjk_runs:
                tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        return tokens


class L1StoreHydrator:
    """Hydrate frontmatter entries and legacy multi-section L1 files."""

    def __init__(self, l1_store: Any):
        self.l1_store = l1_store

    @staticmethod
    def _legacy_section(
        raw: str, section: Any
    ) -> tuple[str, str] | None:
        """Return one unambiguous bodyful legacy section, never a guess."""
        if not isinstance(section, str) or not section.strip():
            return None
        from .injector import _bodyful_sections

        matches = [
            (label, body)
            for label, body in _bodyful_sections(raw)
            if label == section
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    @staticmethod
    def _candidate_section(candidate: Any) -> Any:
        payload = getattr(candidate, "payload", {})
        if not isinstance(payload, Mapping):
            return None
        section = payload.get("section")
        if section is not None:
            return section
        metadata = payload.get("metadata")
        return metadata.get("section") if isinstance(metadata, Mapping) else None

    def hydrate(
        self, entry_ids: Sequence[str], candidates: Sequence[_Candidate] | None = None
    ) -> dict[str, dict[str, Any]]:
        hydrated: dict[str, dict[str, Any]] = {}
        candidate_by_id = {
            candidate.entry_id: candidate for candidate in candidates or ()
        }
        for entry_id in entry_ids:
            candidate = candidate_by_id.get(entry_id)
            payload = candidate.payload if candidate is not None else {}
            domain = payload.get("domain") if isinstance(payload, Mapping) else None
            if isinstance(domain, str) and domain:
                meta, content = self.l1_store.read(domain)
                if isinstance(meta, Mapping) and meta.get("id") == entry_id:
                    hydrated[entry_id] = {
                        **dict(meta),
                        "id": entry_id,
                        "domain": domain,
                        "content": content,
                        "text": content,
                    }
                    continue
                section = self._candidate_section(candidate)
                legacy = self._legacy_section(content, section)
                if legacy is not None:
                    label, body = legacy
                    hydrated[entry_id] = {
                        "id": entry_id,
                        "entry_id": entry_id,
                        "domain": domain,
                        "section": label,
                        "content": body,
                        "text": body,
                    }
                    continue
            if hasattr(self.l1_store, "get_by_id"):
                stored = self.l1_store.get_by_id(entry_id)
                if stored is not None:
                    hydrated[entry_id] = _serializable(stored)
        return hydrated


class GraphSearchAdapter:
    """Adapter around the existing graph recall policy."""

    def __init__(self, config: Any, store: Any = None):
        self.config = config
        self.store = store

    def filter_and_expand(self, candidates, query=None, route=None):
        from .graph_search import apply_graph_recall

        outcome = apply_graph_recall(
            self.config, candidates, identity_key="id", store=self.store
        )
        return {
            "candidates": outcome.results,
            "expanded": [],
            "reason": None,
        }


def build_unified_engine(config: Any, *, vector_store: Any, l1_store: Any, graph_store: Any = None) -> UnifiedRecallEngine:
    """Build the shared engine for MCP and REST opt-in calls."""

    return UnifiedRecallEngine(
        vector=vector_store,
        bm25=VectorStoreBm25Channel(vector_store),
        router=L0RouteAdapter(getattr(config, "l0_index_file", None)),
        graph=GraphSearchAdapter(config, graph_store),
        l1=L1StoreHydrator(l1_store),
    )


__all__ = [
    "GraphSearchAdapter",
    "L0RouteAdapter",
    "L1StoreHydrator",
    "UnifiedRecallEngine",
    "VectorStoreBm25Channel",
    "build_unified_engine",
]

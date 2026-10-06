"""
Knowledge Injector — Smart write engine for L1 knowledge files.

Provides high-level write operations that handle:
  - Deduplication check before writing
  - Section-level targeting (find or create ## headings)
  - Multiple write modes: upsert / append / merge
  - Automatic L0 index sync after successful writes
  - File size warnings and content validation
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

from filelock import FileLock

from .heading import heading_skeleton
from .recall import find_similar_knowledge, invalidate_scan_cache, knowledge_health, scan_knowledge_files

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.injector")

# Section heading pattern (H1-H6, unified with recall.py)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Max recommended file size (4KB)
MAX_RECOMMENDED_SIZE = 4096

# v2.8.0: in append mode, skip writing content whose similarity to an existing
# entry is at/above this threshold (near-verbatim duplicate). Set high so that
# legitimately appending a distinct-but-related note still works; only nearly
# identical content is refused. This plugs the silent-bloat hole.
APPEND_DEDUP_SKIP_THRESHOLD = 0.98

# ---------------------------------------------------------------------------
# v3.1.0: Semantic upsert thresholds (cosine over bge-small-zh-v1.5)
# ---------------------------------------------------------------------------
# Calibrated against real Chinese knowledge duplicate-pairs:
#   - true duplicate / same-topic reword  → 0.74 .. 0.84
#   - unrelated content                    → 0.44 .. 0.52
#   - safe gap band                        → 0.55 .. 0.72
# These are the deterministic decision boundaries for the write path. They are
# overridable per-install via config / env (see MemoryConfig.sem_*_threshold);
# the constants below are the shipped defaults.
SEM_SKIP_THRESHOLD = 0.95    # >= → near-verbatim, no-op (Layer 1 usually caught it)
SEM_FUSE_THRESHOLD = 0.72    # >= → "same knowledge", defer section fusion to caller
SEM_MERGE_THRESHOLD = 0.55   # >= → related, line-dedup merge into target section
# below SEM_MERGE_THRESHOLD → genuinely new → append


# ---------------------------------------------------------------------------
# v3.3.7: same-skeleton families — the write side of the v3.3.2 read detector.
# ---------------------------------------------------------------------------
# A periodic note ("AI项目横评 2026-09-22 期（文档解析…）") has a heading that
# looks unique to a reader and to cosine similarity, but not to
# ``heading_skeleton``. That asymmetry is why the auditor could already report a
# family the writer kept appending to. These helpers give the writer the same
# eyes: when a write would create another member of an existing family, hand the
# whole family back and require it to be rewritten as one section.

_SECTION_SPLIT_RE = re.compile(r"(?m)^(##[^\n]*)$")


def _mode_note(mode_deprecated: str | None) -> dict:
    """Response fragment for a caller that asked for the retired ``append``.

    Injected into every response shape (deferred/skipped/consolidated/plain) so
    a stale caller can be found and fixed instead of silently getting different
    semantics than it asked for.
    """
    if not mode_deprecated:
        return {}
    return {
        "mode_deprecated": mode_deprecated,
        "mode_note": (
            f"mode={mode_deprecated!r} is no longer honoured (it appended without "
            "merging, which is how periodic families piled up); it behaved as "
            "'upsert' for this write."
        ),
    }


def _split_raw(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a domain file into (preamble, [(heading_line, body), ...]).

    Byte-faithful: ``preamble + "".join(h + b for h, b in parts)`` restores the
    input exactly, so a caller can rebuild the file changing only what it means
    to change.
    """
    parts = _SECTION_SPLIT_RE.split(raw)
    preamble = parts[0]
    sections: list[tuple[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i], parts[i + 1]))
    return preamble, sections


def _heading_text(heading_line: str) -> str:
    """``"## Foo"`` → ``"Foo"``."""
    return heading_line[2:].strip()


def _heading_family(raw: str, section: str) -> list[tuple[str, str]]:
    """Existing (heading_line, body) pairs whose skeleton matches ``section``."""
    skeleton = heading_skeleton(section)
    if not skeleton:
        return []
    _preamble, sections = _split_raw(raw)
    return [
        (h, b) for h, b in sections if heading_skeleton(_heading_text(h)) == skeleton
    ]


def _family_hash(family: list[tuple[str, str]]) -> str:
    """Optimistic-lock token for a whole family (cf. ``_hash_body`` per section)."""
    return _hash_body("\n".join(h + b for h, b in family))


def _family_gate(config, filepath: Path, section: str, content: str) -> dict | None:
    """Return a ``deferred_consolidate`` response, or None to write normally.

    Fires when the target file already holds a section with the same heading
    *skeleton* and this write would add another member (``consolidate_min_family``
    members in total). A write whose heading matches a member exactly is an
    ordinary update — the family does not grow, so it is not gated.
    """
    if not bool(getattr(config, "consolidate_enabled", False)):
        return None
    try:
        if not filepath.exists():
            return None
        raw = filepath.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw:
        return None

    family = _heading_family(raw, section)
    if not family:
        return None
    if any(_heading_text(h) == section for h, _b in family):
        return None  # exact-heading update, not a new member

    min_family = int(getattr(config, "consolidate_min_family", 2) or 2)
    if len(family) < max(1, min_family - 1):
        return None

    # Visible on purpose: an unattended caller that ignores the action would
    # otherwise look like a silent no-op write.
    logger.warning(
        "deferred_consolidate: %s already holds %d section(s) with skeleton %r; "
        "refusing to add another",
        section, len(family), heading_skeleton(section),
    )

    return {
        "success": True,
        "action": "deferred_consolidate",
        "domain": None,  # filled in by the caller
        "section": section,
        "family_size": len(family),
        "would_be_family_size": len(family) + 1,
        "family": [{"heading": _heading_text(h), "body": b.strip()} for h, b in family],
        "new_content": content.strip(),
        "expected_hash": _family_hash(family),
        "hint": (
            f"This file already holds {len(family)} section(s) on the same topic "
            f"(same heading skeleton after dropping dates/issue numbers), and this "
            f"write would add another. A memory file is a snapshot of the current "
            f"understanding, not a change log: rewrite the whole family into ONE "
            f"section — the current understanding plus a one-line provenance note "
            f"(e.g. '出处：09-22/09-26 两期实测') — then re-submit that text with "
            f"fuse=True and the expected_hash below. Once the family holds two or "
            f"more members the framework also refuses a write-back that fails to "
            f"shrink it by at least "
            f"{100 - int(float(getattr(config, 'consolidate_size_ceiling', 0.9) or 0.9) * 100)}% "
            f"(a re-statement in a new shape is not consolidation)."
        ),
        "l0_synced": False,
    }


def _consolidation_writeback(
    config, filepath: Path, section: str, expected_hash: str | None, content: str
) -> dict | None:
    """Validate a consolidation write-back (second half of the handshake).

    Distinguishes a consolidation from a plain fusion by the hash: the
    ``deferred_consolidate`` response carries the hash of the whole *family*,
    ``deferred_fusion`` the hash of one section body. Returns ``{"ok": True}``,
    ``{"refused": <response>}`` or None when this is not a consolidation.
    """
    if not bool(getattr(config, "consolidate_enabled", False)):
        return None
    if not expected_hash:
        return None
    try:
        if not filepath.exists():
            return None
        raw = filepath.read_text(encoding="utf-8")
    except OSError:
        return None

    family = _heading_family(raw, section)
    if not family or _family_hash(family) != expected_hash:
        return None

    total_old = sum(len(b) for _h, b in family)
    new_len = len(content.strip())
    ceiling = float(getattr(config, "consolidate_size_ceiling", 0.9) or 0)
    # The ceiling compares the write-back against the sections it REPLACES, which
    # is meaningful once several have piled up. With a single existing member the
    # incoming note is itself part of what must be preserved, so demanding
    # shrinkage would refuse every first consolidation and deadlock the gate
    # (measured on the live service: a 22-character member cannot host a merged
    # body in under 0.9 × 22 characters). Anti-laziness therefore starts at two
    # existing members; a single member only has to come back as ONE section.
    if len(family) >= 2 and ceiling and total_old and new_len >= total_old * ceiling:
        return {
            "refused": {
                "success": False,
                "action": "consolidate_refused",
                "domain": None,
                "section": section,
                "family_size": len(family),
                "family_bytes": total_old,
                "submitted_bytes": new_len,
                "ratio": round(new_len / total_old, 3),
                "ceiling": ceiling,
                "reason": (
                    "The write-back is not smaller than the family it replaces "
                    f"({new_len} vs {total_old} bytes, ratio "
                    f"{new_len / total_old:.3f} ≥ ceiling {ceiling}). This is the "
                    "anti-laziness check: re-stating the old sections in a new "
                    "shape is not consolidation. Rewrite the family as the current "
                    "understanding plus a provenance line, then re-submit."
                ),
                "l0_synced": False,
            }
        }
    return {"ok": True, "family_size": len(family)}


def _write_consolidated(
    filepath: Path, raw: str, section: str, content: str, provenance: str
) -> dict:
    """Collapse every same-skeleton section into one, in the first member's slot.

    Called under the file lock, after ``_do_write`` has already written ``.bak``.
    """
    skeleton = heading_skeleton(section)
    preamble, sections = _split_raw(raw)
    idxs = [
        i
        for i, (h, _b) in enumerate(sections)
        if heading_skeleton(_heading_text(h)) == skeleton
    ]
    if not idxs:
        return {
            "success": False,
            "write_action": "consolidate_conflict",
            "error": "no sections matched the heading skeleton at write time",
        }

    first = idxs[0]
    kept: list[str] = []
    removed = 0
    for i, (h, b) in enumerate(sections):
        if i == first:
            kept.append(f"## {section}\n\n{content}{provenance}\n")
        elif i in idxs:
            removed += 1
            continue
        else:
            kept.append(h + b)

    new_text = preamble + "".join(kept)
    filepath.write_text(new_text, encoding="utf-8")
    size = len(new_text.encode("utf-8"))
    return {
        "success": True,
        "write_action": "consolidated",
        "family_size_before": len(idxs),
        "sections_removed": removed,
        "bytes_written": size,
        "file_size_bytes": size,
    }

# v3.1.1: short-text semantic guard. bge-small-zh cosine is unreliable on very
# short, structured English facts: "PostgreSQL on 5432" vs "Redis on 6379" lands
# at cos≈0.80 (a false "same knowledge"). Below this character length the
# semantic layer is SKIPPED entirely — the write falls through to exact (Layer 1)
# + fuzzy (Layer 3), which are precise on short strings. This is the temporary
# guardrail called for by the "content-is-king" decision: we no longer gate on
# whether the section TITLE matches (that produced the reword-miss bug); we gate
# only on text length, where the embedding model is genuinely untrustworthy.
# The proper fix is per-corpus threshold self-calibration (see calibrate_*).
SEM_SHORT_TEXT_CHARS = 30


def _hash_body(body: str) -> str:
    """Stable content hash of a section body for the fuse optimistic lock.

    Normalizes line endings and strips trailing whitespace so cosmetic churn
    (a trailing newline added by a reader) doesn't spuriously invalidate a
    pending deferred-fusion handshake. Returns a short hex digest.
    """
    norm = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _semantic_thresholds(config) -> tuple[float, float, float]:
    """Resolve (skip, fuse, merge) thresholds.

    Priority (highest first):
      1. Per-corpus SELF-CALIBRATED thresholds cached at
         ``<home>/data/sem_thresholds.json`` (written by ``calibrate_thresholds``
         during reindex). These adapt the cosine bands to the actual embedding
         distribution of THIS knowledge base / language / model.
      2. Explicit config overrides (``config.sem_*_threshold``).
      3. Shipped defaults (calibrated only for bge-small-zh + Chinese technical
         knowledge — see the module docstring).

    Falls back through the chain so the boundaries are always defined, and the
    write path never depends on calibration having run.
    """
    skip = getattr(config, "sem_skip_threshold", None) or SEM_SKIP_THRESHOLD
    fuse = getattr(config, "sem_fuse_threshold", None) or SEM_FUSE_THRESHOLD
    merge = getattr(config, "sem_merge_threshold", None) or SEM_MERGE_THRESHOLD

    # Layer 1: self-calibrated cache wins when present and the config carries no
    # explicit override (an explicit override is a deliberate user choice).
    try:
        cal = _load_calibrated_thresholds(config)
        if cal:
            if getattr(config, "sem_fuse_threshold", None) is None and cal.get("fuse"):
                fuse = cal["fuse"]
            if getattr(config, "sem_merge_threshold", None) is None and cal.get("merge"):
                merge = cal["merge"]
            if getattr(config, "sem_skip_threshold", None) is None and cal.get("skip"):
                skip = cal["skip"]
    except Exception as e:  # noqa: BLE001 — calibration is advisory, never fatal
        logger.debug("calibrated threshold load failed: %s", e)

    return float(skip), float(fuse), float(merge)


def _thresholds_source(config) -> str:
    """Return 'calibrated' or 'default' for the currently-effective fuse band.

    Used to stamp write responses so callers can see whether the magic numbers
    were adapted to their corpus or are the shipped bge/Chinese defaults.
    """
    if getattr(config, "sem_fuse_threshold", None) is not None:
        return "config_override"
    try:
        cal = _load_calibrated_thresholds(config)
        if cal and cal.get("fuse"):
            return "calibrated"
    except Exception:
        pass
    return "default"


# ---------------------------------------------------------------------------
# v3.1.1: Threshold self-calibration.
#
# The shipped 0.72/0.55 bands were hand-tuned for bge-small-zh + Chinese
# technical prose. Swap the language, domain, or embedding model and they drift
# — a magic number masquerading as a determined boundary. Calibration derives
# the bands from THIS corpus: embed every section, take the distribution of
# pairwise cosines, and place the fuse band at the "valley" (histogram trough)
# that separates the tail of true-duplicate/same-topic pairs from the bulk of
# unrelated pairs. Merge sits a fixed step below fuse. If the corpus is too
# small to find a stable valley, we DO NOT invent one — we fall back to the
# shipped defaults and say so.
# ---------------------------------------------------------------------------

_CALIBRATION_FILE = "sem_thresholds.json"
# Below this many sections there aren't enough pairwise samples for the valley
# to be anything but noise → refuse to calibrate, keep defaults.
_CALIBRATION_MIN_SECTIONS = 25


def _calibration_path(config) -> Path:
    return config.home / "data" / _CALIBRATION_FILE


def _load_calibrated_thresholds(config) -> dict | None:
    """Load cached self-calibrated thresholds, or None if absent/unusable."""
    try:
        p = _calibration_path(config)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # Only trust an entry that actually calibrated (not a recorded fallback).
        if not data.get("calibrated"):
            return None
        return data
    except Exception:
        return None


def _find_distribution_valley(cosines: list[float]) -> float | None:
    """Find the histogram trough in a cosine-similarity distribution.

    Returns the cosine value at the lowest-density bin that sits BETWEEN the
    main unrelated-pair mass (low cosines) and the high-similarity duplicate
    tail — the natural boundary for "same knowledge". Returns None when no
    clear bimodal valley exists (unimodal / too sparse), so the caller can
    fall back to defaults rather than trust a noisy pick.
    """
    import numpy as np

    if len(cosines) < 30:
        return None
    arr = np.asarray(cosines, dtype=np.float64)
    # Focus on the decision-relevant range; almost all unrelated bge pairs sit
    # in 0.3–0.6 and dup pairs 0.7+. A valley outside [0.45, 0.9] isn't a
    # fuse/merge boundary.
    lo, hi = 0.40, 0.92
    bins = np.linspace(lo, hi, 25)
    hist, edges = np.histogram(arr, bins=bins)
    if hist.sum() < 20:
        return None
    # Need mass on BOTH sides to call it bimodal — otherwise there's no valley,
    # just a single hump (don't fabricate a boundary).
    mid = len(hist) // 2
    left_mass = hist[:mid].sum()
    right_mass = hist[mid:].sum()
    if left_mass < 5 or right_mass < 5:
        return None
    # The valley = the global-minimum bin in the interior (ignore the extreme
    # edges, which are trivially empty). Search bins [3 .. n-3].
    interior = hist[3:-3]
    if len(interior) == 0:
        return None
    vidx = int(np.argmin(interior)) + 3
    valley_center = float((edges[vidx] + edges[vidx + 1]) / 2.0)
    # Sanity: the valley must genuinely be a trough (lower than both flanks'
    # average), else the distribution is basically flat → not reliable.
    flank = (float(hist[:vidx].mean()) + float(hist[vidx + 1:].mean())) / 2.0
    if flank <= 0 or float(hist[vidx]) >= flank * 0.85:
        return None
    return round(valley_center, 4)


def calibrate_thresholds(config, persist: bool = True) -> dict:
    """Derive fuse/merge thresholds from the current corpus's cosine distribution.

    Reads every section vector from the store, computes the pairwise cosine
    distribution, finds the bimodal valley (see ``_find_distribution_valley``),
    and sets:
        fuse  = valley
        merge = max(valley - 0.17, 0.40)   # a fixed step below fuse
        skip  = shipped default (near-verbatim is model-agnostic enough)
    Persists the result to ``<home>/data/sem_thresholds.json`` when ``persist``.

    Returns a report dict. When the corpus is too small or unimodal, returns
    ``calibrated=False`` with ``fallback`` defaults and a human-readable reason —
    it never invents a boundary from insufficient data.
    """
    import numpy as np
    from .storage.vector_store import EMBED_DIM
    import sqlite3 as _sqlite3

    skip_def, fuse_def, merge_def = (
        SEM_SKIP_THRESHOLD, SEM_FUSE_THRESHOLD, SEM_MERGE_THRESHOLD
    )
    report: dict = {
        "success": True,
        "calibrated": False,
        "fuse": fuse_def,
        "merge": merge_def,
        "skip": skip_def,
        "source": "default",
        "note": (
            "Using shipped defaults (calibrated only for bge-small-zh + Chinese "
            "technical knowledge). Provide a larger/more varied corpus and "
            "reindex to self-calibrate."
        ),
    }
    try:
        db_path = config.home / "data" / "vectors.db"
        if not db_path.exists():
            report["reason"] = "no_vector_store"
            return report
        with _sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT vector FROM vectors").fetchall()
        vectors = []
        for (vec_json,) in rows:
            try:
                v = np.array(json.loads(vec_json), dtype=np.float32)
            except Exception:
                continue
            if v.shape[0] == EMBED_DIM:
                vectors.append(v)
        n = len(vectors)
        report["sections"] = n
        if n < _CALIBRATION_MIN_SECTIONS:
            report["reason"] = (
                f"too_few_sections ({n} < {_CALIBRATION_MIN_SECTIONS}); "
                "keeping defaults"
            )
            if persist:
                _persist_calibration(config, report)
            return report

        matrix = np.vstack(vectors)
        sims = matrix @ matrix.T
        iu = np.triu_indices(n, k=1)
        cosines = sims[iu].astype(float).tolist()

        valley = _find_distribution_valley(cosines)
        if valley is None:
            report["reason"] = "no_bimodal_valley; keeping defaults"
            if persist:
                _persist_calibration(config, report)
            return report

        fuse = float(np.clip(valley, 0.60, 0.90))
        merge = float(max(fuse - 0.17, 0.40))
        report.update({
            "calibrated": True,
            "fuse": round(fuse, 4),
            "merge": round(merge, 4),
            "skip": skip_def,
            "source": "calibrated",
            "valley": valley,
            "pairs": len(cosines),
            "note": (
                f"Self-calibrated from {n} sections ({len(cosines)} pairs): "
                f"fuse={round(fuse, 4)}, merge={round(merge, 4)} at the "
                f"distribution valley cos={valley}."
            ),
        })
        if persist:
            _persist_calibration(config, report)
        return report
    except Exception as e:  # noqa: BLE001 — calibration must never break reindex
        logger.warning("calibrate_thresholds failed (non-critical): %s", e)
        report["success"] = False
        report["error"] = str(e)
        return report


def _persist_calibration(config, report: dict) -> None:
    try:
        p = _calibration_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "calibrated": bool(report.get("calibrated")),
            "fuse": report.get("fuse"),
            "merge": report.get("merge"),
            "skip": report.get("skip"),
            "valley": report.get("valley"),
            "sections": report.get("sections"),
            "note": report.get("note"),
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not persist calibration: %s", e)


def inject_knowledge(
    config: "MemoryConfig",
    domain: str,
    section: str,
    content: str,
    mode: str = "upsert",
    agent_id: str | None = None,
    fuse: bool = False,
    expected_hash: str | None = None,
) -> dict:
    """Smart knowledge injection — the primary write entry point.

    Args:
        config: MemoryConfig instance.
        domain: Target L1 file (with or without .md), e.g. "infra" or "infra.md".
        section: Target ## heading in the file, e.g. "WSL 代理".
                 If the section doesn't exist, it will be created.
        content: Knowledge content to inject (markdown text).
        mode: "upsert" (replace if similar exists), "append" (always add),
              or "merge" (combine new + existing unique parts).
        agent_id: Optional agent identifier for provenance tracking.
        fuse: v3.1.0 fusion write-back flag. When True, the caller (an LLM
              agent) has already fused the old section body with new content
              and is committing the final text; the semantic upsert deferral is
              bypassed and ``content`` wholesale-replaces the target section.
              This is the second half of the deferred-fusion handshake and MUST
              be paired with an explicit ``section`` (the hit section returned
              by the previous ``deferred_fusion`` response). No-op for append
              mode (append always adds, never fuses).
        expected_hash: v3.1.1 optimistic-lock token. The ``expected_hash`` echoed
              from the prior ``deferred_fusion`` response. On ``fuse=True`` the
              framework recomputes the target section's current body hash and
              refuses the write (``action="fuse_conflict"``) if it differs,
              preventing a lost update when the section changed between the two
              handshake calls.

    Returns:
        Result dict with action taken, dedup info, L0 sync status. When a
        semantic near-duplicate is detected in upsert mode (cosine ≥ fuse
        threshold), returns ``action="deferred_fusion"`` carrying the old
        section body + new content (and an ``expected_hash``) for the caller to
        fuse and re-submit with ``fuse=True`` + that ``expected_hash``.
    """
    # --- 1. Validate inputs ---
    if not content or not content.strip():
        return {"success": False, "error": "Content is empty"}

    # Normalize domain to filename
    filename = domain if domain.endswith(".md") else f"{domain}.md"
    section_clean = section.strip().lstrip("#").strip()
    if not section_clean:
        return {"success": False, "error": "Section heading cannot be empty"}

    # Security: validate path to prevent traversal attacks
    # Filename must not contain path separators or parent references
    if "/" in filename or "\\" in filename or ".." in filename:
        return {"success": False, "error": f"Invalid filename (path traversal blocked): {filename}"}

    # Find the actual file location across all knowledge dirs (namespace + shared).
    # If the file exists in a shared dir, write there instead of creating a duplicate.
    filepath = None
    for kdir in config.knowledge_dirs:
        candidate = kdir / filename
        try:
            candidate.resolve().relative_to(kdir.resolve())
        except ValueError:
            continue
        if candidate.exists():
            filepath = candidate
            break

    # File doesn't exist yet — default to namespace dir
    if filepath is None:
        filepath = config.knowledge_dir / filename
        try:
            filepath.resolve().relative_to(config.knowledge_dir.resolve())
        except ValueError:
            return {"success": False, "error": f"Path traversal blocked: {filename}"}

    # v3.3.7: append is no longer honoured. Appending each periodic entry is how
    # this store accumulated four-issue families that no cosine threshold can
    # see; "append" now behaves as upsert (and the response says so), so the
    # family gate below covers every write path.
    mode_deprecated: str | None = None
    if mode == "append":
        mode = "upsert"
        mode_deprecated = "append"

    # v3.3.7: same-skeleton family gate. It runs before the dedup layers because
    # cosine is blind to this by construction — the family's members resemble
    # each other, but it is the *heading* that makes them one topic, and each
    # heading carries a fresh date.
    if not fuse:
        family_gate = _family_gate(config, filepath, section_clean, content)
        if family_gate is not None:
            family_gate["domain"] = filename.removesuffix(".md")
            family_gate["file"] = filename
            if mode_deprecated:
                family_gate["mode_deprecated"] = mode_deprecated
            return family_gate

    # --- 2. Dedup check (scan across all knowledge dirs: namespace + shared) ---
    kdirs = [str(d) for d in config.knowledge_dirs]
    knowledge_arg = kdirs if len(kdirs) > 1 else kdirs[0]
    domain_clean = filename.removesuffix(".md")
    data_dir = config.home / "data"

    # v3.1.0: fuse write-back path. The caller already did the semantic fusion
    # (deferred_fusion handshake) and is committing the final section body.
    # Bypass semantic deferral entirely and wholesale-replace the target
    # section. Exact Layer-1 dedup is intentionally skipped here — the fused
    # body legitimately contains the old text plus new material, which the
    # exact guard would otherwise read as a duplicate.
    #
    # v3.1.1: OPTIMISTIC LOCK. The deferred_fusion handshake is two independent
    # MCP calls with no transaction between them. If another write mutated the
    # hit section after we handed back old_body but before this fuse commit, a
    # blind replace would silently clobber that intervening write (lost update).
    # The deferred response now carries expected_hash = sha256(old_body); on
    # fuse=True the caller MUST echo it back. We recompute the CURRENT section
    # body's hash and refuse the write on mismatch, forcing a re-defer. This
    # also closes the "fuse=True is a Layer-1-dedup bypass backdoor" concern:
    # you can only replace a section whose content is exactly what you were
    # shown, so the bypass can't be aimed at arbitrary sections.
    family_wb = (
        _consolidation_writeback(config, filepath, section_clean, expected_hash, content)
        if (fuse and mode != "append")
        else None
    )
    if family_wb and family_wb.get("refused"):
        refused = family_wb["refused"]
        refused["domain"] = filename.removesuffix(".md")
        refused["file"] = filename
        return refused

    if family_wb and family_wb.get("ok"):
        # Collapsing a family: the family hash already matched, so there is no
        # section-level lock to verify, and the write below rewrites every member.
        dedup_result = {
            "similar_found": True,
            "similarity": 1.0,
            "matched_file": filename,
            "suggestion": "replace",
            "match_kind": "consolidation_writeback",
        }
        effective_action = "consolidated"
    elif fuse and mode != "append":
        lock_check = _verify_fuse_precondition(
            filepath, section_clean, expected_hash
        )
        if not lock_check["ok"]:
            return {
                "success": False,
                "action": "fuse_conflict",
                "error": lock_check["error"],
                "file": filename,
                "domain": domain_clean,
                "section": f"## {section_clean}",
                "expected_hash": expected_hash,
                "current_hash": lock_check.get("current_hash"),
                "reason": (
                    "The target section changed since the deferred_fusion "
                    "response was issued (or no expected_hash was supplied). "
                    "Refusing to overwrite to avoid a lost update. Re-call "
                    "inject_knowledge WITHOUT fuse to obtain a fresh "
                    "deferred_fusion (new old_body + expected_hash), fuse "
                    "again, and re-submit."
                ),
                "l0_synced": False,
            }
        dedup_result = {
            "similar_found": True,
            "similarity": 1.0,
            "matched_file": filename,
            "suggestion": "replace",
            "match_kind": "fusion_writeback",
        }
        effective_action = "replaced"
    else:
        dedup_result = _check_dedup(
            content,
            knowledge_arg,
            config.dedup_threshold,
            data_dir=data_dir,
            domain=domain_clean,
            config=config,
            new_section=section_clean,
        )

        # --- 3. Resolve action based on mode + dedup ---
        effective_action = _resolve_action(mode, dedup_result)

        # v3.1.1: CONTENT IS KING. The prior "explicit section-targeting gate"
        # (downgrade deferred_fusion → passive hint when the caller's new
        # section title != the semantically-hit section title) has been REMOVED.
        # Rationale (user decision): the same knowledge is re-extracted under
        # different headings over time ("框架总览" → "框架补充" → "框架说明"); gating
        # on title equality let every retitled duplicate slip through = the
        # original append-bloat bug reincarnated. Semantic similarity ≥ fuse
        # threshold now triggers fusion REGARDLESS of whether the titles match;
        # the fusion is written into the HIT section, not the caller's new one.
        # The short-English false-positive that the title gate was patching over
        # ("PG:5432" vs "Redis:6379" cos≈0.80) is handled at its real root in
        # _semantic_dedup: very short texts skip the semantic layer entirely
        # (SEM_SHORT_TEXT_CHARS) rather than being rescued by a title check.

    # v3.1.0: deferred fusion — a same-knowledge hit (cosine ≥ fuse threshold)
    # in upsert mode. The framework has no LLM and MUST NOT fuse two prose
    # bodies itself; it hands the raw materials back so the calling agent (which
    # IS an LLM) fuses them and re-submits with fuse=True. Nothing is written
    # now — no transient bloat, no lossy machine merge.
    if effective_action == "deferred_fusion":
        sem = dedup_result.get("semantic", {}) or {}
        old_body = sem.get("old_body", "")
        return {
            "success": True,
            "action": "deferred_fusion",
            "needs_fusion": True,
            "file": filename,
            "domain": domain_clean,
            "section": f"## {sem.get('hit_section', section_clean)}",
            # v3.1.1: optimistic-lock token. Echo this back as expected_hash on
            # the fuse=True re-call; the framework refuses the write if the
            # section changed in between (lost-update guard).
            "expected_hash": _hash_body(old_body),
            "reason": (
                "Semantic near-duplicate detected: this looks like an UPDATE to "
                "existing knowledge, not a new fact. The framework does not fuse "
                "prose itself (zero-LLM core). Fuse old_body + new_content into "
                "one lossless body, then re-call inject_knowledge with the same "
                "domain, section=hit_section, content=<fused>, mode='upsert', "
                "fuse=True, expected_hash=<the expected_hash above>."
            ),
            "fusion": {
                "hit_section": sem.get("hit_section"),
                "hit_domain": sem.get("hit_domain", domain_clean),
                "cosine": sem.get("cosine"),
                "old_body": old_body,
                "new_content": content.strip(),
                "expected_hash": _hash_body(old_body),
            },
            "thresholds_source": _thresholds_source(config),
            "dedup": dedup_result,
            "l0_synced": False,
            **_mode_note(mode_deprecated),
        }

    if effective_action == "skipped":
        return {
            "success": True,
            "action": "skipped",
            "file": filename,
            "section": f"## {section_clean}",
            "reason": "Content already exists (high similarity)",
            "dedup": dedup_result,
            "l0_synced": False,
        }

    # For a fusion write-back the caller passes the exact hit section heading;
    # honor it verbatim so we replace the right block.

    # --- 4. Execute write (with file lock) ---
    write_result = _execute_write(
        filepath=filepath,
        filename=filename,
        section=section_clean,
        content=content.strip(),
        action=effective_action,
        agent_id=agent_id,
        config=config,
    )

    if not write_result.get("success"):
        return write_result

    # Invalidate scan cache across ALL knowledge dirs so subsequent recalls see fresh file listing
    for kdir in config.knowledge_dirs:
        invalidate_scan_cache(str(kdir))

    # --- 5. L0 auto-sync ---
    from .l0_manager import auto_sync_if_enabled
    sync_report = auto_sync_if_enabled(config)

    # --- 6. Build result ---
    write_action = write_result.get("write_action", effective_action)

    # --- 6b. Vector store sync (v2.2.0 → v3.1.0) ---
    # v3.1.0: rebuild the whole domain per-section (replace_domain=True) instead
    # of appending one coarse whole-content vector. This keeps vectors.db a
    # faithful section-level mirror of the file — which is exactly what the new
    # semantic dedup recall reads back. The old default-mode add() left a growing
    # pile of coarse per-write vectors that diluted section-level recall.
    try:
        current_text = filepath.read_text(encoding="utf-8")
    except OSError:
        current_text = None
    if current_text is not None:
        vector_result = sync_to_vector_store(
            data_dir=data_dir,
            domain=domain_clean,
            content=current_text,
            summary=_summarize_for_l0(content),
            replace_domain=True,
        )
    else:
        vector_result = sync_to_vector_store(
            data_dir=data_dir,
            domain=domain_clean,
            content=content.strip(),
            summary=_summarize_for_l0(content),
        )

    is_new_file = write_action == "created"

    tag = getattr(config, "l0_tag", "[L0]")
    l0_pointer = f"{tag} {domain_clean}: {_summarize_for_l0(content)} → knowledge/{filename}"

    # Plan B: only prompt agent to write L0 pointer when a NEW L1 file is created.
    # For existing files (append/upsert/merge/replace), L0 auto-sync already
    # covers the update — no manual memory write needed.
    if is_new_file:
        l0_hint = (
            "A NEW knowledge file was created. Write the l0_pointer to your "
            "agent's persistent memory store so future sessions can discover it. "
            f'Example: add to memory: "{l0_pointer}"'
        )
    else:
        l0_hint = "L0 index auto-synced, no action needed."

    result = {
        "success": True,
        "action": write_action,
        "file": filename,
        "section": f"## {section_clean}",
        "bytes_written": write_result.get("bytes_written", 0),
        "file_size_bytes": write_result.get("file_size_bytes", 0),
        "dedup": dedup_result,
        "l0_synced": sync_report is not None,
        "l0_sync_report": sync_report,
        "l0_pointer": l0_pointer,
        "hint": l0_hint,
        "is_new_file": is_new_file,
    }
    # v3.3.7: surface the effect of a consolidation so the caller can report it
    # ("collapsed 4 sections into 1") instead of guessing from the file.
    if write_result.get("sections_removed") is not None:
        result["sections_removed"] = write_result["sections_removed"]
        result["family_size_before"] = write_result.get("family_size_before")

    # v3.1.1: vector-sync visibility. sync_to_vector_store was best-effort and
    # swallowed its own exceptions, so a write that succeeded to the .md file
    # but FAILED to sync its vectors left the file with a section the vector
    # store can't recall — the next dedup call misses it and appends a
    # duplicate (a more insidious leak than the difflib one). Surface the flag
    # so the caller (and tests) can see the divergence, and point at reindex as
    # the deterministic reconciliation path.
    vector_sync_ok = bool(vector_result.get("success"))
    result["vector_sync_ok"] = vector_sync_ok
    if not vector_sync_ok:
        result["vector_sync_error"] = vector_result.get("error")
        result["vector_sync_hint"] = (
            "Vector sync FAILED after the file write succeeded — the semantic "
            "dedup index is now stale for this domain and future writes may not "
            "detect this section as a duplicate. Run reindex_vector_store to "
            "reconcile vectors.db from the markdown files."
        )

    # v3.1.2: WRITE-PATH SLIMMING. The cross-file / shared / cross-namespace
    # duplicate hints that used to be recomputed on EVERY inject (a full global
    # vector scan + filesystem-scope probes) have been REMOVED from the write
    # path. They were "heavy + best-effort": a per-write global search over all
    # namespaces was a performance drag, and it fed the same failure chain the
    # user called out (vector desync → recall miss → drift). Cross-library
    # duplicate detection is now a PULL operation: the user/agent runs the
    # reconcile tool (reconcile_knowledge / list_cross_domain_duplicates), which
    # owns the global view. The write path keeps only two deterministic, cheap
    # ops: exact dedup + same-file semantic fuse decision.
    #
    # v3.1.2: title-diff passive hint (Task 1). When a same-file semantic hit
    # cleared the fuse band but the caller's NEW section title differed and the
    # cosine sat in the 0.72..diff_title band, the content was written as its own
    # section AND this advisory surfaced so the user can consolidate if it truly
    # is the same knowledge. Same-file only — no cross-library scan.
    if dedup_result.get("title_diff_hint"):
        result["title_diff_hint"] = dedup_result["title_diff_hint"]
    # v3.1.0: passive stale-residue prompt. A same-knowledge hit that the caller
    # chose to write as a NEW section (append/merge) instead of fusing may mean
    # an older near-duplicate section is now redundant. Flag it; never delete.
    # (Same-file only — the semantic block comes from in-domain recall.)
    sem = dedup_result.get("semantic") or {}
    if sem.get("hit_section") and write_action in ("appended", "merged", "section_created"):
        result["stale_residue_hint"] = (
            f"Section '## {sem['hit_section']}' in domain '{domain_clean}' is "
            f"highly similar (cos={sem.get('cosine')}) to what you just wrote. "
            "If it is superseded, consider consolidating it — the framework "
            "never deletes knowledge automatically."
        )

    # Size warning
    if write_result.get("file_size_bytes", 0) > MAX_RECOMMENDED_SIZE:
        result["warning"] = (
            f"File size ({write_result['file_size_bytes']} bytes) exceeds "
            f"recommended {MAX_RECOMMENDED_SIZE} bytes. Consider splitting."
        )

    # v2.3.0: Framework self-maintenance (auto-maintain).
    # The layered architecture introduced an L1↔agent-memory dual-write; the
    # framework now owns keeping them consistent and slim, so the agent never
    # has to manually sync L0 pointers or remember to compact. Rides along on
    # this natural write call (stdio-safe, no background thread). Fails silently.
    if getattr(config, "auto_maintain", True):
        try:
            from .memory_compactor import auto_maintain_after_write
            maint = auto_maintain_after_write(
                config,
                l0_pointer=l0_pointer,
                domain=domain_clean,
                filepath=filepath,
            )
            result["auto_maintain"] = maint
            # When the framework completes the dual-write itself, the agent no
            # longer needs the manual "write this pointer to memory" hint.
            dw = (maint or {}).get("dual_write") or {}
            if dw.get("action") in ("added", "replaced", "present"):
                result["hint"] = "L0 pointer auto-written to agent memory by framework."
        except Exception as e:  # noqa: BLE001 — maintenance must not break writes
            # v2.9.3: was a silent `pass`, which hid dual-write/dedup failures
            # and let duplicate L0 pointers accumulate undetected. Log it (and
            # surface a soft flag) while still never breaking the primary write.
            logger.warning("auto_maintain after inject failed (non-critical): %s", e)
            result["auto_maintain_error"] = str(e)
    else:
        # Auto-maintain disabled — fall back to legacy advisory warning so the
        # agent can compact manually.
        try:
            from .memory_compactor import detect_memory_bloat
            bloat = detect_memory_bloat(config=config)
            if bloat.get("success") and bloat.get("total_entries", 0) > 0:
                bloat_pct = bloat["stats"]["bloat_percentage"]
                total_chars = bloat["stats"]["total_chars"]
                if bloat_pct > 80 or total_chars > 3200:
                    result["memory_bloat_warning"] = (
                        f"Agent memory is {bloat_pct}% full ({total_chars} chars). "
                        f"{bloat['bloat_entries']} entry(ies) are not L0 index pointers. "
                        "Run `compact_memory(dry_run=True)` to see what would happen, "
                        "then run without dry_run to auto-migrate."
                    )
        except Exception:
            pass  # Non-critical check, fail silently

    # v3.3.7: tell the caller when we overrode a deprecated mode, so a stale
    # caller can be found and fixed instead of silently getting different
    # semantics than it asked for.
    if isinstance(result, dict):
        result.update(_mode_note(mode_deprecated))

    return result


def append_to_section(
    config,
    filename: str,
    section: str,
    content: str,
    agent_id: str | None = None,
) -> dict:
    """Append content to an existing section in an L1 file.

    Simpler than inject_knowledge — no dedup, no mode selection.
    Just appends to the specified ## section.
    """
    return inject_knowledge(
        config,
        domain=filename,
        section=section,
        content=content,
        mode="append",
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_dedup(
    content: str,
    knowledge_dir: "str | list[str]",
    threshold: float,
    data_dir: "str | Path | None" = None,
    domain: str | None = None,
    config=None,
    new_section: str | None = None,
) -> dict:
    """Run dedup check against existing knowledge.

    v3.1.2 three-layer strategy (write path is now DETERMINISTIC + CHEAP):
      1. EXACT layer (primary): normalized verbatim match across all knowledge
         files. O(total_lines), size-independent, 100% precise, CJK-safe. Plugs
         byte-for-byte append bloat. Reports similarity=1.0 on an exact hit.
      2. SEMANTIC layer: bge-small-zh cosine over the section-level vector store,
         scoped to the TARGET DOMAIN's OWN FILE ONLY (v3.1.2 slimming). Returns
         a suggestion of skip / defer-fusion / merge / append per calibrated
         cosine bands, with a TITLE-GRADED fuse boundary (Task 1): a same-file
         hit whose section title differs must clear a higher cosine to auto-fuse.
         The old per-write GLOBAL cross-namespace / shared scan (heavy +
         best-effort) is GONE — cross-library duplicate detection moved to the
         pull-based reconcile tool.
      3. FUZZY layer (legacy fallback): difflib whole-file SequenceMatcher, used
         ONLY when the vector store is unavailable (model not yet downloaded,
         empty index on cold start). Kept so the write path degrades gracefully
         instead of hard-failing.

    Args:
        content: new content being injected.
        knowledge_dir: dir(s) to scan for the exact + fuzzy fallback layers.
        threshold: legacy fuzzy dedup threshold (config.dedup_threshold).
        data_dir: <home>/data holding vectors.db (enables the semantic layer).
        domain: target domain (filename without .md) — semantic recall is scoped
                to THIS domain's own file only.
        config: MemoryConfig, for threshold overrides.
        new_section: the caller's NEW section heading — used for the title-graded
                fuse decision (Task 1). None → title signal skipped (defer as
                before).
    """
    # --- Layer 1: exact verbatim match (size-independent, CJK-safe) ---
    try:
        exact = _find_exact_duplicate(content, knowledge_dir)
        if exact:
            return {
                "similar_found": True,
                "similarity": 1.0,
                "matched_file": exact,
                "suggestion": "skip",
                "total_similar": 1,
                "match_kind": "exact",
            }
    except Exception as e:
        logger.warning("Exact dedup check failed (non-critical): %s", e)

    # --- Layer 2: semantic recall over the section-level vector store ---
    if data_dir is not None and domain is not None:
        try:
            sem = _semantic_dedup(content, data_dir, domain, config, new_section=new_section)
            if sem is not None:
                return sem
        except Exception as e:
            # Never let the semantic layer break a write — fall through to fuzzy.
            logger.warning("Semantic dedup failed (non-critical), "
                           "falling back to fuzzy: %s", e)

    # --- Layer 3: fuzzy similarity (legacy difflib fallback) ---
    try:
        similar = find_similar_knowledge(content, knowledge_dir, threshold=threshold * 0.8)
    except Exception as e:
        logger.warning("Dedup check failed: %s", e)
        return {"similar_found": False, "similarity": 0.0, "matched_file": None, "suggestion": None}

    if not similar:
        return {"similar_found": False, "similarity": 0.0, "matched_file": None, "suggestion": None}

    best = similar[0]
    return {
        "similar_found": True,
        "similarity": best["similarity"],
        "matched_file": best["file"],
        "suggestion": best["suggestion"],
        "total_similar": len(similar),
        "match_kind": "fuzzy",
    }


def _section_body_from_hit(text: str) -> str:
    """Extract the section body from a vector-store 'text' field.

    Vectors are stored as "section\\nbody" (see sync_to_vector_store._make_entry
    which builds f"{section}\\n{body}"). Recall returns that text (possibly
    truncated to 200 chars). We split off the first line (the section heading
    echo) and return the rest. For the authoritative full body the caller should
    re-read the file; this is the best-effort inline copy.
    """
    if "\n" in text:
        return text.split("\n", 1)[1].strip()
    return text.strip()


def _read_section_body(data_dir, domain: str, section: str) -> str | None:
    """Read the authoritative full body of a ## section from the L1 file.

    The vector store's 'text' is truncated to 200 chars for display; for the
    fusion contract the caller needs the *complete* old body, so we read it
    straight from the markdown file. Returns None if the file/section is gone.
    """
    try:
        # data_dir is <home>/data; the knowledge dir is <home>/knowledge[/ns].
        home = Path(data_dir).parent
        # Try the common layouts: <home>/knowledge/<domain>.md and namespaced.
        candidates = [home / "knowledge" / f"{domain}.md"]
        kroot = home / "knowledge"
        if kroot.exists():
            candidates += list(kroot.glob(f"*/{domain}.md"))
        for fp in candidates:
            if not fp.exists():
                continue
            raw = fp.read_text(encoding="utf-8")
            pos, end = _find_section(raw.replace("\r\n", "\n").replace("\r", "\n"), section)
            if pos is None:
                continue
            block = raw[pos:end]
            # Drop the leading "## section" line; return the body.
            lines = block.split("\n")
            body = "\n".join(lines[1:]).strip()
            return body
    except Exception as e:
        logger.debug("Could not read section body for %s/%s: %s", domain, section, e)
    return None


def _semantic_dedup(
    content: str,
    data_dir,
    domain: str,
    config,
    new_section: str | None = None,
) -> dict | None:
    """Semantic dedup decision via the section-level vector store.

    v3.1.2 SCOPE MODEL — WRITE-PATH SLIMMING (Task 2). Recall is confined to the
    TARGET DOMAIN's OWN FILE. The write action (skip / deferred_fusion / merge /
    append) is decided purely from same-file hits. The v3.1.1 per-write GLOBAL
    cross-file scan (search all namespaces + shared, classify each hit's scope
    via filesystem probes, emit cross_domain_hints / shared_hit_hint /
    cross_namespace_hints) has been REMOVED: it was a heavy, best-effort op run
    on every single write, and it was a link in the failure chain the user
    flagged (a per-write global vector search whose staleness silently skewed
    behavior). Cross-library duplicate detection is now a PULL operation — the
    reconcile tool (promotion.scan_cross_domain_duplicates) owns the global view.
    The write path keeps only deterministic, cheap, same-file work.

    v3.1.2 CONTENT-ONLY FUSION (decision B). Within the fuse band the framework
    does NOT judge whether two similar sections are truly the same knowledge —
    that needs understanding it can't do reliably. Any same-file hit at or above
    the fuse threshold is DEFERRED: both bodies are handed to the calling agent,
    which decides at fusion time whether to merge or keep them separate. No title
    heuristic, no second magic threshold.

    v3.1.1 short-text guard (retained): below ``SEM_SHORT_TEXT_CHARS`` the
    semantic layer abstains (returns None → exact + fuzzy layers, precise on
    short strings). We distrust the MODEL on short text.

    Returns a dedup_result dict, or None to signal "fall back to fuzzy" (empty
    index / model unavailable / short text).
    """
    from .storage.vector_store import VectorStore

    # v3.1.1 short-text guard — abstain rather than risk a false "same knowledge".
    if len((content or "").strip()) < SEM_SHORT_TEXT_CHARS:
        return None

    db_path = Path(data_dir) / "vectors.db"
    if not db_path.exists():
        return None

    store = VectorStore(db_path)
    # Empty index → nothing to compare against; let fuzzy fallback handle it
    # (it will also find nothing on a truly fresh install, i.e. append).
    try:
        if store.stats().get("total_entries", 0) == 0:
            return None
    except Exception:
        return None

    skip_th, fuse_th, merge_th = _semantic_thresholds(config)

    # --- In-domain recall ONLY: drives the write action. No global scan. ---
    in_hits = store.search(content, top_n=3, domain=domain)
    best = in_hits[0] if in_hits else None
    best_cos = float(best["score"]) if best else 0.0

    if best is None:
        # No in-domain vector hit at all → genuinely new (in this domain).
        return {
            "similar_found": False,
            "similarity": 0.0,
            "matched_file": None,
            "suggestion": None,
            "match_kind": "semantic",
        }

    meta = best.get("metadata") or {}
    hit_section = meta.get("section") or ""
    old_body = _read_section_body(data_dir, domain, hit_section)
    if old_body is None:
        old_body = _section_body_from_hit(best.get("text", ""))

    semantic_block = {
        "cosine": round(best_cos, 4),
        "hit_section": hit_section,
        "hit_domain": domain,
        "old_body": old_body,
        "top_hits": [
            {"section": (h.get("metadata") or {}).get("section"),
             "cosine": round(float(h["score"]), 4)}
            for h in in_hits
        ],
    }

    # Deterministic decision by calibrated cosine bands. The framework only
    # judges "how similar is the content" — it does NOT try to decide whether two
    # similar-but-maybe-distinct pieces are truly the same knowledge (that needs
    # understanding, e.g. two sibling config sections that share wording but are
    # genuinely distinct).
    # So a fuse-band hit is DEFERRED to the calling agent, which sees both bodies
    # during fusion and decides whether to actually merge or keep them separate.
    # No title-string heuristic (it misjudges CJK titles that differ by one key
    # character) and no magic second threshold.
    if best_cos >= skip_th:
        suggestion, kind = "skip", "semantic"
    elif best_cos >= fuse_th:
        suggestion, kind = "defer_fusion", "semantic"
    elif best_cos >= merge_th:
        suggestion, kind = "merge", "semantic"
    else:
        suggestion, kind = "append", "semantic"

    result = {
        "similar_found": best_cos >= merge_th,
        "similarity": round(best_cos, 4),
        "matched_file": f"{domain}.md",
        "suggestion": suggestion,
        "total_similar": len(in_hits),
        "match_kind": kind,
        "semantic": semantic_block,
    }
    return result


def _normalize_for_exact(text: str) -> str:
    """Normalize a content block for exact-duplicate comparison.

    Collapses internal whitespace and strips surrounding whitespace so that
    cosmetic differences (trailing spaces, indentation, blank-line padding)
    don't defeat the verbatim match. Intentionally NOT lowercasing or stripping
    markdown — exact means exact in substance.
    """
    return re.sub(r"\s+", " ", text).strip()


def _find_exact_duplicate(content: str, knowledge_dir: str | list[str]) -> str | None:
    """Return the filename containing a verbatim copy of `content`, else None.

    Compares the normalized content block against the normalized full text of
    each knowledge file (substring match). O(sum of file sizes), no quadratic
    SequenceMatcher, no length-ratio pre-filter — so it stays correct even when
    a target file is already large (exactly when append-bloat used to slip
    through).
    """
    norm_content = _normalize_for_exact(content)
    if not norm_content:
        return None

    dirs = knowledge_dir if isinstance(knowledge_dir, list) else [knowledge_dir]
    for kdir in dirs:
        try:
            kpath = Path(kdir)
            if not kpath.exists():
                continue
            for fp in kpath.glob("*.md"):
                try:
                    raw = fp.read_text(encoding="utf-8")
                except OSError:
                    continue
                if norm_content in _normalize_for_exact(raw):
                    return fp.name
        except Exception:
            continue
    return None


def _resolve_action(mode: str, dedup_result: dict) -> str:
    """Determine the effective write action based on mode and dedup result.

    v3.1.0: when the dedup match came from the SEMANTIC layer, the calibrated
    cosine band already decided the intent (skip / defer_fusion / merge /
    append) — this function just maps that suggestion onto the mode contract.
    For the legacy FUZZY fallback (and exact matches) the historical float-
    threshold logic is preserved verbatim, so behaviour is unchanged whenever
    the vector store isn't in play.
    """
    if not dedup_result.get("similar_found"):
        return "created" if mode != "append" else "appended"

    similarity = dedup_result.get("similarity", 0)
    match_kind = dedup_result.get("match_kind")
    is_exact = match_kind == "exact"

    # v2.8.0: an exact verbatim duplicate is always a no-op write, regardless
    # of mode (append/upsert/merge). This is the primary bloat guard and does
    # not rely on a fuzzy float threshold.
    if is_exact:
        return "skipped"

    # v3.1.0: semantic decision — the cosine band is authoritative.
    if match_kind == "semantic":
        suggestion = dedup_result.get("suggestion")
        # append mode always adds a new note, but still refuse a semantic
        # near-verbatim duplicate (skip band) to keep the bloat guard honest.
        if mode == "append":
            return "skipped" if suggestion == "skip" else "appended"
        if suggestion == "skip":
            return "skipped"
        if suggestion == "defer_fusion":
            # upsert → hand fusion to the caller; merge → machine line-merge is
            # acceptable (that IS what merge mode asked for); we still prefer the
            # safer deferral for upsert.
            return "deferred_fusion" if mode == "upsert" else "merged"
        if suggestion == "merge":
            return "merged"
        # suggestion == "append" (related-but-distinct) → add as new section body
        return "appended"

    # --- Legacy fuzzy fallback (unchanged float thresholds) ---
    if mode == "append":
        # Even in append mode, refuse to write a near-verbatim duplicate.
        # Exact dups are already handled above; this catches fuzzy-near ones.
        if similarity >= APPEND_DEDUP_SKIP_THRESHOLD:
            return "skipped"
        return "appended"

    if mode == "upsert":
        if similarity >= 0.9:
            return "replaced"   # Nearly identical — replace
        elif similarity >= 0.7:
            return "replaced"   # Similar enough — replace (upsert)
        else:
            return "appended"   # Partially similar — append as new

    if mode == "merge":
        if similarity >= 0.9:
            return "skipped"    # Already there — skip
        else:
            return "merged"     # Merge unique parts

    return "appended"


def _execute_write(
    filepath: Path,
    filename: str,
    section: str,
    content: str,
    action: str,
    agent_id: str | None,
    config,
) -> dict:
    """Execute the actual file write with locking."""
    # Provenance comment
    provenance = ""
    if agent_id:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        provenance = f"\n<!-- injected by: {agent_id} at {ts} -->"

    # File lock for concurrency safety
    lock_path = filepath.with_suffix(filepath.suffix + ".lock")
    lock = FileLock(str(lock_path), timeout=10)

    result = {"success": False, "error": "unexpected state"}
    try:
        with lock:
            try:
                result = _do_write(filepath, filename, section, content, action, provenance)
            except Exception as e:
                logger.error("Write error for %s: %s", filename, e)
                result = {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("File lock/write error for %s: %s", filename, e)
        result = {"success": False, "error": str(e)}
    finally:
        # Clean up lock file
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    return result


def _do_write(
    filepath: Path,
    filename: str,
    section: str,
    content: str,
    action: str,
    provenance: str,
) -> dict:
    """Core write logic — must be called within file lock."""
    # If file doesn't exist, create with action-appropriate content
    if not filepath.exists():
        new_content = f"# {filename.removesuffix('.md')}\n\n## {section}\n\n{content}{provenance}\n"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(new_content, encoding="utf-8")
        return {
            "success": True,
            "write_action": "created",
            "bytes_written": len(new_content.encode("utf-8")),
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    # File exists — backup then modify
    raw = filepath.read_text(encoding="utf-8")

    # v0.6.0: Create .bak backup before modification
    try:
        bak_path = filepath.with_suffix(filepath.suffix + ".bak")
        bak_path.write_text(raw, encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to create .bak for %s: %s", filename, e)

    # v3.3.7: consolidation write-back — collapse the whole same-skeleton family
    # into a single section, in the first member's slot. Runs here (under the
    # lock, after .bak) instead of through the section-replace arithmetic below,
    # which only ever touches one section.
    if action == "consolidated":
        return _write_consolidated(filepath, raw, section, content, provenance)

    # CRLF normalization — _find_section and all slice operations use
    # line-length arithmetic; CRLF (\r\n) causes positional drift because
    # _find_section normalises content internally but the outer existing
    # string still contains \r characters. Normalise once here to keep
    # all offsets consistent.
    existing = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Find section position
    section_pos, section_end = _find_section(existing, section)

    if section_pos is None:
        # Section doesn't exist — append it
        block = f"\n\n## {section}\n\n{content}{provenance}\n"
        new_content = existing.rstrip("\n") + "\n" + block
        filepath.write_text(new_content, encoding="utf-8")
        return {
            "success": True,
            "write_action": "section_created",
            "bytes_written": len(block.encode("utf-8")),
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    # Section exists — act based on action
    if action == "merged":
        # Merge: only add lines from new content that don't already appear
        # in the existing section (line-level dedup)
        existing_section = existing[section_pos:section_end]

        def _normalize_merge_line(line: str) -> str:
            """Normalize a line for dedup comparison.

            Strips markdown list prefixes (-, *, 1.), trims whitespace,
            and lowercases for case-insensitive comparison.
            """
            s = line.strip()
            # Strip markdown list markers: - , * , 1. , 1) etc.
            s = re.sub(r"^[-*+]\s+", "", s)
            s = re.sub(r"^\d+[.)]\s+", "", s)
            s = re.sub(r"^#{1,6}\s+", "", s)  # strip heading markers
            return s.lower()

        existing_normalized = set()
        for line in existing_section.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                existing_normalized.add(_normalize_merge_line(stripped))

        new_lines = content.strip().split("\n")
        unique_new = [
            line for line in new_lines
            if line.strip()
            and _normalize_merge_line(line) not in existing_normalized
        ]
        if not unique_new:
            return {
                "success": True,
                "write_action": "merged_no_change",
                "bytes_written": 0,
                "file_size_bytes": len(existing.encode("utf-8")),
            }
        merged_text = "\n".join(unique_new) + provenance
        insert_text = f"\n{merged_text}"
        new_content = existing[:section_end] + insert_text + existing[section_end:]
        filepath.write_text(new_content, encoding="utf-8")
        return {
            "success": True,
            "write_action": "merged",
            "bytes_written": len(insert_text.encode("utf-8")),
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    if action in ("appended", "created"):
        # v2.8.0: precise in-section duplicate guard. find_similar_knowledge
        # (used by _resolve_action) compares whole-file similarity and bails
        # out via a 15:1 length-ratio pre-filter once the file is large —
        # which is exactly when runaway append-bloat happens (the new content
        # is tiny vs a 1MB file, so similarity reads as 0 and the dup sails
        # through). Guard here with an exact, size-independent check: if the
        # trimmed content block already appears verbatim inside the target
        # section, skip the write. Genuinely new content still appends.
        existing_section = existing[section_pos:section_end]
        if content.strip() and content.strip() in existing_section:
            return {
                "success": True,
                "write_action": "append_no_change",
                "bytes_written": 0,
                "file_size_bytes": len(existing.encode("utf-8")),
            }
        # Insert after existing section content (created = first time adding to existing file)
        insert_text = f"\n{content}{provenance}"
        new_content = existing[:section_end] + insert_text + existing[section_end:]
        filepath.write_text(new_content, encoding="utf-8")
        return {
            "success": True,
            "write_action": "appended" if action == "created" else action,
            "bytes_written": len(insert_text.encode("utf-8")),
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    if action == "replaced":
        # Replace entire section content
        new_section = f"## {section}\n\n{content}{provenance}\n"
        new_content = existing[:section_pos] + new_section + existing[section_end:]
        filepath.write_text(new_content, encoding="utf-8")
        return {
            "success": True,
            "write_action": "replaced",
            "bytes_written": len(new_section.encode("utf-8")),
            "file_size_bytes": len(new_content.encode("utf-8")),
        }

    return {"success": False, "error": f"Unknown action: {action}"}


def _section_exists(filepath: Path, section_heading: str) -> bool:
    """True if a ## section with this heading already exists in the file."""
    try:
        if not filepath.exists():
            return False
        raw = filepath.read_text(encoding="utf-8")
        pos, _ = _find_section(raw.replace("\r\n", "\n").replace("\r", "\n"), section_heading)
        return pos is not None
    except Exception:
        return False


def _current_section_body(filepath: Path, section_heading: str) -> str | None:
    """Return the CURRENT body of a ## section in ``filepath``, or None.

    Reads the live file (not the vector store) so the fuse optimistic lock
    compares against the authoritative on-disk state. Strips the heading line;
    returns None when the file or section is absent.
    """
    try:
        if not filepath.exists():
            return None
        raw = filepath.read_text(encoding="utf-8")
        norm = raw.replace("\r\n", "\n").replace("\r", "\n")
        pos, end = _find_section(norm, section_heading)
        if pos is None:
            return None
        block = norm[pos:end]
        lines = block.split("\n")
        return "\n".join(lines[1:]).strip()
    except Exception:
        return None


def _verify_fuse_precondition(
    filepath: Path,
    section_heading: str,
    expected_hash: str | None,
) -> dict:
    """Optimistic-lock check for a fuse write-back.

    The deferred_fusion response handed back ``expected_hash = sha256(old_body)``.
    Before committing the fuse we recompute the hash of the section's CURRENT
    on-disk body and require a match. This guards against a lost update when a
    concurrent write mutated the section between the two handshake calls, and
    turns fuse=True from an arbitrary "replace any section" backdoor into a
    scoped "replace exactly the content you were shown" operation.

    Returns ``{"ok": True}`` on match. On mismatch / missing token / vanished
    section returns ``{"ok": False, "error": ..., "current_hash": ...}``.

    Backward-compat: when ``expected_hash`` is None (a legacy caller that
    predates the lock) we DO NOT hard-fail — we allow the write but the caller
    gets no lost-update protection. This keeps the older two-call contract
    working while new callers opt into the lock by echoing the token. The
    stricter "require the token" behavior can be turned on later once all
    callers are updated.
    """
    if expected_hash is None:
        # Legacy path: no token supplied → no lock enforced (backward compatible).
        return {"ok": True, "unlocked": True}

    current = _current_section_body(filepath, section_heading)
    if current is None:
        # Section is gone (deleted/renamed since defer) — refuse; re-defer.
        return {
            "ok": False,
            "error": (
                f"Target section '## {section_heading}' no longer exists (it was "
                "renamed or removed since the deferred_fusion response). "
                "Re-defer before fusing."
            ),
            "current_hash": None,
        }
    current_hash = _hash_body(current)
    if current_hash != expected_hash:
        return {
            "ok": False,
            "error": (
                f"Section '## {section_heading}' changed since the "
                f"deferred_fusion response (expected {expected_hash}, found "
                f"{current_hash}). Refusing to overwrite (lost-update guard)."
            ),
            "current_hash": current_hash,
        }
    return {"ok": True}


def _find_section(content: str, section_heading: str) -> tuple[int | None, int]:
    """Find the byte range of a ## section in markdown content.

    Returns (start_pos, end_pos) where:
      - start_pos = position of "## heading" line
      - end_pos = position where the next ## or ### heading or EOF begins

    If section not found, returns (None, len(content)).
    """
    # Normalize line endings (Windows CRLF → LF) before splitting
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    section_start_line = None
    section_end_line = len(lines)

    target = f"## {section_heading}".strip().lower()

    for i, line in enumerate(lines):
        if line.strip().lower() == target:
            section_start_line = i
            continue
        if section_start_line is not None:
            # Next ## heading ends this section
            if re.match(r"^##\s+", line):
                section_end_line = i
                break

    if section_start_line is None:
        return None, len(content)

    # Convert line positions to character positions
    start_pos = sum(len(lines[i]) + 1 for i in range(section_start_line))
    end_pos = sum(len(lines[i]) + 1 for i in range(section_end_line))

    return start_pos, end_pos


def _summarize_for_l0(content: str, max_chars: int = 80) -> str:
    """Generate a concise one-line summary from knowledge content for L0 index.

    Rules:
      - Take the first meaningful line (skip blank lines and headings)
      - Truncate to max_chars with ellipsis if needed
      - Strip markdown formatting for readability
    """
    lines = content.strip().split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip headings — we want content, not structure
        if stripped.startswith("#"):
            continue
        # Strip markdown formatting markers while PRESERVING identifier
        # characters. The naive r"[*_`#]" removal corrupted snake_case
        # identifiers (e.g. enabled_toolsets → enabledtoolsets); we now only
        # strip paired emphasis/code markers and leading heading hashes, and
        # leave underscores inside words intact.
        clean = stripped
        clean = re.sub(r"`+", "", clean)              # inline code backticks
        clean = re.sub(r"\*+", "", clean)             # **bold** / *italic*
        clean = re.sub(r"^#+\s*", "", clean)          # leading heading hashes
        # Underscore emphasis only when it wraps a span (e.g. _italic_):
        # require a non-word boundary on the outer side so snake_case is safe.
        clean = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"\1", clean)
        clean = clean.strip()
        # Truncate
        if len(clean) > max_chars:
            clean = clean[:max_chars - 3] + "..."
        return clean
    # Fallback to domain name — shouldn't happen but safe
    return ""


def sync_to_vector_store(
    data_dir: str | Path,
    domain: str,
    content: str,
    summary: str = "",
    replace_domain: bool = False,
) -> dict:
    """Sync a knowledge entry to the vector store for semantic search.

    Called after every successful write to L1 (inject/append/update/create).
    Idempotent — existing entries are updated, new ones are added.

    Two modes:
      - Default (replace_domain=False): add a single entry for ``content``.
        Used by section-level writes (inject_knowledge), where ``content`` is
        one section's body.
      - Full rebuild (replace_domain=True): ``content`` is the WHOLE L1 file.
        Delete every existing vector for ``domain`` then re-add one vector per
        ``## section``. This keeps vectors.db strictly in sync with the file
        for whole-file writes (update/create_knowledge_file), instead of
        leaving the previous section vectors behind as orphans.

    Args:
        data_dir: Path to the data directory containing vectors.db
        domain: Knowledge domain (e.g. "infra")
        content: Section body (default mode) or full file text (rebuild mode)
        summary: One-line summary for indexing
        replace_domain: When True, rebuild the whole domain from ``content``.

    Returns:
        dict with success status
    """
    try:
        from .storage.vector_store import VectorStore
        from .models import KnowledgeEntry, SourceInfo, SourceType, ReviewStatus, KnowledgeType
        import re
        import sqlite3
        import uuid

        db_path = Path(data_dir) / "vectors.db"
        vector_store = VectorStore(db_path)

        def _make_entry(section: str, body: str) -> "KnowledgeEntry":
            return KnowledgeEntry(
                id=str(uuid.uuid4()),
                domain=domain,
                section=section,
                content=body,
                summary=section,
                type=KnowledgeType.FACT,
                confidence=0.9,
                review_status=ReviewStatus.APPROVED,
                source=SourceInfo(type=SourceType.MANUAL, extracted_by="auto_sync"),
            )

        if replace_domain:
            # Whole-file write: rebuild the domain section-by-section so the
            # vector store mirrors the file exactly (no leftover orphans).
            if db_path.exists():
                with sqlite3.connect(db_path) as conn:
                    conn.execute("DELETE FROM vectors WHERE domain = ?", (domain,))
                    conn.commit()
                vector_store._invalidate_cache()

            # Parse "## section\n body" blocks; skip the file-level "# title"
            # header and blockquote intro (they carry no recall value).
            added = 0
            parts = re.split(r"\n(?=## )", content)
            for part in parts:
                m = re.match(r"^##\s+(.+?)\n(.*)", part.strip(), re.DOTALL)
                if not m:
                    continue
                section = m.group(1).strip()
                body = m.group(2).strip()
                if not body:
                    continue
                vector_store.add(_make_entry(section, f"{section}\n{body}"))
                added += 1

            logger.debug("Rebuilt vector store domain=%s sections=%d", domain, added)
            return {"success": True, "action": "vector_rebuilt", "domain": domain, "sections": added}

        text = (summary + "\n" + content).strip() if summary else content.strip()
        entry = KnowledgeEntry(
            id=str(uuid.uuid4()),
            domain=domain,
            section=domain,
            content=content,
            summary=summary or domain,
            type=KnowledgeType.FACT,
            confidence=0.9,
            review_status=ReviewStatus.APPROVED,
            source=SourceInfo(type=SourceType.MANUAL, extracted_by="auto_sync"),
        )
        vector_store.add(entry)
        logger.debug("Synced to vector store: domain=%s", domain)
        return {"success": True, "action": "vector_synced", "domain": domain}
    except Exception as e:
        logger.warning("Vector store sync failed (non-critical): %s", e)
        return {"success": False, "error": str(e)}


def remove_from_vector_store(
    data_dir: str | Path,
    domain: str,
) -> dict:
    """Remove all entries for a domain from the vector store.

    Called when an L1 knowledge file is deleted.
    """
    try:
        import sqlite3
        db_path = Path(data_dir) / "vectors.db"
        if not db_path.exists():
            return {"success": True, "action": "none", "reason": "No vector store"}

        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM vectors WHERE domain = ?", (domain,)
            )
            deleted = cursor.rowcount
            conn.commit()

        logger.info("Removed %d vector entries for domain=%s", deleted, domain)
        return {"success": True, "action": "vector_removed", "domain": domain, "removed": deleted}
    except Exception as e:
        logger.warning("Vector store removal failed (non-critical): %s", e)
        return {"success": False, "error": str(e)}


def reindex_vector_store(
    config: "MemoryConfig",
    drop_existing: bool = True,
    calibrate: bool = True,
) -> dict:
    """Full rebuild of vectors.db from every L1 .md file (section-level).

    The vector store is PURE DERIVED DATA — every vector can be recomputed from
    the markdown body alone, one vector per ## section. That single-direction
    derivability is the technical guarantee that "vectors are not a second
    source of truth": if vectors.db is lost, stale, or was only partially
    synced by historical writes, this rebuilds it losslessly from the files.

    Use cases:
      - Cold start after pip install / first run on an existing knowledge base.
      - Reconciliation when past inject writes didn't all sync their vectors.
      - After bulk external edits to the .md files.

    Args:
        config: MemoryConfig (knowledge dirs + data dir).
        drop_existing: when True (default) wipe vectors.db first so the rebuild
            is authoritative (no orphaned vectors from deleted sections/files).
            The per-domain replace_domain rebuild already clears each domain it
            touches; the global wipe additionally reaps whole domains whose file
            no longer exists.
        calibrate: when True (default) also self-calibrate the semantic
            thresholds from the freshly-rebuilt corpus (Task 4). The result is
            attached under ``calibration`` and cached for the write path.

    Returns:
        dict: {success, domains, sections, files, errors, calibration}
    """
    data_dir = config.home / "data"
    db_path = data_dir / "vectors.db"

    result = {
        "success": True,
        "domains": 0,
        "sections": 0,
        "files": 0,
        "errors": [],
    }

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if drop_existing and db_path.exists():
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.execute("DELETE FROM vectors")
                conn.commit()
            # Reset the singleton store's cache if one is already live.
            try:
                from .storage.vector_store import VectorStore
                VectorStore(db_path)._invalidate_cache()
            except Exception:
                pass

        seen_domains: set[str] = set()
        for kdir in config.knowledge_dirs:
            if not kdir.exists():
                continue
            for fp in sorted(kdir.glob("*.md")):
                domain = fp.name.removesuffix(".md")
                # A domain can exist in namespace + shared; only rebuild once.
                # First file wins (namespace dir is listed first).
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                try:
                    text = fp.read_text(encoding="utf-8")
                except OSError as e:
                    result["errors"].append(f"{fp.name}: {e}")
                    continue
                sync = sync_to_vector_store(
                    data_dir=data_dir,
                    domain=domain,
                    content=text,
                    replace_domain=True,
                )
                result["files"] += 1
                if sync.get("success"):
                    result["domains"] += 1
                    result["sections"] += int(sync.get("sections", 0) or 0)
                else:
                    result["errors"].append(f"{fp.name}: {sync.get('error')}")

        # v3.1.1: self-calibrate thresholds from the freshly-rebuilt corpus.
        if calibrate:
            try:
                result["calibration"] = calibrate_thresholds(config, persist=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("post-reindex calibration failed: %s", e)
                result["calibration"] = {"success": False, "error": str(e)}

        logger.info(
            "Reindexed vector store: %d domains, %d sections from %d files",
            result["domains"], result["sections"], result["files"],
        )
        if result["errors"]:
            result["success"] = len(result["errors"]) < result["files"]
        return result
    except Exception as e:
        logger.error("Vector reindex failed: %s", e)
        return {"success": False, "error": str(e), **result}


def vector_store_needs_reindex(config: "MemoryConfig") -> bool:
    """Heuristic: does vectors.db look under-populated vs the .md files?

    Used to decide whether to trigger a first-run/post-update rebuild. Returns
    True when there are markdown files but the vector store is empty, covers
    fewer DOMAINS than exist on disk, OR (v3.1.1) is missing SECTIONS within a
    covered domain — the section-level gap that a silently-failed vector sync
    leaves behind (file has the section, vectors.db doesn't → dedup can't recall
    it → duplicate re-appends). Cheap: counts markdown ## headings and compares
    to per-domain vector counts; no embedding.
    """
    try:
        import sqlite3
        from .storage.vector_store import VectorStore
        data_dir = config.home / "data"
        db_path = data_dir / "vectors.db"

        # Count markdown domains AND their section headings (bodyful sections
        # only — the rebuild skips empty-body sections, so we must too or we'd
        # report a false gap).
        md_domains: set[str] = set()
        md_section_counts: dict[str, int] = {}
        seen: set[str] = set()
        for kdir in config.knowledge_dirs:
            if not kdir.exists():
                continue
            for fp in kdir.glob("*.md"):
                dom = fp.name.removesuffix(".md")
                md_domains.add(dom)
                if dom in seen:
                    continue  # first file (namespace) wins, mirrors rebuild
                seen.add(dom)
                try:
                    raw = fp.read_text(encoding="utf-8")
                except OSError:
                    continue
                md_section_counts[dom] = _count_bodyful_sections(raw)
        if not md_domains:
            return False
        if not db_path.exists():
            return True

        stats = VectorStore(db_path).stats()
        indexed_domains = set(stats.get("domains", {}).keys())
        # Domain-level coverage gap.
        if not md_domains.issubset(indexed_domains):
            return True

        # Section-level coverage gap: a domain has more bodyful sections on disk
        # than vectors in the store → historical sync missed some sections.
        per_domain_vectors = stats.get("domains", {})
        for dom, want in md_section_counts.items():
            have = int(per_domain_vectors.get(dom, 0) or 0)
            if want > have:
                logger.debug(
                    "needs_reindex: domain %s has %d sections but %d vectors",
                    dom, want, have,
                )
                return True
        return False
    except Exception as e:
        logger.debug("needs_reindex check failed: %s", e)
        return False


def _count_bodyful_sections(raw: str) -> int:
    """Count ## sections with a non-empty body — mirrors the rebuild's filter.

    ``sync_to_vector_store(replace_domain=True)`` skips sections whose body is
    empty, so the reindex-need check must count the same way to avoid a false
    "missing section" positive.
    """
    import re as _re
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = _re.split(r"\n(?=## )", norm)
    count = 0
    for part in parts:
        m = _re.match(r"^##\s+(.+?)\n(.*)", part.strip(), _re.DOTALL)
        if not m:
            continue
        if m.group(2).strip():
            count += 1
    return count

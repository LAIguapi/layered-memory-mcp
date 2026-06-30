"""
Knowledge Injector — Smart write engine for L1 knowledge files.

Provides high-level write operations that handle:
  - Deduplication check before writing
  - Section-level targeting (find or create ## headings)
  - Multiple write modes: upsert / append / merge
  - Automatic L0 index sync after successful writes
  - File size warnings and content validation
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

from filelock import FileLock

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


def inject_knowledge(
    config: "MemoryConfig",
    domain: str,
    section: str,
    content: str,
    mode: str = "upsert",
    agent_id: str | None = None,
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

    Returns:
        Result dict with action taken, dedup info, L0 sync status.
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

    # --- 2. Dedup check (scan across all knowledge dirs: namespace + shared) ---
    kdirs = [str(d) for d in config.knowledge_dirs]
    dedup_result = _check_dedup(content, kdirs if len(kdirs) > 1 else kdirs[0], config.dedup_threshold)

    # --- 3. Resolve action based on mode + dedup ---
    effective_action = _resolve_action(mode, dedup_result)

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
    domain_clean = filename.removesuffix(".md")
    write_action = write_result.get("write_action", effective_action)

    # --- 6b. Vector store sync (v2.2.0) ---
    vector_result = sync_to_vector_store(
        data_dir=config.home / "data",
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
            maint = auto_maintain_after_write(config, l0_pointer=l0_pointer)
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

def _check_dedup(content: str, knowledge_dir: str, threshold: float) -> dict:
    """Run dedup check against existing knowledge.

    v2.8.0 two-layer strategy:
      1. EXACT layer (primary): normalized verbatim match across all knowledge
         files. O(total_lines), size-independent, 100% precise, CJK-safe. This
         is what actually plugs the runaway-append bloat (99% of which was
         byte-for-byte duplicates). Reports similarity=1.0 on an exact hit.
      2. FUZZY layer (secondary): the legacy difflib whole-file SequenceMatcher
         for near-but-not-identical content. Kept as a soft signal for
         upsert/merge decisions; its known weaknesses (15:1 length pre-filter,
         whole-file dilution, no CJK semantics) no longer matter for the bloat
         case because the exact layer fires first.

    NOTE: a proper semantic layer (bge-small-zh via fastembed/ONNX) is the
    planned upgrade to replace the fuzzy difflib layer — tracked separately.
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

    # --- Layer 2: fuzzy similarity (legacy difflib, soft signal only) ---
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
    """Determine the effective write action based on mode and dedup result."""
    if not dedup_result.get("similar_found"):
        return "created" if mode != "append" else "appended"

    similarity = dedup_result.get("similarity", 0)
    is_exact = dedup_result.get("match_kind") == "exact"

    # v2.8.0: an exact verbatim duplicate is always a no-op write, regardless
    # of mode (append/upsert/merge). This is the primary bloat guard and does
    # not rely on a fuzzy float threshold.
    if is_exact:
        return "skipped"

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

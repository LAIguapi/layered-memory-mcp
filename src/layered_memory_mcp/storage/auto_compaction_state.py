"""Auto-compaction backoff state — derived operational state, nothing more.

WHY THIS FILE EXISTS
--------------------
Under routing mode ``enforce`` a compaction pass deliberately leaves every
non-L1 entry in the agent's memory file: a blocked entry has nowhere else to go
yet, and dropping it would be data loss. The lazy trigger in
``memory_compactor.auto_maintain_after_write`` is a usage ratio, so a memory
file full of blocked entries stays over threshold forever and every subsequent
write re-ran a full pass that migrated nothing.

Remembering *which* blocked set was already seen is what lets the next write
skip a pass that cannot do anything — while a genuinely new bloat entry changes
the fingerprint and still triggers one immediately.

WHAT THIS IS NOT
----------------
Not L1 knowledge, and not ``MEMORY_CHANGELOG`` provenance (that log records
semantic mutations of L0/L1; backing off is not a mutation). Nothing here is a
source of truth: deleting this file costs exactly one extra no-op compaction
pass, which is why every read and write below is best-effort and never raises.

SHAPE
-----
One small JSON document under ``<home>/data/auto_compaction_state.json``::

    {
      "schema_version": 2,
      "kind": "auto_compaction_backoff",
      "updated_at": "2026-10-12T09:00:00+00:00",
      "targets": {
        "<memory-path key>": {
          "blocked_set": {"count": 3, "fingerprint": "sha256:…",
                          "by_surface": {"skill": 2, "todo": 1}},
          "routing_mode": "enforce",
          "routing_enabled": true,
          "source_digest": "sha256:…" | null,
          "classifier_inputs_hash": "sha256:…" | null,
          "first_seen_at": "…", "recorded_at": "…", "last_attempt_at": "…",
          "skipped_passes": 2
        }
      }
    }

Keyed by a *hash* of the memory file path, for two reasons: two memory files
under one home cannot overwrite each other's state, and the document carries no
absolute path, so it can be quoted in a report or a log without leaking where
the user's memory file lives. Entry content is likewise present only as a hash —
this file stores no memory text.

``source_digest`` answers "is the memory file still the one that produced this
blocked set"; ``classifier_inputs_hash`` (schema 2) answers the other half,
"would the classifier still agree". Byte-identity alone was never enough: the
same file classifies differently after a marker-vocabulary or domain-rule
change, so a shortcut taken on bytes only would have gone on matching a stale
blocked set until somebody happened to edit the memory file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("layered_memory.auto_compaction_state")

#: Bumped whenever a field changes meaning. A document written by another
#: version is ignored rather than guessed at (see :meth:`AutoCompactionState.load`).
#:
#: 2 — added ``classifier_inputs_hash``. No migration is written for derived
#: state: a version-1 document is simply ignored, which costs the install one
#: extra compaction pass per memory file and then re-records itself. Reading a
#: v1 record as if it carried a classifier hash is the one outcome worth
#: avoiding — it would be a shortcut taken on an input nobody checked.
SCHEMA_VERSION = 2

#: Self-describing marker, so a stray file is identifiable without this module.
STATE_KIND = "auto_compaction_backoff"

STATE_FILENAME = "auto_compaction_state.json"

#: Length of the truncated hex digests used as keys/fingerprints. 16 hex chars
#: (64 bits) is far past collision range for "how many entries are in one
#: memory file", and keeps the document readable.
_KEY_CHARS = 16

_WHITESPACE_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(config: Any) -> Path:
    """``<home>/data/auto_compaction_state.json`` for this install."""
    return Path(config.home) / "data" / STATE_FILENAME


def target_key(memory_path: str | Path) -> str:
    """Stable, non-reversible key for one agent memory file."""
    absolute = os.path.abspath(str(memory_path))
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:_KEY_CHARS]


def entry_content_hash(entry: str) -> str:
    """Content hash of one memory entry, whitespace-normalised.

    Normalisation (strip + collapse whitespace runs) means re-wrapping or
    re-indenting an untouched entry does not read as a new blocked entry, while
    any change to its words does.
    """
    normalised = _WHITESPACE_RE.sub(" ", str(entry or "")).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:_KEY_CHARS]


def source_digest(memory_path: str | Path) -> str | None:
    """Digest of the memory file as it stands, or None if unreadable."""
    try:
        raw = Path(memory_path).read_bytes()
    except OSError as exc:
        logger.debug("auto-compaction state: source digest failed: %s", exc)
        return None
    return "sha256:" + hashlib.sha256(raw).hexdigest()[: _KEY_CHARS * 2]


def blocked_set_fingerprint(records: list[dict]) -> dict:
    """Fingerprint a set of blocked candidates: ``{count, fingerprint, by_surface}``.

    Hashed per record: the entry's content hash, the surface it routed to and
    its status. Sorted before hashing, so the fingerprint is a property of the
    *set* — neither the order entries appear in the memory file nor the order
    the buckets were filled can change it. No timestamp is involved, so two
    passes over unchanged content agree.

    An empty set has ``fingerprint: None`` rather than the hash of the empty
    string: "nothing is blocked" is a state to clear, not one to match against.
    """
    lines: list[str] = []
    by_surface: dict[str, int] = {}
    for record in records or []:
        surface = record.get("surface") or "unclassified"
        digest = record.get("entry_hash") or entry_content_hash(
            record.get("entry_preview") or ""
        )
        lines.append(f"{surface}|{record.get('status') or ''}|{digest}")
        by_surface[surface] = by_surface.get(surface, 0) + 1

    if not lines:
        return {"count": 0, "fingerprint": None, "by_surface": {}}

    payload = "\n".join(sorted(lines))
    return {
        "count": len(lines),
        "fingerprint": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "by_surface": dict(sorted(by_surface.items())),
    }


def _empty_document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": None,
        "targets": {},
    }


class AutoCompactionState:
    """The JSON document above. Every method is best-effort and never raises."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ── read ────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return False

    def load(self) -> dict:
        """The whole document, or an empty one when absent/unusable.

        A document carrying a different ``schema_version`` is treated as absent:
        the cost of ignoring it is one extra compaction pass, and the cost of
        misreading it is a pass that *should* have run and didn't.
        """
        if not self.exists():
            return _empty_document()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("auto-compaction state unreadable (%s): %s", self.path, exc)
            return _empty_document()
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return _empty_document()
        targets = data.get("targets")
        if not isinstance(targets, dict):
            return _empty_document()
        document = _empty_document()
        document["updated_at"] = data.get("updated_at")
        document["targets"] = {
            str(key): value for key, value in targets.items() if isinstance(value, dict)
        }
        return document

    def get(self, key: str) -> dict | None:
        """The record for one memory file, or None."""
        return self.load()["targets"].get(str(key))

    # ── write (atomic replace; best-effort) ─────────────────────────────

    def record(
        self,
        key: str,
        *,
        blocked_set: dict,
        routing_mode: str | None,
        routing_enabled: bool | None,
        source_digest: str | None = None,
        classifier_inputs_hash: str | None = None,
    ) -> dict | None:
        """Remember the blocked set an enforce pass just left behind.

        ``source_digest`` is the memory file as the pass left it, and is only
        passed when that pass was a clean no-op (no errors, nothing refused for
        want of a heading). It enables the cheap "file is byte-identical"
        check; omitting it just means the next decision classifies again.

        ``classifier_inputs_hash`` is the classifier that produced this blocked
        set (``routing.classifier_inputs_hash``). ``None`` means the caller
        could not describe it, and a stored ``None`` can never match a later
        hash — so the shortcut stays closed rather than opening on an unknown.

        ``first_seen_at`` survives a re-record of the same fingerprint, so the
        report can say how long this set has been sitting there.
        """
        document = self.load()
        previous = document["targets"].get(str(key)) or {}
        same_set = (
            previous.get("blocked_set", {}).get("fingerprint")
            == blocked_set.get("fingerprint")
        )
        now = _now()
        record = {
            "blocked_set": {
                "count": blocked_set.get("count", 0),
                "fingerprint": blocked_set.get("fingerprint"),
                "by_surface": dict(blocked_set.get("by_surface") or {}),
            },
            "routing_mode": routing_mode,
            "routing_enabled": routing_enabled,
            "source_digest": source_digest,
            "classifier_inputs_hash": classifier_inputs_hash,
            "first_seen_at": previous.get("first_seen_at", now) if same_set else now,
            "recorded_at": now,
            "last_attempt_at": now,
            "skipped_passes": previous.get("skipped_passes", 0) if same_set else 0,
        }
        document["targets"][str(key)] = record
        return record if self._save(document) else None

    def note_skipped_pass(
        self,
        key: str,
        *,
        source_digest: str | None = None,
        classifier_inputs_hash: str | None = None,
    ) -> dict | None:
        """Count one backed-off pass against an existing record.

        Also refreshes ``source_digest`` when the caller recomputed it (the
        memory file changed in a way that did not change the blocked set — a
        new L0 pointer, say), so the next decision can take the cheap path. A
        ``classifier_inputs_hash`` is refreshed on the same terms: the caller
        re-classified under the new inputs and got the same blocked set, so the
        record now belongs to that classifier.

        Both are only written when passed. ``None`` means "I did not recompute
        it", never "it is unknown" — a skip must not quietly erase the pins the
        next shortcut is checked against.
        """
        document = self.load()
        record = document["targets"].get(str(key))
        if not isinstance(record, dict):
            return None
        record["last_attempt_at"] = _now()
        record["skipped_passes"] = int(record.get("skipped_passes") or 0) + 1
        if source_digest is not None:
            record["source_digest"] = source_digest
        if classifier_inputs_hash is not None:
            record["classifier_inputs_hash"] = classifier_inputs_hash
        document["targets"][str(key)] = record
        return record if self._save(document) else None

    def clear(self, key: str) -> bool:
        """Forget one memory file's blocked set. True when something was removed."""
        document = self.load()
        if str(key) not in document["targets"]:
            return False
        document["targets"].pop(str(key), None)
        return self._save(document)

    def _save(self, document: dict) -> bool:
        """Atomic replace: a reader never sees a half-written document.

        No lock. Two concurrent writers can only lose the *losing* writer's
        update, and this is derived state — the next pass recomputes it.
        """
        document["schema_version"] = SCHEMA_VERSION
        document["kind"] = STATE_KIND
        document["updated_at"] = _now()
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            logger.debug("auto-compaction state write failed (%s): %s", self.path, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False


__all__ = [
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "STATE_KIND",
    "AutoCompactionState",
    "blocked_set_fingerprint",
    "entry_content_hash",
    "source_digest",
    "state_path",
    "target_key",
]

"""Confined, no-follow knowledge reads; unsupported platforms fail closed."""

import os
import stat
from pathlib import Path

MAX_KNOWLEDGE_BYTES = 5 * 1024 * 1024
_SECURE_OPEN_SUPPORTED = os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")


class KnowledgeReadError(ValueError):
    """A stable, content-free refusal code."""


def _identity(status):
    return status.st_dev, status.st_ino


def read_knowledge_text(
    filepath: Path, knowledge_root: Path | None = None, *,
    encoding: str = "utf-8", errors: str = "strict",
) -> str:
    """Read a regular text file using pinned, validated directory descriptors.

    Every component, including ancestors of the configured root, must be a
    non-symlink. Name/inode checks reject replacement before or during reading.
    This is a read boundary, not a write sandbox or protection from hard links
    and concurrent in-place writers with the same filesystem permissions.
    Knowledge callers retain strict UTF-8 defaults; the legacy cache helper
    supplies its explicit encoding and replacement-decoding policy.
    """
    filepath = Path(filepath)
    root = Path(knowledge_root) if knowledge_root is not None else filepath.parent
    if ".." in filepath.parts or ".." in root.parts:
        raise KnowledgeReadError("knowledge_path_invalid")
    target = Path(os.path.abspath(filepath))
    root = Path(os.path.abspath(root))
    try:
        target.relative_to(root)
    except ValueError:
        raise KnowledgeReadError("knowledge_path_outside_root") from None
    if not _SECURE_OPEN_SUPPORTED:
        raise KnowledgeReadError("knowledge_secure_open_unsupported")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors = []
    components = []
    try:
        directory = os.open(target.anchor, directory_flags)
        descriptors.append(directory)
        for component in target.parts[1:-1]:
            before = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise KnowledgeReadError("knowledge_symlink_or_invalid_component")
            child = os.open(component, directory_flags, dir_fd=directory)
            descriptors.append(child)
            if _identity(before) != _identity(os.fstat(child)):
                raise KnowledgeReadError("knowledge_read_changed")
            components.append((directory, component, before))
            directory = child
        before = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise KnowledgeReadError("knowledge_symlink_or_not_regular")
        descriptor = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW |
                             getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise KnowledgeReadError("knowledge_read_changed")
        if opened.st_size > MAX_KNOWLEDGE_BYTES:
            raise KnowledgeReadError("knowledge_file_too_large")
        chunks = []
        remaining = MAX_KNOWLEDGE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if not remaining:
            raise KnowledgeReadError("knowledge_file_too_large")
        components.append((directory, target.name, opened))
        for parent, name, expected in components:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(current) != _identity(expected) or current.st_mode != expected.st_mode:
                raise KnowledgeReadError("knowledge_read_changed")
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise KnowledgeReadError("knowledge_read_changed")
        return b"".join(chunks).decode(encoding, errors=errors)
    except FileNotFoundError:
        raise FileNotFoundError("knowledge_file_not_found") from None
    except OSError:
        raise KnowledgeReadError("knowledge_read_denied_or_changed") from None
    except (UnicodeError, LookupError):
        raise KnowledgeReadError("knowledge_encoding_invalid") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

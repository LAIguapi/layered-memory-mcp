"""YAML frontmatter parser for markdown files.

Supports standard --- delimited frontmatter.
Gracefully handles files without frontmatter (legacy mode).
"""

from __future__ import annotations

import re
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


# Regex to match YAML frontmatter: ---\n...\n---
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter from markdown text.

    Returns:
        (metadata_dict, content) where metadata_dict is None if no frontmatter.
    """
    if yaml is None:
        return None, text

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text

    try:
        meta = yaml.safe_load(m.group(1))
        if not isinstance(meta, dict):
            return None, text
        return meta, m.group(2)
    except yaml.YAMLError:
        return None, text


def dump_frontmatter(metadata: dict[str, Any], content: str) -> str:
    """Serialize metadata and content to markdown with YAML frontmatter.

    Args:
        metadata: Dictionary of metadata fields.
        content: Markdown content (without frontmatter).

    Returns:
        Full markdown string with frontmatter.
    """
    if yaml is None:
        raise ImportError("PyYAML is required for frontmatter serialization")

    # Clean metadata: remove None values and empty lists
    clean_meta = {}
    for k, v in metadata.items():
        if v is None:
            continue
        if isinstance(v, list) and not v:
            continue
        clean_meta[k] = v

    yaml_str = yaml.safe_dump(
        clean_meta,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    return f"---\n{yaml_str}---\n\n{content.strip()}\n"

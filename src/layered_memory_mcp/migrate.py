"""One-time migration helper: seed ``domain_keywords`` into config.yaml.

Background
----------
Older releases shipped a hard-coded domain-classification table inside the
framework, so users with **no** ``config.yaml`` still got automatic
infra/dev/docs classification for free. That built-in preset has been removed —
the framework now ships zero presets and reads the table exclusively from
``config.domain_keywords`` (constructor / env / ``config.yaml``).

Users who relied on the old built-in defaults can run this CLI once to write the
"classic" technical table into their ``~/.layered-memory/config.yaml``, exactly
restoring the previous behaviour. The written table is fully editable — add,
remove, or rename domains freely afterwards.

The classic default is **purely technical** (infra/dev/docs). The framework
never ships any business/subject-matter presets; domain vocabulary specific to a
user's field is theirs to add in their own config.

Usage
-----
    python -m layered_memory_mcp.migrate            # write if not configured
    python -m layered_memory_mcp.migrate --dry-run  # preview only, no write
    python -m layered_memory_mcp.migrate --force    # overwrite existing table
    python -m layered_memory_mcp.migrate --config /path/to/config.yaml
"""

import argparse
import sys
from pathlib import Path

import yaml

from .config import default_home


# Classic default classification table — the clean technical content that the
# framework used to ship built-in. Purely infra/dev/docs; no business presets.
_CLASSIC_DEFAULT_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "infra": [
        "proxy", "server", "docker", "ssh", "network", "deploy",
        "config", "cloud", "kubernetes", "nginx", "dns", "firewall",
        "linux", "shell", "bash", "cron",
    ],
    "dev": [
        "principle", "testing", "DRY", "design", "refactor",
        "code review", "TDD", "architecture", "pattern",
    ],
    "docs": [
        "readme", "documentation", "guide", "tutorial", "how-to",
    ],
}


def _resolve_config_path(config_arg: str | None) -> Path:
    """Resolve the target config.yaml path (``--config`` or ``<home>/config.yaml``)."""
    if config_arg:
        return Path(config_arg).expanduser()
    return default_home() / "config.yaml"


def _load_config_data(path: Path) -> dict:
    """Parse an existing config.yaml into a dict.

    Returns ``{}`` for a missing or empty file. Raises ``ValueError`` when the
    file exists but does not parse to a mapping — we refuse to touch a file we
    can't understand rather than silently clobbering it.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} does not contain a top-level mapping; refusing to modify it."
        )
    return data


def _has_domain_keywords(data: dict) -> bool:
    """True when the config already defines a non-empty ``domain_keywords`` table."""
    section = data.get("domain_keywords")
    return isinstance(section, dict) and len(section) > 0


def _render_domain_keywords_block(keywords: dict[str, list[str]]) -> str:
    """Render a commented ``domain_keywords:`` YAML block for the classic table."""
    body = yaml.safe_dump(
        {"domain_keywords": keywords},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    header = (
        "# Domain classification table for the auto-extractor and compaction.\n"
        "# Maps <domain> -> [keyword, ...]; matched case-insensitively, the\n"
        "# highest-scoring domain wins, unmatched content stays the fallback.\n"
        "# Written by `layered-memory-migrate`; edit freely.\n"
    )
    return header + body


def _build_new_content(
    path: Path,
    existing_text: str | None,
    data: dict,
    keywords: dict[str, list[str]],
    force: bool,
) -> str:
    """Produce the full new file content that seeds ``domain_keywords``.

    - Fresh file (or empty): a standalone commented block.
    - Existing file without the section: append the block, leaving the original
      bytes (and any comments) untouched.
    - ``--force`` over an existing section: round-trip the whole mapping (YAML
      comments in the original are not preserved in this path).
    """
    block = _render_domain_keywords_block(keywords)

    if not existing_text or not existing_text.strip():
        return block

    if force and _has_domain_keywords(data):
        merged = dict(data)
        merged["domain_keywords"] = keywords
        return yaml.safe_dump(
            merged, sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    # Existing content, no domain_keywords section — append, preserving the rest.
    return existing_text.rstrip("\n") + "\n\n" + block


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="layered-memory-migrate",
        description=(
            "Seed the classic technical domain_keywords table (infra/dev/docs) "
            "into your layered-memory config.yaml. Idempotent by default."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to config.yaml (default: <LAYERED_MEMORY_HOME>/config.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be written without touching any file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing domain_keywords table.",
    )
    args = parser.parse_args(argv)

    path = _resolve_config_path(args.config)

    try:
        data = _load_config_data(path)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    already = _has_domain_keywords(data)

    if already and not args.force:
        print(f"config.yaml already defines domain_keywords ({len(data['domain_keywords'])} domains).")
        print(f"  location: {path}")
        print("  nothing to do — the framework already reads your table.")
        print("  re-run with --force to overwrite it with the classic defaults.")
        return 0

    existing_text = path.read_text(encoding="utf-8") if path.exists() else None
    keywords = _CLASSIC_DEFAULT_DOMAIN_KEYWORDS
    new_content = _build_new_content(path, existing_text, data, keywords, args.force)

    action = "overwrite" if already else ("create" if existing_text is None else "update")
    domains = ", ".join(keywords.keys())

    if args.dry_run:
        print(f"[dry-run] would {action}: {path}")
        print(f"[dry-run] would write domain_keywords: {domains}")
        print("[dry-run] --- content preview ---")
        print(new_content.rstrip("\n"))
        print("[dry-run] --- end preview (nothing written) ---")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")

    print(f"{action}d: {path}")
    print(f"wrote domain_keywords: {domains}")
    print("The framework will pick this up on next start. Edit the file to customise.")
    return 0


def main() -> None:
    """Console-script entry point (``layered-memory-migrate``)."""
    raise SystemExit(_run())


if __name__ == "__main__":
    main()

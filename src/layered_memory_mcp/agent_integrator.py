"""
Agent Integrator — inject layered-memory awareness into the agent's runtime.

Problem: The agent sees L0 index through its own memory file (manually
maintained), but the MCP Server maintains L0.md as the authoritative source.
These two diverge over time — the memory file stagnates, L0.md grows. The agent
becomes blind to newly created knowledge domains.

Solution: At framework init time, inject a small rule into the agent's
persona/memory so it knows to call get_l0_index() each session — bypassing
the stale L0 entries entirely.

WHERE THE PRODUCT KNOWLEDGE LIVES
---------------------------------
This module is integration *support*, not common core, and it no longer probes
for any product itself: :func:`detect_agent_type` delegates to the environment
adapter registry (:mod:`.adapters.environment`), and the Hermes persona block
lives with the Hermes adapter (:mod:`.adapters.hermes`). What stays here is
product-agnostic: marker-delimited idempotent injection, and the recommendation
payload ``init_framework`` returns — whose per-agent branch is keyed off the
``agent_type`` the adapter reports.

Supported agents:
  - any agent whose adapter reports a persona file (Hermes: ``SOUL.md``)
  - generic MCP agents: returns a memory snippet for the user to add manually

All injections are marked with BEGIN/END comments — identifiable and reversible.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .adapters.hermes import SOUL_BLOCK

if TYPE_CHECKING:
    from .config import MemoryConfig

logger = logging.getLogger("layered_memory_mcp.integrator")

# ── Injection markers (for idempotency + reversibility) ────────────
BEGIN_MARKER = "<!-- BEGIN layered-memory-mcp (auto-injected"
END_MARKER = "<!-- END layered-memory-mcp -->"

# SOUL_BLOCK (re-exported above) is the Hermes-native persona text and lives in
# adapters/hermes.py. The snippet below names no product file layout, so it is
# the one offered to every host, including the ones nothing recognises.

MEMORY_SNIPPET = """知识索引由 layered-memory MCP Server 管理。
每 session 调用 get_l0_index() 获取完整 L0 索引。
L0.md 自动维护，不依赖 MEMORY.md 中的 [L0] 条目。
写入用 inject_knowledge，检索用 recall_knowledge。"""


def detect_agent_type(home: Path | None = None) -> dict:
    """Detect which agent is hosting this MCP server.

    Delegates to the environment adapter registry — the probe used to be a
    hardcoded ``~/.hermes`` walk right here. The first adapter that recognises
    its own installation wins; an unrecognised host reports ``generic_mcp``
    with every path ``None``.

    Returns a dict with keys:
      - agent_type: the adapter's agent type ("hermes", ..., "generic_mcp")
      - soul_path: Path or None (persona file, when the product has one)
      - memory_path: Path or None (the agent's native memory file)
      - home_dir: Path to the agent's own home, or None
    """
    from .adapters.environment import integration_environment

    return integration_environment(home)


def is_soul_injected(soul_path: Path) -> bool:
    """Check if the SOUL file already has our injection block."""
    if not soul_path.exists():
        return False
    content = soul_path.read_text(encoding="utf-8")
    return BEGIN_MARKER.split("(")[0] in content and END_MARKER in content


def is_memory_injected(memory_path: Path) -> bool:
    """Check if the memory file already has our injection snippet."""
    if not memory_path.exists():
        return False
    content = memory_path.read_text(encoding="utf-8")
    return "layered-memory MCP Server 管理" in content or "get_l0_index()" in content


def inject_soul(soul_path: Path, version: str) -> dict:
    """Inject layered-memory rules into the agent's persona file.

    ``soul_path`` comes from the adapter (``detect_agent_type()["soul_path"]``);
    the block itself is :data:`SOUL_BLOCK`, owned by that adapter.

    Returns status dict. Idempotent — won't inject twice.
    """
    if not soul_path:
        return {"success": False, "error": "SOUL.md not found", "action": "none"}

    if is_soul_injected(soul_path):
        return {
            "success": True,
            "action": "already_injected",
            "file": str(soul_path),
            "message": "SOUL.md already has layered-memory rules.",
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block = SOUL_BLOCK.format(
        begin_marker=BEGIN_MARKER,
        version=version,
        timestamp=timestamp,
        end_marker=END_MARKER,
    )

    try:
        content = soul_path.read_text(encoding="utf-8")
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + block + "\n"
        soul_path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "action": "injected",
            "file": str(soul_path),
            "message": "SOUL.md updated with layered-memory rules.",
        }
    except OSError as exc:
        return {"success": False, "error": str(exc), "action": "failed"}


def inject_memory(memory_path: Path) -> dict:
    """Inject layered-memory awareness snippet into MEMORY.md.

    Returns status dict. Idempotent — won't inject twice.
    """
    if not memory_path:
        return {"success": False, "error": "MEMORY.md not found", "action": "none"}

    if is_memory_injected(memory_path):
        return {
            "success": True,
            "action": "already_injected",
            "file": str(memory_path),
            "message": "MEMORY.md already has layered-memory awareness snippet.",
        }

    snippet = f"\n{MEMORY_SNIPPET}\n"

    try:
        content = memory_path.read_text(encoding="utf-8")
        if not content.endswith("\n"):
            content += "\n"
        content += snippet
        memory_path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "action": "injected",
            "file": str(memory_path),
            "message": "MEMORY.md updated with layered-memory awareness snippet.",
        }
    except OSError as exc:
        return {"success": False, "error": str(exc), "action": "failed"}


def remove_soul_injection(soul_path: Path) -> dict:
    """Remove the layered-memory injection block from SOUL.md."""
    if not soul_path or not soul_path.exists():
        return {"success": False, "error": "SOUL.md not found", "action": "none"}

    content = soul_path.read_text(encoding="utf-8")
    begin_idx = content.find(BEGIN_MARKER.split("(")[0])
    end_idx = content.find(END_MARKER)

    if begin_idx == -1 or end_idx == -1:
        return {"success": True, "action": "not_found", "message": "No injection block found."}

    # Remove the block including surrounding newlines
    before = content[:begin_idx].rstrip("\n")
    after = content[end_idx + len(END_MARKER):].lstrip("\n")
    new_content = before + "\n" + after
    if new_content.endswith("\n\n"):
        new_content = new_content.rstrip("\n") + "\n"

    soul_path.write_text(new_content, encoding="utf-8")
    return {"success": True, "action": "removed", "file": str(soul_path)}


# ── Integration recommendation for init_framework ───────────────────

def build_init_recommendation(
    agent_info: dict,
    version: str,
    memory_already_injected: bool,
    soul_already_injected: bool,
) -> dict:
    """Build the recommendation payload for init_framework to return.

    The agent will present this to the user as interactive choices.

    The per-agent branch below is keyed off the ``agent_type`` the environment
    adapter reported, not off a probe of its own. The option *text* is still
    written for Hermes (it names SOUL.md and the Hermes Dashboard) because
    Hermes is the only host with those surfaces today; a second agent with a
    persona file would add its own branch here rather than change the core.
    """
    agent_type = agent_info["agent_type"]
    recommendations = []

    if agent_type == "hermes":
        if soul_already_injected:
            recommendations.append({
                "option": "A",
                "label": "SOUL 已注入",
                "description": "SOUL.md 已包含 layered-memory 规则，无需操作。",
                "action": "none",
                "already_done": True,
            })
        else:
            recommendations.append({
                "option": "A",
                "label": "注入 SOUL.md（推荐）",
                "description": (
                    "在 SOUL.md 中追加记忆框架规则。每个 session agent 会自动调用 "
                    "get_l0_index() 获取完整 L0 索引，不依赖 MEMORY.md 中的陈旧 [L0] 条目。"
                    "注入内容带 BEGIN/END 标记，可随时移除。"
                ),
                "action": "inject_soul",
                "already_done": False,
            })

        if memory_already_injected:
            recommendations.append({
                "option": "B",
                "label": "MEMORY 已注入",
                "description": "MEMORY.md 已包含记忆框架指引，无需操作。",
                "action": "none",
                "already_done": True,
            })
        else:
            recommendations.append({
                "option": "B",
                "label": "写入 MEMORY.md（轻量）",
                "description": (
                    "在 MEMORY.md 中写入一条简短指引（约 200 字符），"
                    "agent 看到后会调用 get_l0_index()。不改动 SOUL.md。"
                ),
                "action": "inject_memory",
                "already_done": False,
            })

        # v2.5.0+: Dashboard plugin option
        from .dashboard_plugin import check_dashboard_plugin_status, deploy_dashboard_plugin
        dashboard_status = check_dashboard_plugin_status(agent_info["home_dir"])
        if dashboard_status["status"] == "not_installed":
            recommendations.append({
                "option": "C",
                "label": "安装 Dashboard 插件",
                "description": (
                    "在 Hermes Dashboard 中安装可视化面板，展示四层记忆架构、"
                    "知识生命周期与健康诊断。安装后重启 Dashboard 生效。"
                ),
                "action": "install_dashboard",
                "already_done": False,
            })
        elif dashboard_status["status"] == "up_to_date":
            recommendations.append({
                "option": "C",
                "label": "Dashboard 插件已安装",
                "description": f"Dashboard 插件 v{dashboard_status['version']} 已安装并是最新版本。",
                "action": "none",
                "already_done": True,
            })
        elif dashboard_status["status"] == "update_available":
            recommendations.append({
                "option": "C",
                "label": "Dashboard 插件需更新",
                "description": (
                    f"已安装 v{dashboard_status['version']}，期望 v{dashboard_status['expected_version']}。"
                    f"请使用 Hermes 协助解决版本冲突。"
                ),
                "action": "install_dashboard",  # Will show version conflict in result
                "already_done": False,
            })

    else:
        recommendations.append({
            "option": "manual",
            "label": "手动配置（通用 MCP agent）",
            "description": (
                "请将以下 snippet 添加到你的 agent 的 system prompt 或 persona 中：\n\n"
                f"{MEMORY_SNIPPET}"
            ),
            "action": "manual",
            "snippet": MEMORY_SNIPPET,
        })

    # Always offer skip
    recommendations.append({
        "option": "skip",
        "label": "跳过",
        "description": "不做任何注入，稍后手动配置。",
        "action": "skip",
        "already_done": False,
    })

    return {
        "agent_type": agent_type,
        "version": version,
        "soul_injected": soul_already_injected,
        "memory_injected": memory_already_injected,
        "recommendations": recommendations,
    }

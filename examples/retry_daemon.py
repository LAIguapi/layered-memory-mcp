#!/usr/bin/env python3
"""
Retry Daemon for Hermes Cron Jobs (script 版)
===============================================
作为 Hermes cron job 的 pre-check script 运行。

工作方式：
  1. 扫描 jobs.json 找 last_status="error" 的任务
  2. 检查 agent.log 中是否有模型调用失败
  3. 对失败任务调用 `hermes cron run` 重新触发（非阻塞）
  4. 记录重试状态到 ~/.task-resilience/
  5. 输出 JSON 控制后续行为：
     - 无失败 → {"wakeAgent": false}（跳过 agent，省 token）
     - 有重试动作 → 报告给 agent（用于日志/通知）
     - 有 BLOCKED → 报告给 agent（用于发通知）

用法（作为 Hermes cron job 的 script）：
  hermes cron create \
    --name "Retry Daemon" \
    --schedule "*/5 * * * *" \
    --script "retry_daemon.py" \
    --prompt "..."

状态管理：
  python3 ~/.hermes/scripts/retry_daemon.py --status
  python3 ~/.hermes/scripts/retry_daemon.py --once
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
AGENT_LOG = HERMES_HOME / "logs" / "agent.log"
STATE_DIR = Path.home() / ".task-resilience"
STATE_DIR.mkdir(parents=True, exist_ok=True)

MAX_RETRIES = 5
BACKOFF_MINUTES = [5, 15, 30, 60, 120]

# BLOCKED 任务通知冷却期（小时），已通知过的任务在此时间内不再重复告警
BLOCKED_NOTIFY_COOLDOWN_HOURS = int(os.environ.get("BLOCKED_NOTIFY_COOLDOWN_HOURS", "24"))

FAILURE_PATTERNS = [
    r"HTTP 429",
    r"HTTP 5[0-9]{2}",
    r"rate.?limit",
    r"overloaded",
    r"API call failed",
    r"connection.*(?:reset|refused|timeout)",
    r"ReadTimeout",
    r"ConnectionError",
    r"timeout",
]


# ── 状态管理 ──────────────────────────────────────────

def state_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def load_state(job_id: str) -> dict:
    p = state_path(job_id)
    if p.exists():
        return json.loads(p.read_text())
    return {
        "job_id": job_id,
        "status": "NEW",
        "retry_count": 0,
        "max_retries": MAX_RETRIES,
        "last_error": None,
        "detected_at": None,
        "next_retry_at": None,
        "history": [],
    }


def save_state(state: dict):
    p = state_path(state["job_id"])
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def clear_state(job_id: str):
    p = state_path(job_id)
    if p.exists():
        p.unlink()


# ── 失败检测 ──────────────────────────────────────────

def load_jobs() -> list:
    if not JOBS_FILE.exists():
        return []
    data = json.loads(JOBS_FILE.read_text())
    return data.get("jobs", [])


def find_failed_jobs() -> list[dict]:
    """
    双重检测：
    1. jobs.json last_status == "error" + 模型调用失败关键词
    2. last_status == "ok" 但 agent.log 中有静默失败
    """
    jobs = load_jobs()
    failed = []

    for job in jobs:
        job_id = job["id"]
        last_status = job.get("last_status")
        last_error = job.get("last_error") or ""
        last_run = job.get("last_run_at")

        if not last_run:
            continue

        # Skip jobs the user has deliberately disabled/paused — never retry
        # or alarm on them (otherwise a paused job with last_status=error
        # generates recurring false BLOCKED notifications forever).
        if job.get("enabled") is False or job.get("state") in ("paused", "disabled"):
            clear_state(job_id)
            continue

        # 条件1: 显式失败
        if last_status == "error":
            is_model_err = _is_model_failure(last_error)
            failed.append({
                "job": job,
                "reason": "model_error" if is_model_err else "unknown_error",
                "error": last_error,
                "last_run": last_run,
            })
            continue

        # 条件2: 静默失败（status=ok 但 log 有错）
        if last_status == "ok":
            try:
                run_time = datetime.fromisoformat(last_run)
                now = datetime.now(run_time.tzinfo)
                if now - run_time < timedelta(minutes=30):
                    log_errors = _check_agent_log(job_id, last_run)
                    if log_errors:
                        failed.append({
                            "job": job,
                            "reason": "silent_model_error",
                            "error": "; ".join(log_errors[:3]),
                            "last_run": last_run,
                        })
            except (ValueError, TypeError):
                pass

    return failed


def _is_model_failure(error_msg: str) -> bool:
    if not error_msg:
        return False
    for pattern in FAILURE_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return True
    return False


def _check_agent_log(job_id: str, run_time: str) -> list[str]:
    if not AGENT_LOG.exists():
        return []
    # Parse run_time threshold to filter out old log entries
    try:
        threshold = datetime.fromisoformat(run_time)
    except (ValueError, TypeError):
        threshold = None
    errors = []
    try:
        with open(AGENT_LOG, "r") as f:
            for line in f:
                if job_id in line and any(kw in line for kw in ["ERROR", "429", "failed"]):
                    # Skip entries before the run_time threshold
                    if threshold:
                        try:
                            # Log format: "2026-04-30 11:35:23,358 ..."
                            log_time_str = line[:23]
                            log_time = datetime.strptime(log_time_str, "%Y-%m-%d %H:%M:%S,%f")
                            # Make threshold naive if log_time is naive
                            if log_time.tzinfo is None and threshold.tzinfo is not None:
                                threshold = threshold.replace(tzinfo=None)
                            if log_time < threshold:
                                continue
                        except (ValueError, IndexError):
                            pass  # Can't parse timestamp, include the line
                    for pattern in FAILURE_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            errors.append(line.strip()[:200])
                            break
    except Exception:
        pass
    return errors


# ── 重试 ──────────────────────────────────────────────

def should_retry(state: dict) -> bool:
    if state["status"] in ("BLOCKED", "COMPLETED"):
        return False
    if state["retry_count"] >= state["max_retries"]:
        return False
    if state.get("next_retry_at"):
        try:
            next_time = datetime.fromisoformat(state["next_retry_at"])
            if datetime.now(next_time.tzinfo) < next_time:
                return False
        except (ValueError, TypeError):
            pass
    return True


def trigger_retry(job_id: str) -> bool:
    """hermes cron run 是非阻塞的，只设 next_run_at"""
    try:
        result = subprocess.run(
            ["hermes", "cron", "run", job_id, "--accept-hooks"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"trigger failed for {job_id}: {e}", file=sys.stderr)
        return False


def compute_next_retry(retry_count: int) -> str:
    idx = min(retry_count, len(BACKOFF_MINUTES) - 1)
    wait = BACKOFF_MINUTES[idx]
    return (datetime.now() + timedelta(minutes=wait)).isoformat()


# ── 主逻辑（script 模式）───────────────────────────────

def scan_and_retry() -> dict:
    """
    核心扫描。返回 report dict：
    - no_failures → {"wakeAgent": false}
    - has_retries → report 给 agent 处理通知
    - has_blocked → report 给 agent 处理通知
    """
    report = {
        "scanned_at": datetime.now().isoformat(),
        "failed_found": 0,
        "retries_triggered": [],
        "blocked": [],
        "recovered": [],
    }

    failed_jobs = find_failed_jobs()
    report["failed_found"] = len(failed_jobs)

    if not failed_jobs:
        # 清理已完成的状态文件
        for sf in STATE_DIR.glob("*.json"):
            try:
                s = json.loads(sf.read_text())
                if s["status"] == "COMPLETED":
                    sf.unlink()
            except Exception:
                pass
        return report

    for entry in failed_jobs:
        job = entry["job"]
        job_id = job["id"]
        job_name = job.get("name", job_id)
        error = entry["error"] or "unknown"

        state = load_state(job_id)

        # 检查是否已恢复（RETRYING 或 BLOCKED 状态但 job 已经成功）
        if job.get("last_status") == "ok" and state["status"] in ("RETRYING", "BLOCKED"):
            report["recovered"].append({"id": job_id[:8], "name": job_name})
            state["status"] = "COMPLETED"
            save_state(state)
            continue

        if state["status"] == "BLOCKED":
            # 检查是否在通知冷却期内
            last_notified = state.get("last_notified_at")
            if last_notified:
                try:
                    notified_time = datetime.fromisoformat(last_notified)
                    cooldown = timedelta(hours=BLOCKED_NOTIFY_COOLDOWN_HOURS)
                    if datetime.now(notified_time.tzinfo) - notified_time < cooldown:
                        # 冷却期内，跳过报告
                        continue
                except (ValueError, TypeError):
                    pass
            report["blocked"].append({
                "id": job_id[:8], "name": job_name,
                "retries": state["retry_count"], "error": state.get("last_error"),
            })
            # 记录通知时间
            state["last_notified_at"] = datetime.now().isoformat()
            save_state(state)
            continue

        if state["status"] == "NEW":
            state["status"] = "RETRYING"
            state["detected_at"] = datetime.now().isoformat()

        if not should_retry(state):
            if state["retry_count"] >= state["max_retries"]:
                state["status"] = "BLOCKED"
                save_state(state)
                report["blocked"].append({
                    "id": job_id[:8], "name": job_name,
                    "retries": state["retry_count"], "error": error,
                })
            continue

        # 执行重试
        state["retry_count"] += 1
        success = trigger_retry(job_id)

        state["history"].append({
            "time": datetime.now().isoformat(),
            "attempt": state["retry_count"],
            "result": "triggered" if success else "trigger_failed",
            "error": error,
        })
        state["last_error"] = error
        state["next_retry_at"] = compute_next_retry(state["retry_count"]) if success else None
        save_state(state)

        report["retries_triggered"].append({
            "id": job_id[:8], "name": job_name,
            "attempt": f"{state['retry_count']}/{state['max_retries']}",
            "next_retry": state.get("next_retry_at"),
        })

    return report


# ── CLI 入口 ──────────────────────────────────────────

def main():
    # --status: 显示当前重试状态
    if "--status" in sys.argv:
        print("\n📋 Retry Daemon Status")
        print("=" * 50)
        states = list(STATE_DIR.glob("*.json"))
        if not states:
            print("  No retry states. All clean.")
        for sf in sorted(states):
            try:
                s = json.loads(sf.read_text())
                icon = {"NEW": "🆕", "RETRYING": "🔄", "BLOCKED": "🚫", "COMPLETED": "✅"}.get(s["status"], "?")
                print(f"  {icon} {s['job_id'][:8]} | {s['status']} | {s['retry_count']}/{s['max_retries']}")
                if s.get("last_error"):
                    print(f"     err: {s['last_error'][:80]}")
            except Exception as e:
                print(f"  ? {sf.name} ({e})")

        # 顺便显示当前失败的 cron jobs
        failed = find_failed_jobs()
        if failed:
            print(f"\n⚠️ Failed cron jobs:")
            for e in failed:
                print(f"  • {e['job'].get('name', '?')} | {e['reason']} | {e['error'][:60]}")
        return

    # --once: 单次扫描（调试用）
    if "--once" in sys.argv:
        report = scan_and_retry()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ── script 模式（被 Hermes cron 调用）──
    report = scan_and_retry()

    if report["failed_found"] == 0:
        # 无失败 → 告诉 Hermes 跳过 agent，省 token
        print('{"wakeAgent": false}')
        return

    # 有重试动作或 BLOCKED → 输出报告给 agent，让 agent 决定是否通知
    print("## Retry Daemon 扫描报告")
    print(f"扫描时间: {report['scanned_at']}")
    print(f"发现失败任务: {report['failed_found']}")

    if report["retries_triggered"]:
        print(f"\n### 已触发重试 ({len(report['retries_triggered'])})")
        for r in report["retries_triggered"]:
            print(f"- **{r['name']}** ({r['id']}) — 第 {r['attempt']} 次重试, 下次检查: {r.get('next_retry', 'N/A')}")

    if report["blocked"]:
        print(f"\n### ⚠️ 已 BLOCKED ({len(report['blocked'])})")
        print("以下任务重试超限，需要手动介入：")
        for b in report["blocked"]:
            print(f"- **{b['name']}** ({b['id']}) — 已重试 {b['retries']} 次, 错误: {b.get('error', 'unknown')[:80]}")
        print("\n请通过 hermes send 或 email 通知用户。")

    if report["recovered"]:
        print(f"\n### ✅ 已恢复 ({len(report['recovered'])})")
        for r in report["recovered"]:
            print(f"- {r['name']} ({r['id']})")


if __name__ == "__main__":
    main()

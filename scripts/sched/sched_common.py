"""定时任务共享库：问题日志 / 报告头 / 重试 / 告警（任务 0/1/2 公用）。

约定：
- 问题日志 `logs/issues/YYYY-MM-DD.jsonl`——任何脚本失败/告警都追加至此，
  任务 1（帕累托日报）以此为主要数据源；
- 报告统一头部：任务名 / 调度 / 执行时间 / 状态 / 依赖 / 数据窗口；
- 报告目录 `reports/{bench,daily,weekly}/YYYY-MM-DD(.md)`。
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from fdl_core.srs.time_layer import local_date, now_utc

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS = ROOT / "logs"
REPORTS = ROOT / "reports"


# ── 问题日志（任务 1 的数据源）─────────────────────────────
def log_issue(
    itype: str,
    severity: int,
    message: str,
    *,
    evidence: str = "",
    day: str | None = None,
) -> Path:
    """记录一条系统问题（severity 1-5，5 最重）。返回 jsonl 路径。"""
    d = day or local_date().isoformat()
    out = LOGS / "issues" / f"{d}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": fmt_now(),
        "type": itype,
        "severity": severity,
        "message": message,
        "evidence": evidence,
        "resolved": False,
    }
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


def read_issues(day: str) -> list[dict]:
    """读取某自然日的全部问题记录（文件缺失 = 无问题，返回空表）。"""
    f = LOGS / "issues" / f"{day}.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def mark_resolved(day: str, itype: str, fix_note: str) -> int:
    """把某日某类型的未解决问题标记为已解决（帕累托闭环）。"""
    f = LOGS / "issues" / f"{day}.jsonl"
    if not f.exists():
        return 0
    n = 0
    lines = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == itype and not rec.get("resolved"):
            rec["resolved"] = True
            rec["fix_note"] = fix_note
            n += 1
        lines.append(json.dumps(rec, ensure_ascii=False))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


# ── 重试 ──────────────────────────────────────────────────
def retry(fn, *, attempts: int = 3, delay_sec: float = 10.0, label: str = ""):
    """失败重试：attempts 次、固定间隔；最终失败抛出并记录问题日志。"""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 —— 重试边界必须吞掉一切
            last_err = e
            if i < attempts - 1:
                time.sleep(delay_sec)
    log_issue(
        "task_failure",
        4,
        f"{label or fn} 重试 {attempts} 次仍失败",
        evidence=f"{type(last_err).__name__}: {last_err}",
    )
    raise last_err  # type: ignore[misc]


# ── 报告头部（统一格式）───────────────────────────────────
def render_header(task: str, cron: str, status: str, deps: dict[str, str], window: str) -> str:
    deps_txt = "；".join(f"{k}={v}" for k, v in deps.items()) or "无"
    return (
        f"# {task}\n\n"
        f"> **调度**：{cron} ｜ **状态**：{status} ｜ **执行时间**：{fmt_now()} ｜ "
        f"**数据窗口**：{window}\n>\n> **依赖**：{deps_txt}\n"
    )


def fmt_now() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%SZ")


def week_dates(ref: date | None = None) -> list[str]:
    """返回最近 7 天日期串（含 ref 当日，升序）。"""
    d = ref or local_date()
    return [(d - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

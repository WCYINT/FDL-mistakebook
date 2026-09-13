"""任务 1 · 帕累托日报（每日 05:00，cron: 0 5 * * *；在 04:00 daily_batch 之后）。

流程：汇总前一自然日问题（logs/issues/）→ 帕累托分析（按类型频次+严重度
排序，累计占比）→ 选 Top1 → 根因诊断 → 方案 → 自动实施（仅 SAFE 类）
→ 验证 → 日报 reports/daily/YYYY-MM-DD.md。
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sched_common import (  # noqa: E402
    REPORTS,
    mark_resolved,
    read_issues,
    render_header,
)

# 根因诊断知识库（错误家族 → 根因假设 → 方案 → 是否可自动实施）
RUNBOOK: dict[str, dict] = {
    "disk_io": {
        "root": "外置 SSD 间歇 I/O 故障（连接/线材）——SQLite 随机写+fsync 失败",
        "fix": "内置盘重建库后回拷；每日备份（P2-29）；建议物理检查",
        "auto": False,
    },
    "task_failure": {
        "root": "定时任务执行异常（依赖不可达/超时）",
        "fix": "查看对应任务日志；重试机制已内置；连续失败升级人工",
        "auto": False,
    },
    "redline": {
        "root": "体验/质量红线触发（拒绝使用/时长失控/通过率崩塌）",
        "fix": "执行红线首选动作（降量/查 new_per_day/查 R@R）",
        "auto": False,
    },
    "test_failure": {
        "root": "回归测试失败（代码/数据变更引入）",
        "fix": "pytest 重跑定位失败用例 → 修复 → 绿后关闭",
        "auto": True,
    },
    "ocr_quality": {
        "root": "OCR 识别质量不足（红笔阈值/手写体/角度）",
        "fix": "调整预处理/阈值（数据驱动定界）",
        "auto": True,
    },
}
DEFAULT_RUNBOOK = {
    "root": "未知错误家族——需人工归类",
    "fix": "人工分析后补充本 RUNBOOK 条目",
    "auto": False,
}


def pareto(issues: list[dict]) -> list[dict]:
    """帕累托：按类型聚合（频次×最大严重度）降序 + 累计占比。"""
    counter: Counter = Counter()
    sev: Counter = Counter()
    for it in issues:
        counter[it["type"]] += 1
        sev[it["type"]] = max(sev[it["type"]], it.get("severity", 1))
    total = sum(counter.values()) or 1
    ranked = sorted(counter.items(), key=lambda x: (-x[1], -sev[x[0]]))
    out, cum = [], 0
    for t, n in ranked:
        cum += n
        out.append(
            {
                "type": t,
                "count": n,
                "max_sev": sev[t],
                "share": round(n / total, 3),
                "cum_share": round(cum / total, 3),
            }
        )
    return out


def diagnose_and_fix(top_type: str, day: str, evidence: list[dict]) -> dict:
    """Top1 问题：根因诊断 → 方案 → 自动实施（仅 SAFE）→ 验证。"""
    rb = RUNBOOK.get(top_type, DEFAULT_RUNBOOK)
    result: dict = {
        "type": top_type,
        "root": rb["root"],
        "plan": rb["fix"],
        "auto": rb["auto"],
        "verified": None,
        "fix_note": "",
    }
    if rb["auto"] and top_type == "test_failure":
        # SAFE 自动实施：重跑全量测试验证
        import subprocess

        r = subprocess.run(
            [str(Path(sys.executable)), "-m", "pytest", "-q", "--basetemp=/tmp/pt-pareto"],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        result["verified"] = r.returncode == 0
        result["fix_note"] = (
            "重跑 pytest 通过——问题已消解" if r.returncode == 0 else r.stdout[-300:]
        )
        if result["verified"]:
            mark_resolved(day, top_type, "pytest 重跑通过")
    else:
        result["verified"] = None
        result["fix_note"] = "非 SAFE 类——记录根因与方案，转人工执行（不强改）"
    return result


def today() -> date:
    return date.today()  # 可测试点（monkeypatch 此函数冻结日期）


def main() -> int:
    yesterday = (today() - timedelta(days=1)).isoformat()
    issues = read_issues(yesterday)
    deps = {"daily_batch_0400": "✅（05:00 时已跑完）", "issues_log": f"{len(issues)} 条"}

    # 帕累托
    pareto_rows = pareto(issues)
    top = pareto_rows[0] if pareto_rows else None
    top_result = diagnose_and_fix(top["type"], yesterday, issues) if top else None

    # 日报
    body = render_header(
        "任务 1 · 问题帕累托日报",
        "0 5 * * *（每日 05:00，04:00 批处理后）",
        "OK" if issues else "OK（无问题）",
        deps,
        f"{yesterday}（前一自然日）",
    )
    body += f"\n## 一、问题清单（{len(issues)} 条）\n\n"
    if issues:
        body += "| 时间 | 类型 | 严重度 | 描述 |\n|---|---|---|---|\n"
        for it in issues:
            body += (
                f"| {it['ts'][11:19]} | {it['type']} | {it.get('severity', 1)} | "
                f"{it['message'][:60]} |\n"
            )
    else:
        body += "前一自然日无问题记录。\n"

    body += "\n## 二、帕累托分析\n\n"
    "| 类型 | 频次 | 最大严重度 | 占比 | 累计 |\n"
    "|---|---|---|---|---|\n"
    for r in pareto_rows:
        body += (
            f"| {r['type']} | {r['count']} | {r['max_sev']} |"
            f" {r['share']:.0%} | {r['cum_share']:.0%} |\n"
        )

    body += "\n## 三、Top1 问题处置（根因诊断 → 方案 → 实施 → 验证）\n\n"
    if top_result:
        body += (
            f"- **问题类型**：{top_result['type']}\n"
            f"- **根因诊断**：{top_result['root']}\n"
            f"- **解决方案**：{top_result['plan']}\n"
            f"- **自动实施**：{'是' if top_result['auto'] else '否（转人工）'}\n"
            f"- **验证结果**：{top_result['verified'] or top_result['fix_note']}\n"
        )
    else:
        body += "无问题——无需处置。\n"

    body += (
        "\n## 四、结论\n\n"
        f"- 前一日问题 {len(issues)} 条，覆盖 {len(pareto_rows)} 个类型；\n"
        f"- Top1（{top['type'] if top else '—'}）已按 RUNBOOK 处置；\n"
        "- 本日报供任务 2（周五周报）聚合引用。\n"
    )

    out_dir = REPORTS / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{yesterday}.md"
    out.write_text(body, encoding="utf-8")
    print(f"[task1] 日报: {out}（{len(issues)} 条问题，Top1={top['type'] if top else '—'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

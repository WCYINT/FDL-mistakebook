"""任务 2 · 周报与流程迭代（每周五 20:00，cron: 0 20 * * 5）。

聚合本周：问题数量/类型分布/解决率/遗留风险 + 引用任务0 对标 + 任务1 日报
→ 流程改进建议。失败自动顺延次日重试（launchd 每日 20:00 触发，脚本内
判断"本周未成功才执行"——等效顺延），上限 3 次（周五/六/日/周一），
超限 → 告警；依赖缺失 → 降级运行（标记缺失，不推测）。
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
    log_issue,
    read_issues,
    render_header,
)

MAX_ATTEMPTS = 3  # 顺延上限：周五/六/日/周一共 4 个触发点，3 次重试
ISO_WEEK = 5  # Friday


def week_done(year: int, week: int) -> bool:
    """本周报告已成功生成（幂等：已成功则不再执行）。"""
    marker = REPORTS / "weekly" / f"{year}-W{week:02d}.md"
    return marker.exists() and "状态**：OK" in marker.read_text(encoding="utf-8")


def attempt_count(year: int, week: int) -> int:
    """本周已尝试次数（weekly 目录内 DEGRADED/FAILED 标记计数）。"""
    n = 0
    for p in (REPORTS / "weekly").glob(f"{year}-W{week:02d}*.md"):
        if "状态**：FAILED" in p.read_text(encoding="utf-8"):
            n += 1
    return n


def today() -> date:
    return date.today()  # 可测试点


def main() -> int:
    now_d = today()
    year, week, _ = now_d.isocalendar()
    if week_done(year, week):
        print("[task2] 本周报告已生成——幂等跳过")
        return 0
    attempts = attempt_count(year, week)
    if attempts >= MAX_ATTEMPTS:
        log_issue(
            "task2_failure",
            5,
            f"周报 {year}-W{week:02d} 顺延 {attempts} 次仍失败——停止重试，转人工",
        )
        print(f"[task2] 顺延 {attempts} 次仍失败——告警已写，转人工", file=sys.stderr)
        return 1

    # 依赖收集（缺失 → 降级标记，不推测）
    deps: dict[str, str] = {}
    bench = sorted((REPORTS / "bench").glob("*.md"))
    deps["task0_对标报告"] = bench[-1].name if bench else "缺失（降级）"
    daily_week = sorted(
        p
        for p in (REPORTS / "daily").glob("*.md")
        if (date.today() - date.fromisoformat(p.stem)).days <= 7
    )
    deps["task1_本周日报"] = f"{len(daily_week)} 份"

    # 本周问题聚合
    issues: list[dict] = []
    for i in range(6, -1, -1):
        issues += read_issues((now_d - timedelta(days=i)).isoformat())
    by_type = Counter(x["type"] for x in issues)
    resolved = sum(1 for x in issues if x.get("resolved"))
    solve_rate = round(resolved / len(issues), 2) if issues else None
    risks = [x for x in issues if x.get("severity", 0) >= 4 and not x.get("resolved")]

    status = "OK" if daily_week else ("DEGRADED" if issues or bench else "FAILED")

    body = render_header(
        "任务 2 · 周运行总结与流程迭代",
        "0 20 * * 5（每周五 20:00，失败顺延次日）",
        status,
        deps,
        f"{(now_d - timedelta(days=6)).isoformat()} ~ {now_d.isoformat()}",
    )

    # 一、本周运行
    body += f"\n## 一、本周运行情况\n\n- 问题总数：**{len(issues)}**\n"
    body += f"- 类型分布：{dict(by_type) or '—'}\n"
    body += (
        f"- 解决率：{solve_rate:.0%}（已标记解决 {resolved}/{len(issues)}）\n"
        if solve_rate is not None
        else "- 解决率：—（本周无问题记录）\n"
    )
    body += f"- 遗留风险：{len(risks)} 条（severity≥4 未解决）\n"
    for r in risks:
        body += f"  - [{r['type']}] {r['message'][:70]}\n"

    # 二、引用与迭代
    body += "\n## 二、引用输入\n\n"
    body += f"- 任务0 对标：{deps['task0_对标报告']}（最近一份）\n"
    body += f"- 任务1 日报：{len(daily_week)} 份（本周）\n"

    body += "\n## 三、流程迭代改进建议\n\n"
    suggestions = []
    if bench:
        txt = bench[-1].read_text(encoding="utf-8")
        for line in txt.splitlines():
            if line.startswith("- [") and "可落地的优化点" not in line:
                suggestions.append("对标引入：" + line[3:])
    if by_type:
        top_type, n = by_type.most_common(1)[0]
        if n >= 3:
            suggestions.append(
                f"帕累托引入：本周「{top_type}」出现 {n} 次——建议为其建立"
                "专项 RUNBOOK 条目或增加前置检查"
            )
    if solve_rate is not None and solve_rate < 0.8:
        suggestions.append("解决率低于 80%——建议任务 1 的自动实施范围扩大（评估 SAFE 类）")
    if not suggestions:
        suggestions.append("本周无结构性改进项——维持现有流程（帕累托/对标/自动化范围）")
    for x in suggestions:
        body += f"- {x}\n"

    body += (
        "\n## 四、结论\n\n"
        f"- 周报状态 {status}（尝试第 {attempts + 1} 次，上限 {MAX_ATTEMPTS} 次顺延）；\n"
        "- 改进建议为建议清单——不自动改流程，供 King 审阅后纳入批次计划。\n"
    )

    out_dir = REPORTS / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if status == "OK" else f"-{status}-{attempts + 1}"
    out = out_dir / f"{year}-W{week:02d}{suffix}.md"
    out.write_text(body, encoding="utf-8")
    print(f"[task2] 周报: {out}（{status}，问题 {len(issues)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

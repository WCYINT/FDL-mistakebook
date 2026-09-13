"""任务 0 · 对标检索（每 2 天 05:00，cron: 0 5 */2 * *）。

GitHub 检索与 FDL 类似的开源项目（儿童学习/间隔重复/教育 SRS）→
按 stars+相关度取 TOP 5 → 逐项解析（核心功能/技术架构/性能/可借鉴点）
→ 对标研究报告 reports/bench/YYYY-MM-DD.md。

失败重试 2 次；联网失败 → issues 日志 + 告警（不阻塞任务 1/2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from sched_common import REPORTS, log_issue, render_header, retry  # noqa: E402

API = "https://api.github.com/search/repositories"
QUERIES = [
    "spaced repetition children",
    "kids math learning app",
    "educational srs flashcards",
    " FSRS scheduler",
]
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "FDL-bench"}


def search_github() -> list[dict]:
    """多查询词检索 → 按 stars 去重排序取 TOP 5。"""
    seen: dict[str, dict] = {}
    for q in QUERIES:
        r = requests.get(
            API,
            params={"q": q, "sort": "stars", "per_page": 10},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("items", []):
            name = item["full_name"]
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "stars": item["stargazers_count"],
                    "lang": item.get("language") or "—",
                    "desc": (item.get("description") or "")[:80],
                    "updated": item["pushed_at"][:10],
                    "url": item["html_url"],
                    "topics": ",".join(item.get("topics", [])[:5]),
                }
    ranked = sorted(seen.values(), key=lambda x: -x["stars"])
    return ranked[:5]


def analyse(item: dict) -> str:
    """单项对标分析（规则化提取，映射到 FDL 模块）。"""
    borrow = []
    t = (item["topics"] + " " + item["desc"]).lower()
    if any(k in t for k in ("fsrs", "spaced", "srs", "repetition")):
        borrow.append("间隔调度参数与衰减模型可对照 fdl_core/srs")
    if any(k in t for k in ("kid", "child", "children")):
        borrow.append("儿童友好 UI 措施可对照 fdl/ui 十原则")
    if any(k in t for k in ("ocr", "scan", "math")):
        borrow.append("拍照录入/识别管线可对照 fdl_core/ingest")
    if "offline" in t or item["lang"] == "Swift":
        borrow.append("离线优先架构可对照 NFR-1")
    if not borrow:
        borrow.append("关注其测试与发布流程")
    return (
        f"### {item['name']}（⭐ {item['stars']} · {item['lang']} ·"
        f" 最近更新 {item['updated']}）\n\n"
        f"- **定位**：{item['desc']}\n"
        f"- **技术架构**：主语言 {item['lang']}；主题标签：{item['topics'] or '—'}\n"
        f"- **性能表现**：社区规模 ⭐{item['stars']}；更新频率 {item['updated']}\n"
        f"- **可借鉴点**：{'；'.join(borrow)}\n"
        f"- **链接**：{item['url']}\n"
    )


def main() -> int:
    deps = {"github_api": "可达"}
    try:
        top5 = retry(search_github, attempts=2, delay_sec=10, label="GitHub 检索")
    except Exception as e:  # noqa: BLE001
        log_issue("task0_failure", 4, f"对标检索失败：{e}")
        deps["github_api"] = f"失败（{e}）"
        top5 = []
    status = "OK" if top5 else "FAILED"

    body = render_header(
        "任务 0 · 开源项目对标检索",
        "0 5 */2 * *（每 2 天 05:00）",
        status,
        deps,
        "检索时点快照",
    )
    if top5:
        body += "\n## TOP 5 清单\n\n| # | 项目 | ⭐ | 语言 | 定位 |\n|---|---|---|---|---|\n"
        for i, it in enumerate(top5, 1):
            body += f"| {i} | {it['name']} | {it['stars']} | {it['lang']} | {it['desc']} |\n"
        body += "\n## 对标分析\n\n" + "\n".join(analyse(it) for it in top5)
        body += "\n\n## 汇总：可落地的优化点（供任务 2 周报引用）\n\n"
        for it in top5:
            takeaway = analyse(it).split("可借鉴点**：")[1].split("\n")[0]
        body += f"- [{it['name']}] {takeaway}\n"
    else:
        body += "\n检索失败——见 issues 日志；本报告供任务 2 降级引用（标记依赖缺失）。\n"

    out_dir = REPORTS / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{__import__('datetime').date.today().isoformat()}.md"
    out.write_text(body, encoding="utf-8")
    print(f"[task0] 报告: {out}（{len(top5)} 项，{status}）")
    return 0 if top5 else 1


if __name__ == "__main__":
    sys.exit(main())

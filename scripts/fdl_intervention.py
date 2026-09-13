#!/usr/bin/env python3
"""Pareto 干预分析 CLI（LLM 根因分析 → 措施 → 落库）。

用法
----
    python scripts/fdl_intervention.py --status     # 看最新分析（不调 LLM）
    python scripts/fdl_intervention.py              # 跑分析（默认预演，不写）
    python scripts/fdl_intervention.py --apply      # 跑分析并落库
    python scripts/fdl_intervention.py --evidence   # 只打印证据快照

说明
----
- 分析由 LLM 完成（Pareto 原则），LLM 不可达时不猜、返回错误；
- 落库写 intervention_action（trigger='PARETO_ROOT'），旧的 PENDING 自动 SKIPPED；
- 报告驾驶舱「错因诊断」下方读取最新一条展示。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.mistakes import intervention as iv  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _print_result(r: dict) -> None:
    print(f"\n== Pareto 根因分析（{r.get('analyzed_at')} · {r.get('source_layer')}）==")
    print(f"总括：{r.get('summary')}")
    print(f"主要根因 {r.get('vital_few_count')} 项：")
    for root in r["roots"]:
        print(
            f"\n  [{root['priority']}] {root['diagnosis']}"
            f"  占比 {root['share'] * 100:.1f}% / 累计 {root['cumulative'] * 100:.1f}%"
        )
        print(f"      证据：{root['evidence']}")
        for a in root["actions"]:
            print(f"      → {a}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pareto 干预分析")
    p.add_argument("--apply", action="store_true", help="落库（缺省只分析不写）")
    p.add_argument("--status", action="store_true", help="打印最新分析（不调 LLM）")
    p.add_argument("--evidence", action="store_true", help="只打印证据快照")
    p.add_argument("--db", default=None, help="库路径")
    args = p.parse_args(argv)

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    try:
        if args.evidence:
            ev = iv.collect_evidence(conn)
            print(json.dumps(ev, ensure_ascii=False, indent=2))
            return 0
        if args.status:
            r = iv.latest_analysis(conn)
            if not r:
                print("[intervention] 尚无 Pareto 分析记录（先跑一次 --apply）")
                return 0
            _print_result(r)
            return 0

        r = iv.pareto_intervention_analysis(conn, write=args.apply)
        if r is None:
            print(
                "[intervention] 分析未完成（LLM 不可达 / 输出不可解析 / 无数据）", file=sys.stderr
            )
            return 1
        _print_result(r)
        if args.apply:
            print(
                f"\n[intervention] 已落库 → intervention_action #{r.get('intervention_id')}"
                f"（报告刷新后可见）"
            )
        else:
            print("\n[intervention] 预演完毕（未落库）。加 --apply 正式写入。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

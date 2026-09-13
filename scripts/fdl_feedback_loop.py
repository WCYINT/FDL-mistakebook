#!/usr/bin/env python3
"""反馈层闭环方案 B CLI（LLM 断点识别 → 个性化干预 → 落库 / 回标）。

用法
----
    python scripts/fdl_feedback_loop.py --list          # 最近 10 条 FEEDBACK_LOOP
    python scripts/fdl_feedback_loop.py --backfill      # 回填历史反馈（真跑 LLM）
    python scripts/fdl_feedback_loop.py --backfill --limit 20
    python scripts/fdl_feedback_loop.py --done 12       # 幂等标 DONE

说明
----
- 分析由 LLM 完成（断点识别 + 个性化干预）；LLM 不可达时不写库、不猜；
- 落库写 intervention_action（trigger='FEEDBACK_LOOP'），同题旧 PENDING 自动 SKIPPED；
- 报告驾驶舱「反馈层闭环」下方展示待办并支持一键回标 DONE。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.mistakes import feedback_loop as fl  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _cmd_list(conn) -> int:
    rows = fl.recent_actions(conn, limit=10)
    if not rows:
        print("[feedback_loop] 尚无 FEEDBACK_LOOP 记录")
        return 0
    print(f"== 最近 {len(rows)} 条反馈闭环干预 ==")
    for r in rows:
        br = (r.get("breakpoint") or "").strip() or "（无断点）"
        print(
            f"  #{r['intervention_id']} [{r['status']}] mistake={r.get('mistake_id')}"
            f" needs_review={'是' if r.get('needs_review') else '否'}"
            f" · {br[:60]}"
        )
    return 0


def _cmd_backfill(conn, limit: int) -> int:
    stats = fl.backfill_feedback_loop(conn, limit=limit)
    print(
        "[feedback_loop] 回填汇总："
        f"处理 {stats['processed']} · 写入 {stats['written']}"
        f" · 跳过 {stats['skipped']} · 失败 {stats['failed']}"
    )
    return 0


def _cmd_done(conn, iid: int) -> int:
    res = fl.mark_done(conn, iid)
    if res.get("not_found"):
        print(f"[feedback_loop] intervention id={iid} 不存在", file=sys.stderr)
        return 1
    if res.get("already"):
        print(f"[feedback_loop] #{iid} 已是 DONE（幂等）")
        return 0
    print(f"[feedback_loop] #{iid} 已标记 DONE")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="反馈层闭环（LLM 断点 → 个性化干预）")
    p.add_argument("--list", action="store_true", help="列出最近 10 条 FEEDBACK_LOOP")
    p.add_argument("--backfill", action="store_true", help="回填历史反馈（真跑 LLM）")
    p.add_argument("--limit", type=int, default=50, help="回填条数上限（默认 50）")
    p.add_argument("--done", type=int, default=None, metavar="ID", help="把干预动作标 DONE")
    p.add_argument("--db", default=None, help="库路径")
    args = p.parse_args(argv)

    if not (args.list or args.backfill or args.done is not None):
        p.print_help()
        return 0

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    try:
        if args.done is not None:
            return _cmd_done(conn, args.done)
        if args.backfill:
            return _cmd_backfill(conn, args.limit)
        return _cmd_list(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""显式执行「ASR 草稿 → 匹配错题 → mark_reviewed」写动作。

🔴 设计约束（E2E 修复 · 问题 1）：
- 报告聚合（scripts/generate_report.collect_metrics）是**只读视图**，绝不写库。
- 本脚本是**唯一**显式写入口：把录音草稿匹配到的错题调用 mark_reviewed
  递增 reappear_count + 写 review_schedule。

命令行：
    python scripts/apply_asr_reviews.py                 # 默认 --dry-run（只打印，不写库）
    python scripts/apply_asr_reviews.py --apply         # 真正写库
    python scripts/apply_asr_reviews.py --apply --date 2026-09-09

安全默认：--dry-run 开启，必须显式传 --apply 才写库。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 路径引导：保证项目根与 scripts/ 都在 sys.path，便于 `python scripts/xxx.py` 直跑
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# 以下导入依赖上方 sys.path 引导，故置于其后
from generate_report import _load_review_audio_analyses  # noqa: E402

from fdl_core.mistakes.review import mark_reviewed  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402
from fdl_core.srs.asr_integration import batch_apply  # noqa: E402


def _build_mistake_lookup(conn: sqlite3.Connection) -> dict[int, dict]:
    lookup: dict[int, dict] = {}
    for row in conn.execute("SELECT id, kp_id, note_id, source_ref FROM mistake_record").fetchall():
        lookup[row[0]] = {
            "kp_id": row[1],
            "note_id": row[2] or "",
            "source_ref": row[3] or "",
        }
    return lookup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式应用 ASR 复习草稿到错题本（默认 dry-run）")
    parser.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="真正写库；缺省为 --dry-run（只打印将标记的错题 id，不写库）",
    )
    parser.add_argument(
        "--date",
        dest="date",
        default=None,
        help="复习日 YYYY-MM-DD（传给 mark_reviewed；默认本地今天）",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        help="数据库路径（默认 fdl_core.paths 主库）",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    db_path = args.db or str(get_paths().primary_db_path)
    # 写入口更需要 WAL + FK + busy_timeout（裸连有并发写损坏风险）
    from fdl_core.db.schema import get_connection

    conn = get_connection(db_path)

    audio_analyses = _load_review_audio_analyses()
    mistake_lookup = _build_mistake_lookup(conn)

    if dry_run:
        results = batch_apply(audio_analyses, mistake_lookup)  # 只读匹配
        matched = [r["mid"] for r in results if r.get("mid") is not None]
        unmatched = [r["audio"] for r in results if r.get("mid") is None]
        print(
            f"[DRY-RUN] 共扫描 {len(audio_analyses)} 份录音草稿，匹配到 "
            f"{len(matched)} 个错题：{matched}"
        )
        if unmatched:
            print(f"[DRY-RUN] 未匹配 {len(unmatched)} 份：{unmatched}")
        print("[DRY-RUN] 未写库。如需应用请加 --apply。")
        conn.close()
        return 0

    # === 真正写库 ===
    def _mark(mid: int) -> bool:
        try:
            return len(mark_reviewed(conn, [mid], day=args.date)) > 0
        except Exception as exc:  # 单条失败不影响其它
            print(f"[WARN] mark_reviewed({mid}) 失败：{exc}")
            return False

    results = batch_apply(audio_analyses, mistake_lookup, _mark)
    matched = [r["mid"] for r in results if r.get("mid") is not None]
    applied = [
        r["mid"] for r in results if r.get("mid") is not None and r.get("new_status") == "已复习"
    ]
    failed = [
        r["mid"]
        for r in results
        if r.get("mid") is not None and r.get("new_status") == "mark_failed"
    ]
    print(
        f"[APPLY] 扫描 {len(audio_analyses)} 份草稿，匹配 {len(matched)} 个错题；"
        f"实际更新 {len(applied)} 条，失败 {len(failed)} 条。"
    )
    if applied:
        print(f"[APPLY] 已标记错题 id：{applied}")
    if failed:
        print(f"[APPLY] 失败错题 id：{failed}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

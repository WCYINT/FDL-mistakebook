#!/usr/bin/env python3
"""Phase 2 批次改写 CLI（2026-09-12 · King 需求）。

三个幂等批次操作（建议按依赖顺序执行）：

    1. --domain     领域术语课标化：数与运算 → 数与代数（DB + 卡片双侧）
    2. --olympiad   奥数卡学期桶：grade_term 统一"小A" + 移入 01-知识点/小A/
    3. --backfill   卡片域回填：卡片缺 knowledge_domain 时从 DB 回填（仅填空）

用法
----
    # 全预演（不写任何东西，先看会发生什么）
    python scripts/kp_batch_rewrite.py --all --dry-run

    # 正式执行三项
    python scripts/kp_batch_rewrite.py --all

    # 单独执行某一项
    python scripts/kp_batch_rewrite.py --domain
    python scripts/kp_batch_rewrite.py --olympiad --subject MATH
    python scripts/kp_batch_rewrite.py --backfill --subject MATH

安全
----
- 正式执行前：主库 WAL checkpoint + 整库备份 `<db>.bak-phase2-<ts>`；
- 受影响卡片备份到 `backups/kp-cards-phase2-<ts>/`（平铺按文件名）；
- 所有操作幂等：重复执行不产生重复写入/移动。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.notes import kp_maintenance as km  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _report_domain(r: dict, dry: bool) -> None:
    tag = "预演" if dry else "完成"
    print(
        f"[domain] {tag}：DB 改写 {r['db_updated']} 条"
        f" | 卡片扫描 {r['cards_scanned']} 张，改写 {len(r['cards_updated'])} 张"
    )
    for it in r["db_items"][:5]:
        print(f"    DB   #{it['id']} {it['code']}: {it['from']} → {it['to']}")
    for it in r["cards_updated"][:5]:
        print(f"    CARD {Path(it['path']).name}: {it['from']} → {it['to']}")
    for e in r["errors"][:5]:
        print(f"    ERR {e}")


def _report_olympiad(r: dict, dry: bool) -> None:
    tag = "预演" if dry else "完成"
    print(
        f"[olympiad] {tag}：判定奥数 {len(r['detected'])} 条"
        f" | 移动 {len(r['moved'])} | 已就位 {len(r['already'])}"
        f" | 缺卡 {len(r['missing'])}"
    )
    for code in r["detected"]:
        print(f"    OLY {code}")
    for it in r["moved"]:
        print(
            f"    MOVE {it['code']}: {Path(it['from']).parent.name}/"
            f"{Path(it['from']).name} → {Path(it['to']).parent.name}/"
            f"{Path(it['to']).name}"
        )
    for code in r["missing"]:
        print(f"    MISS {code}（DB 有 KP 但无卡片 → 可先 --export 补卡）")
    for e in r["errors"][:5]:
        print(f"    ERR {e}")


def _report_backfill(r: dict, dry: bool) -> None:
    tag = "预演" if dry else "完成"
    print(
        f"[backfill] {tag}：回填 {len(r['filled'])} 张"
        f" | 已有值保留 {r['kept']} 张"
        f" | DB 也空跳过 {len(r['skipped_no_db'])} 张"
    )
    for it in r["filled"][:8]:
        print(f"    FILL {it['code']} ← {it['domain']}")
    for code in r["skipped_no_db"][:8]:
        print(f"    SKIP {code}（DB 域为空，需先分类）")
    for e in r["errors"][:5]:
        print(f"    ERR {e}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 2 批次改写（幂等 + 可预演）")
    p.add_argument("--db", default=None, help="库路径（默认生产主库）")
    p.add_argument("--subject", default="MATH", help="奥数/回填的学科（默认 MATH）")
    p.add_argument("--domain", action="store_true", help="领域术语课标化（DB+卡片）")
    p.add_argument("--olympiad", action="store_true", help='奥数卡 grade_term → "小A"')
    p.add_argument("--backfill", action="store_true", help="卡片域从 DB 回填（仅填空）")
    p.add_argument("--all", action="store_true", help="三项全做（domain→olympiad→backfill）")
    p.add_argument("--dry-run", action="store_true", help="只预演不写")
    args = p.parse_args(argv)

    do_domain = args.domain or args.all
    do_oly = args.olympiad or args.all
    do_backfill = args.backfill or args.all
    if not (do_domain or do_oly or do_backfill):
        print("用法：--all ｜ --domain ｜ --olympiad ｜ --backfill（可加 --dry-run）")
        return 0

    paths = get_paths()
    db_path = str(args.db or paths.primary_db_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    card_backup = None if args.dry_run else paths.subdir("backups") / f"kp-cards-phase2-{ts}"

    conn = get_connection(db_path)
    try:
        if not args.dry_run and do_domain:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db_bak = f"{db_path}.bak-phase2-{ts}"
            shutil.copyfile(db_path, db_bak)
            print(f"[backup] DB → {db_bak}")
        if card_backup:
            print(f"[backup] 卡片 → {card_backup}")

        if do_domain:
            r = km.normalize_domain_vocabulary(
                conn,
                dry_run=args.dry_run,
                backup_dir=card_backup,
            )
            _report_domain(r, args.dry_run)
        if do_oly:
            r = km.retag_olympiad_cards(
                conn,
                subject_code=args.subject,
                dry_run=args.dry_run,
                backup_dir=card_backup,
            )
            _report_olympiad(r, args.dry_run)
        if do_backfill:
            r = km.backfill_card_domains(
                conn,
                subject_code=args.subject,
                dry_run=args.dry_run,
                backup_dir=card_backup,
            )
            _report_backfill(r, args.dry_run)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""知识点关联边构建 CLI（Phase 2 · 连边构图，2026-09-12）。

把 51 个知识点连成"学习星图"的边（kp_relation）：
    LLM 双跑提议 → 三重校验 → 闸门定档 → 落库 → 人工复核

用法
----
    # 1) 预演全网（不写库；先看 LLM 会提议什么）
    python scripts/kp_relate.py --propose --dry-run

    # 2) 正式构建（各科内部 + 跨科）
    python scripts/kp_relate.py --propose

    # 3) 只建某类
    python scripts/kp_relate.py --propose --scope cross
    python scripts/kp_relate.py --propose --scope within --subject MATH

    # 4) 状态总览（边数按来源/类型、跨科边明细）
    python scripts/kp_relate.py --status

    # 5) 人工复核
    python scripts/kp_relate.py --review                # 列出 LLM_SUGGEST
    python scripts/kp_relate.py --confirm 3,5           # 确认为 HUMAN_VERIFIED
    python scripts/kp_relate.py --drop 7 --dry-run      # 预演删除
    python scripts/kp_relate.py --drop 7

设计原则
--------
- 默认预演精神：--propose 不带 --write 时只预演并停下（防手滑）；
- 所有写入幂等：已有同对边跳过；重复执行不产生重复行；
- 关系类型白名单：SHARED_METHOD / ANALOGY / APPLICATION / CONTRAST。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.notes import kp_relate as kpr  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _scope_label(r: dict) -> str:
    if r["scope"] == "cross":
        return "cross:" + "×".join(r["subjects"])
    return "within:" + "|".join(r["subjects"])


def _report(r: dict, dry: bool) -> None:
    tag = "预演" if dry else "完成"
    print(
        f"[{_scope_label(r)}] {tag}：节点 {r['items']} | LLM 原始 {r['raw_edges']}"
        f" | 去重 {r.get('unique_edges', 0)}"
        f" | 自动 {len(r['auto'])} | 建议 {len(r['suggest'])}"
        f" | 已有跳过 {r['existing']} | 新增 {r['inserted']}"
    )
    if r["dropped"]:
        print(f"    丢弃明细: {r['dropped']}")
    for it in r["auto"]:
        print(
            f"    AUTO    {it['from']} ↔ {it['to']}  {it['type']:<13}"
            f" conf={it['confidence']:.2f}  {it['note'][:70]}"
        )
    for it in r["suggest"]:
        print(
            f"    SUGGEST {it['from']} ↔ {it['to']}  {it['type']:<13}"
            f" conf={it['confidence']:.2f}  {it['note'][:70]}"
        )
    for e in r["errors"][:5]:
        print(f"    ERR {e}")


def cmd_propose(conn, args) -> None:
    dry = args.dry_run or not args.write
    scope = args.scope
    if scope in ("all", "within"):
        reports = kpr.propose_all(
            conn,
            dry_run=dry,
            only_subjects=[args.subject] if (args.subject and scope == "within") else None,
        )
        if scope == "within" and args.subject:
            # only_subjects 已限制 within；去掉自动生成的 cross 报告
            reports = [r for r in reports if r["scope"] == "within"]
    elif scope == "cross":
        # 手工指定两科对（默认取有 KP 的前两科）
        rows = conn.execute(
            "SELECT s.code, COUNT(*) c FROM knowledge_point k"
            " JOIN subject s ON s.id = k.subject_id"
            " GROUP BY s.code HAVING c >= 2 ORDER BY s.sort_order"
        ).fetchall()
        codes = [r[0] for r in rows]
        if len(codes) < 2:
            print("[kp-relate] 不足两科有 KP，无法建跨科边")
            return
        reports = [
            kpr.propose_relations(
                conn,
                scope="cross",
                subject_codes=codes[:2],
                dry_run=dry,
            )
        ]
    else:
        print(f"未知 --scope {scope}")
        return

    for r in reports:
        _report(r, dry)
    if dry:
        print("\n[kp-relate] 预演完毕。确认无误后加 --write 正式执行。")


def cmd_status(conn) -> None:
    st = kpr.edge_stats(conn)
    print("── 关联边状态 ──────────────────────────────────")
    print(f"  总数: {st['total']}")
    print(f"  按来源: {st['by_source'] or '（空）'}")
    print(f"  按类型: {st['by_type'] or '（空）'}")
    print(f"  跨学科边: {st['cross']}")
    for e in st["cross_items"]:
        print(
            f"    [{e['id']}] {e['from_code']}({e['from_subject']})"
            f" ↔ {e['to_code']}({e['to_subject']})"
            f"  {e['relation_type']}  conf={e['confidence']:.2f}"
            f"  src={e['source']}"
        )
    if st["total"]:
        print("\n  全部边：")
        for e in kpr.list_relations(conn, limit=200):
            arrow = "↔"
            print(
                f"    [{e['id']:>3}] {e['from_code']}"
                f" {arrow} {e['to_code']}  {e['relation_type']:<13}"
                f" conf={e['confidence']:.2f}  src={e['source']}"
            )
    print("──────────────────────────────────────────────")


def cmd_review(conn, limit: int) -> None:
    rows = kpr.list_relations(conn, source=kpr.SRC_SUGGEST, limit=limit)
    if not rows:
        print("[kp-relate] 无待复核的 LLM_SUGGEST 边")
        return
    print(f"[kp-relate] 待复核 {len(rows)} 条：\n")
    for e in rows:
        print(
            f"  [{e['id']:>3}] {e['from_code']} ↔ {e['to_code']}"
            f"  {e['relation_type']:<13} conf={e['confidence']:.2f}"
        )
        print(f"        {e['from_name']} ↔ {e['to_name']}")
        print(f"        依据: {e['note']}")
    print("\n  操作：--confirm IDS 确认 ｜ --drop IDS 删除")


def cmd_confirm(conn, ids: list[int]) -> None:
    r = kpr.confirm_relations(conn, ids)
    print(f"[kp-relate] 确认 {r['confirmed']} 条 → HUMAN_VERIFIED")


def cmd_drop(conn, ids: list[int], *, dry: bool) -> None:
    r = kpr.drop_relations(conn, ids, dry_run=dry)
    tag = "预演" if dry else "完成"
    print(f"[kp-relate] {tag}：{'将删除' if dry else '已删除'} {r['dropped']} 条")
    for e in r["items"]:
        print(f"    [{e['id']}] {e['from_code']} ↔ {e['to_code']} {e['relation_type']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="知识点关联边构建（连边构图）")
    p.add_argument("--db", default=None, help="库路径（默认生产主库）")
    p.add_argument("--propose", action="store_true", help="LLM 提议并落库")
    p.add_argument("--write", action="store_true", help="确认写入（不带则只预演）")
    p.add_argument(
        "--scope", default="all", choices=("all", "within", "cross"), help="作用域（默认 all）"
    )
    p.add_argument("--subject", default=None, help="限定 within 的学科（如 MATH）")
    p.add_argument("--dry-run", action="store_true", help="只预演不写")
    p.add_argument("--status", action="store_true", help="边状态总览")
    p.add_argument("--review", action="store_true", help="列出待复核边")
    p.add_argument("--confirm", default=None, help="确认边 ID（逗号分隔）")
    p.add_argument("--drop", default=None, help="删除边 ID（逗号分隔）")
    p.add_argument("--limit", type=int, default=200, help="review 上限")
    args = p.parse_args(argv)

    def _ids(s: str) -> list[int]:
        return [int(x) for x in s.replace(" ", "").split(",") if x]

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    try:
        if args.confirm:
            cmd_confirm(conn, _ids(args.confirm))
        if args.drop:
            cmd_drop(conn, _ids(args.drop), dry=args.dry_run)
        if args.propose:
            cmd_propose(conn, args)
        if args.status or args.review:
            if args.status:
                cmd_status(conn)
            if args.review:
                cmd_review(conn, args.limit)
        if not any([args.propose, args.status, args.review, args.confirm, args.drop]):
            cmd_status(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

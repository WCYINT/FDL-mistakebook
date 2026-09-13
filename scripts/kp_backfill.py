#!/usr/bin/env python3
"""KP 挂载回填（Phase 1）：对 kp_id=0 的错题批量跑 LLM 匹配。

用法
----
    python scripts/kp_backfill.py --dry-run           # 只统计待回填数，不调用 LLM
    python scripts/kp_backfill.py --limit 3           # 先跑 3 条验证
    python scripts/kp_backfill.py                     # 全量回填
    python scripts/kp_backfill.py --db <path>         # 指定库（副本验证常用）

行为
----
- 扫描 mistake_record WHERE kp_id IS NULL OR kp_id=0
- 逐条调 kp_matcher.match_kp_for_mistake（双跑一致闸门内建）
  · 高置信双跑一致 → 自动写 kp_id + 回填计划
  · 其余 → 落 kp_match_proposal 待人工
- 输出 data/kp_backfill_review_<日期>.md 复核清单（人工队列）

安全
----
- 建议先在副本验证：cp 生产库到 /tmp → --db /tmp/xxx.db
- 生产库直跑有闸门保护（低置信不写权威字段），且可重复执行（已挂载的会被跳过）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.mistakes import kp_matcher as kpm  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _pending_ids(conn) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM mistake_record WHERE kp_id IS NULL OR kp_id = 0 ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KP 挂载回填（Phase 1）")
    parser.add_argument("--db", default=None, help="库路径（默认生产主库）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理条数")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不调用 LLM")
    parser.add_argument("--out", default=None, help="复核清单输出路径")
    args = parser.parse_args(argv)

    db_path = args.db or str(get_paths().primary_db_path)
    conn = get_connection(db_path)
    ids = _pending_ids(conn)
    print(f"[kp-backfill] 待回填 {len(ids)} 条（kp_id 为 NULL/0）")
    if args.dry_run:
        print("[kp-backfill] --dry-run：不调用 LLM，退出")
        conn.close()
        return 0
    if args.limit:
        ids = ids[: args.limit]
        print(f"[kp-backfill] --limit {args.limit}：本次处理 {len(ids)} 条")

    client = kpm.get_default_client()
    results: list[dict] = []
    auto = proposed = failed = 0
    for i, mid in enumerate(ids, 1):
        r = kpm.match_kp_for_mistake(conn, mid, client=client)
        st = r.get("status")
        if st == kpm.STATUS_AUTO:
            auto += 1
        elif st == kpm.STATUS_PROPOSED:
            proposed += 1
        else:
            failed += 1
        # 取错题信息供清单展示
        row = conn.execute(
            "SELECT source_ref, error_type, diagnosis_type FROM mistake_record WHERE id=?", (mid,)
        ).fetchone()
        results.append(
            {
                "id": mid,
                "status": st,
                "kp_code": r.get("kp_code"),
                "kp_id": r.get("kp_id"),
                "confidence": r.get("confidence"),
                "rationale": (
                    str(r.get("rationale") or "")[:120]
                    or (r["errors"][:1][0] if r.get("errors") else None)
                ),
                "source_ref": row[0] if row else "",
                "old_diag": row[2] if row else "",
                "candidate_id": r.get("candidate_id"),
            }
        )
        flag = (
            "AUTO" if st == kpm.STATUS_AUTO else ("人工" if st == kpm.STATUS_PROPOSED else "失败")
        )
        print(
            f"  [{i}/{len(ids)}] #{mid} → {flag}"
            f" code={r.get('kp_code') or '-'} conf={r.get('confidence')}"
        )

    conn.close()
    print(f"\n[kp-backfill] 完成：自动挂载 {auto} | 转人工 {proposed} | 异常 {failed}")

    # 复核清单
    out = (
        Path(args.out)
        if args.out
        else (ROOT / "data" / f"kp_backfill_review_{datetime.now():%Y-%m-%d}.md")
    )
    lines = [
        f"# KP 挂载回填复核清单（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        f"- 处理 {len(ids)} 条 | 自动挂载 **{auto}** | 转人工 **{proposed}** | 异常 {failed}",
        "",
        "## 待人工复核（PROPOSED）",
        "",
        "| 错题 | 题面来源 | LLM 建议 KP | 置信 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r["status"] != kpm.STATUS_PROPOSED:
            continue
        note = ""
        if r["candidate_id"]:
            note = f"另提议新 KP（候选 #{r['candidate_id']}）"
        elif r.get("rationale"):
            note = str(r["rationale"])[:70]
        lines.append(
            f"| #{r['id']} | {str(r['source_ref'])[:34]} | "
            f"{r['kp_code'] or '—'} | {r['confidence']} | {note} |"
        )
    lines += [
        "",
        "## 已自动挂载（AUTO_ACCEPTED）",
        "",
        "| 错题 | 题面来源 | KP code | 置信 |",
        "|---|---|---|---|",
    ]
    for r in results:
        if r["status"] != kpm.STATUS_AUTO:
            continue
        lines.append(
            f"| #{r['id']} | {str(r['source_ref'])[:34]} | {r['kp_code']} | {r['confidence']} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[kp-backfill] 复核清单 → {out}")
    print(json.dumps({"auto": auto, "proposed": proposed, "failed": failed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

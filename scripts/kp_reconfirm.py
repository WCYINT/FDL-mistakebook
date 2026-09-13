#!/usr/bin/env python3
"""复核重跑：回收"复核跑技术性失败"被误判的高置信提案（2026-09-12）。

背景
----
双跑一致闸门的历史实现把"复核跑输出不可解析"（source_layer=L2.A 但 code=null）
与"语义不一致"（两次给出不同 code）同等对待 → 高置信匹配被误送人工队列。
`attribution_engine.CONFIRM_RETRY_MAX` 修复后（复核跑技术性失败自动重试），
本脚本把受影响的提案重跑一遍：

    对每条"r2 技术性失败"的 PROPOSED 提案：
      1. 重新执行 match_kp_for_mistake（修复后的重试逻辑）
      2. 新结果 AUTO_ACCEPTED 且确有挂载 → 旧提案标 SUPERSEDED（保留审计链）
      3. 新结果仍 PROPOSED → 保留旧提案（不动，避免重复条目）
      4. 语义不一致 → 旧提案保留（本来就要人工）

用法
----
    python scripts/kp_reconfirm.py                # 预演（默认）
    python scripts/kp_reconfirm.py --apply        # 正式执行
    python scripts/kp_reconfirm.py --status       # 只看受影响清单
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.mistakes import kp_matcher as kpm  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402
from fdl_core.srs.time_layer import fmt_ts, now_utc  # noqa: E402

STATUS_SUPERSEDED = "SUPERSEDED"


def find_parse_failed(conn) -> list[dict]:
    """找出"r2 属于技术性失败"的 PROPOSED 提案。"""
    out: list[dict] = []
    for pid, mid, code, conf, cj in conn.execute(
        "SELECT id, mistake_id, proposed_kp_code, confidence, confirm_json"
        " FROM kp_match_proposal WHERE status='PROPOSED'"
    ):
        try:
            runs = (json.loads(cj or "{}") or {}).get("runs", [])
        except (ValueError, TypeError):
            runs = []
        if len(runs) < 2:
            continue
        r2 = runs[1]
        # 技术性失败特征：调用成功（L2.A）但 code 为空
        if r2.get("source_layer") == "L2.A" and not r2.get("code"):
            out.append(
                {"proposal_id": pid, "mistake_id": mid, "first_code": code, "first_conf": conf}
            )
    return out


def _supersede(conn, proposal_id: int) -> None:
    conn.execute(
        "UPDATE kp_match_proposal SET status=?, updated_at=? WHERE id=? AND status='PROPOSED'",
        (STATUS_SUPERSEDED, fmt_ts(now_utc()), proposal_id),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="复核重跑（回收技术性失败误判）")
    p.add_argument("--apply", action="store_true", help="正式执行（缺省预演）")
    p.add_argument("--status", action="store_true", help="只看受影响清单")
    p.add_argument("--db", default=None, help="库路径")
    args = p.parse_args(argv)

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    targets = find_parse_failed(conn)
    print(f"[kp-reconfirm] 受影响的 PROPOSED 提案：{len(targets)} 条")
    for t in targets:
        print(
            f"  #{t['proposal_id']} mid={t['mistake_id']}"
            f" first={t['first_code']} conf={t['first_conf']}"
        )
    if args.status or not targets:
        conn.close()
        return 0
    if not args.apply:
        print("\n[kp-reconfirm] 预演完毕。加 --apply 正式执行。")
        conn.close()
        return 0

    client = kpm.get_default_client()
    recovered = still = failed = 0
    for t in targets:
        r = kpm.match_kp_for_mistake(conn, t["mistake_id"], client=client)
        st = r.get("status")
        tag = {kpm.STATUS_AUTO: "AUTO 回收", kpm.STATUS_PROPOSED: "仍转人工", None: "失败"}.get(
            st, str(st)
        )
        print(f"  mid={t['mistake_id']} → {tag} code={r.get('kp_code')} conf={r.get('confidence')}")
        if r.get("errors"):
            for e in r["errors"][:2]:
                print(f"      note: {e}")
        if st == kpm.STATUS_AUTO and r.get("kp_id"):
            _supersede(conn, t["proposal_id"])
            recovered += 1
        elif st == kpm.STATUS_PROPOSED:
            still += 1
        else:
            failed += 1

    # 汇总
    cov = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN kp_id!=0 THEN 1 ELSE 0 END) FROM mistake_record"
    ).fetchone()
    print(f"\n[kp-reconfirm] 完成：回收 {recovered} | 仍人工 {still} | 失败 {failed}")
    print(f"[kp-reconfirm] 挂载率：{cov[1] or 0}/{cov[0]} = {(cov[1] or 0) / cov[0] * 100:.1f}%")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

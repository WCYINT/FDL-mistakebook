#!/usr/bin/env python3
"""挂载提案队列归置（2026-09-13 King #2：「49 条 0.0 分」存量清理）。

背景
----
`kp_match_proposal` 的 PROPOSED 行实为两种语义：
  ① 可复核：有 `proposed_kp_id`（等人工同意/驳回）；
  ② 无法匹配：`proposed_kp_id IS NULL` 且 `confidence=0.0`（LLM 判定现有清单
     覆盖不了，或输出不可解析）——前端此前统一显示"0.0 分"，看起来像低置信，
     实为"无匹配"（0.0 是占位值）。存量 49 条全部为②，且大部分因
     「候选晋级 + 其他路径挂载」而过期，污染"待复核"计数。

做两件事（缺省预演，`--apply` 才写库）
-------------------------------------
A. 作废过期行：无法匹配 **且错题已挂载**（kp_id>0）→ 标 SUPERSEDED
   （decided_by=SYSTEM；rationale 追加说明，保留审计链）。
B. 重匹配未挂载：无法匹配 **且错题仍未挂载** → 重跑 `match_kp_for_mistake`
   （候选晋级后新 KP 已入库，通常可命中）；命中 AUTO 即自动挂载，并作废旧提案。

用法
----
    python scripts/kp_proposals_triage.py                 # 预演（默认）
    python scripts/kp_proposals_triage.py --apply         # 执行 A + B
    python scripts/kp_proposals_triage.py --apply --no-rematch   # 只做 A
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.mistakes import kp_matcher as kpm  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402
from fdl_core.srs.time_layer import fmt_ts, now_utc  # noqa: E402

STATUS_SUPERSEDED = "SUPERSEDED"


def find_no_match_rows(conn) -> list[dict]:
    """找出"无法匹配"的 PROPOSED 行（kp_id 空 + 错题挂载状态）。"""
    rows = []
    for pid, mid, rationale, cur_kp in conn.execute(
        "SELECT p.id, p.mistake_id, p.rationale, m.kp_id"
        " FROM kp_match_proposal p LEFT JOIN mistake_record m ON m.id = p.mistake_id"
        " WHERE p.status='PROPOSED'"
        "   AND (p.proposed_kp_id IS NULL OR p.proposed_kp_id=0)"
        " ORDER BY p.id"
    ):
        rows.append(
            {
                "proposal_id": int(pid),
                "mistake_id": int(mid) if mid is not None else None,
                "rationale": rationale or "",
                "mounted": bool(cur_kp and int(cur_kp) > 0),
            }
        )
    return rows


def _supersede(conn, proposal_id: int, note: str) -> None:
    stamp = fmt_ts(now_utc())
    conn.execute(
        "UPDATE kp_match_proposal SET status=?, decided_by='SYSTEM', decided_at=?,"
        " rationale=substr(COALESCE(rationale,'') || ?, 1, 500), updated_at=?"
        " WHERE id=? AND status='PROPOSED'",
        (STATUS_SUPERSEDED, stamp, f" [归置] {note}", stamp, proposal_id),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="挂载提案队列归置（作废过期 + 重匹配）")
    p.add_argument("--apply", action="store_true", help="正式执行（缺省预演）")
    p.add_argument("--no-rematch", action="store_true", help="只做 A（作废），不做 B（重匹配）")
    p.add_argument("--db", default=None, help="库路径")
    args = p.parse_args(argv)

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    rows = find_no_match_rows(conn)
    stale = [r for r in rows if r["mounted"]]
    todo = [r for r in rows if not r["mounted"]]
    print(
        f"[triage] PROPOSED 无法匹配共 {len(rows)} 条："
        f"过期（错题已挂载）{len(stale)} ｜ 待重匹配 {len(todo)}"
    )

    if not args.apply:
        print("\n[triage] 预演：")
        print(f"  A. 将作废过期提案 {len(stale)} 条 → SUPERSEDED")
        seen = set()
        for r in todo:
            if r["mistake_id"] in seen:
                continue
            seen.add(r["mistake_id"])
            print(
                f"  B. 将重匹配错题 #{r['mistake_id']}"
                f"（提案 #{r['proposal_id']}）：{r['rationale'][:40]}"
            )
        print("\n[triage] 预演完毕。加 --apply 正式执行。")
        conn.close()
        return 0

    # A. 作废过期
    for r in stale:
        _supersede(conn, r["proposal_id"], "错题已由候选晋级/其他路径挂载，本提案作废")
    print(f"[triage] A 完成：作废 {len(stale)} 条")

    if args.no_rematch:
        conn.close()
        return 0

    # B. 重匹配（同一错题只跑一次；命中后其余同题提案一并作废）
    # 🔴 LLM 预检（2026-09-13）：MiniMax 配额耗尽时 match_kp_for_mistake 会把每条
    #    错题写成新的"LLM 不可达"提案（污染队列）→ 先用一次轻量调用探活，
    #    不可用则整段跳过，等配额/网络恢复后重跑本脚本即可。
    client = kpm.get_default_client()
    try:
        client.ask("健康检查", "只回复：ok", timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[triage] LLM 不可用（{type(exc).__name__}: {str(exc)[:80]}）→ 跳过 B 重匹配。"
            "配额恢复后重跑本脚本即可（A 已生效，不受影响）。"
        )
        conn.close()
        return 0

    by_mid: dict[int, list[dict]] = {}
    for r in todo:
        by_mid.setdefault(r["mistake_id"], []).append(r)
    auto = still = 0
    for mid, group in by_mid.items():
        res = kpm.match_kp_for_mistake(conn, mid, client=client)
        st = res.get("status")
        if st == kpm.STATUS_AUTO and res.get("kp_id"):
            auto += 1
            for r in group:
                _supersede(conn, r["proposal_id"], "重匹配命中（候选晋级后 KP 已入库），自动挂载")
            print(f"  #{mid} → AUTO 挂载 kp_id={res.get('kp_id')} code={res.get('kp_code')}")
        elif st == kpm.STATUS_PROPOSED:
            still += 1
            print(f"  #{mid} → 仍无法匹配（保留待人工）: {(res.get('errors') or [''])[0][:60]}")
        else:
            print(f"  #{mid} → 失败/异常：{res.get('errors')}")
    print(f"[triage] B 完成：自动挂载 {auto} 题 ｜ 仍待人工 {still} 题")

    cov = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN kp_id!=0 THEN 1 ELSE 0 END) FROM mistake_record"
    ).fetchone()
    print(f"[triage] 挂载率：{cov[1] or 0}/{cov[0]} = {(cov[1] or 0) / cov[0] * 100:.1f}%")
    left = conn.execute(
        "SELECT COUNT(*) FROM kp_match_proposal WHERE status='PROPOSED'"
    ).fetchone()[0]
    print(f"[triage] 剩余 PROPOSED：{left} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

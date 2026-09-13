#!/usr/bin/env python3
"""KP 候选晋级 CLI（Phase 1 人工闸门操作台）。

用法
----
    # 1) 查看待晋级候选（含证据数、学科、年级、代码解析预览）
    python scripts/kp_promote.py --list

    # 2) 预演（不写库）——先看会发生什么
    python scripts/kp_promote.py --promote 14,20,17 --dry-run

    # 3) 正式晋级
    python scripts/kp_promote.py --promote 14,20,17

    # 4) 驳回（重复/质量差的候选）
    python scripts/kp_promote.py --reject 11,23

    # 4b) 复活被驳回的候选（→ PENDING，可再晋级；驳回历史保留）
    python scripts/kp_promote.py --revive 25,26

    # 5) 修正代码后再晋级（修 schema 格式违规，如含中文的 code）
    python scripts/kp_promote.py --fix-code "29=MATH-G4-CNT-TALLY" --promote 29

    # 6) 全部晋级（谨慎；会打印清单并要求 --yes 确认）
    python scripts/kp_promote.py --promote-all --yes

    # 7) 指定库（副本验证）
    python scripts/kp_promote.py --db /tmp/x.db --list

设计原则
--------
- 默认 dry-run 精神：--promote 不带 --yes 时先打印预演并停下（防手滑）
- subject/grade 自动从 code 解析（MATH-G3-* → MATH/三年级）
- --fix-code 允许在晋级前修正格式违规的 code（写回 kp_candidate）
- 所有操作幂等：已 PROMOTED 的候选不会重复写 knowledge_point
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


def _parse_ids(s: str) -> list[int]:
    return [int(x) for x in s.replace(" ", "").split(",") if x]


def cmd_list(conn) -> None:
    rows = conn.execute(
        "SELECT id, candidate_code, candidate_name, kp_type, evidence_count,"
        " status, substr(rationale,1,80) FROM kp_candidate"
        " WHERE status='PENDING' ORDER BY evidence_count DESC, id"
    ).fetchall()
    if not rows:
        print("[kp-promote] 无待晋级候选")
        return
    print(f"[kp-promote] 待晋级候选 {len(rows)} 个：\n")
    print(f"  {'ID':<4}{'code':<32}{'名称':<20}{'类型':<8}{'证据':<5}{'解析':<16}")
    print("  " + "-" * 88)
    for r in rows:
        cid, code, name, ktype, ev, _st, why = r
        sub, grade = kpm._parse_code_meta(code)
        meta = f"{sub or '?'}/G{grade or '?'}"
        # code 格式检查（schema pattern: ^[A-Z]+(-[A-Z0-9]+)+$）
        import re

        ok = bool(re.match(r"^[A-Z]+(-[A-Z0-9]+)+$", code or ""))
        flag = "" if ok else "  ← code 格式违规"
        print(f"  {cid:<4}{code:<32}{name:<20}{ktype:<8}×{ev:<4}{meta:<16}{flag}")
    print()
    print("  提示：code 格式违规的候选需先用 --fix-code 修正再晋级")
    print("  操作：--promote 14,20,17   ｜   --reject 11,23")


def cmd_fix_code(conn, spec: str) -> int:
    """spec 形如 '29=MATH-G4-CNT-TALLY,26=CHN-G4-POETRY-AUTHOR'"""
    n = 0
    for pair in spec.split(","):
        pair = pair.strip()
        if "=" not in pair:
            print(f"  [skip] 格式错误：{pair}（应为 ID=NEWCODE）")
            continue
        sid, newcode = pair.split("=", 1)
        try:
            cid = int(sid.strip())
        except ValueError:
            print(f"  [skip] ID 非数字：{sid}")
            continue
        import re

        if not re.match(r"^[A-Z]+(-[A-Z0-9]+)+$", newcode.strip()):
            print(f"  [skip] {newcode} 仍不符合格式 ^[A-Z]+(-[A-Z0-9]+)+$")
            continue
        cur = conn.execute(
            "UPDATE kp_candidate SET candidate_code=?, updated_at=datetime('now')"
            " WHERE id=? AND status='PENDING'",
            (newcode.strip(), cid),
        )
        if cur.rowcount:
            print(f"  [fix] 候选 #{cid} code → {newcode.strip()}")
            n += 1
        else:
            print(f"  [skip] 候选 #{cid} 不存在或非 PENDING")
    if n:
        conn.commit()
    return n


def cmd_promote(conn, ids: list[int], *, dry_run: bool, decided_by: str) -> None:
    print(f"[kp-promote] 晋级 {len(ids)} 个候选：{ids}")
    results = []
    for cid in ids:
        row = conn.execute(
            "SELECT candidate_code, candidate_name, status FROM kp_candidate WHERE id=?",
            (cid,),
        ).fetchone()
        if not row:
            print(f"  [skip] #{cid} 不存在")
            continue
        code, name, status = row
        if status != "PENDING":
            print(f"  [skip] #{cid} {code} 状态={status}（非 PENDING）")
            continue
        sub, grade = kpm._parse_code_meta(code)
        if dry_run:
            print(f"  [预演] #{cid} {code} → {name}（{sub or '?'} / 年级 {grade or '?'}）")
            results.append({"id": cid, "code": code, "dry": True})
            continue
        r = kpm.promote_candidate(conn, cid, decided_by=decided_by)
        if r.get("ok"):
            print(
                f"  [OK] #{cid} {code} → knowledge_point #{r['promoted_kp_id']}"
                f"（{r['subject']} / 年级 {r['grade_level']}）"
            )
        else:
            print(f"  [FAIL] #{cid} {code}: {r.get('error')}")
        results.append(r)
    ok = sum(1 for r in results if r.get("ok"))
    if dry_run:
        print(f"\n[kp-promote] 预演完毕：{len(results)} 个将晋级。去掉 --dry-run 正式执行。")
    else:
        print(f"\n[kp-promote] 完成：晋升 {ok} 个")


def cmd_reject(conn, ids: list[int], *, decided_by: str) -> None:
    for cid in ids:
        r = kpm.reject_candidate(conn, cid, decided_by=decided_by)
        tag = "OK" if r.get("ok") else "skip"
        print(f"  [{tag}] 驳回 #{cid}")


def cmd_revive(conn, ids: list[int], *, dry_run: bool) -> None:
    """复活被驳回的候选：REJECTED → PENDING（驳回历史与二审结论保留）。

    用途（2026-09-12）：多科地基落地后，此前因"不属于数学"被驳回的
    语文等候选需要重新纳入 → 先复活，再用 --fix-code/--promote 走正常闸门。
    """
    n = 0
    for cid in ids:
        row = conn.execute(
            "SELECT status, candidate_code, candidate_name FROM kp_candidate WHERE id=?",
            (cid,),
        ).fetchone()
        if not row:
            print(f"  [skip] #{cid} 不存在")
            continue
        status, code, name = row
        if status != "REJECTED":
            print(f"  [skip] #{cid} {code} 状态={status}（仅 REJECTED 可复活）")
            continue
        if dry_run:
            print(f"  [预演] #{cid} {code} {name}：REJECTED → PENDING")
            n += 1
            continue
        conn.execute(
            "UPDATE kp_candidate SET status='PENDING', updated_at=datetime('now')"
            " WHERE id=? AND status='REJECTED'",
            (cid,),
        )
        print(f"  [OK] 复活 #{cid} {code} → PENDING（驳回历史保留）")
        n += 1
    if n:
        conn.commit()
    print(f"[kp-promote] 复活完成：{n} 个" + ("（预演）" if dry_run else ""))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KP 候选晋级 CLI")
    p.add_argument("--db", default=None, help="库路径（默认生产主库）")
    p.add_argument("--list", action="store_true", help="列出待晋级候选")
    p.add_argument("--promote", default=None, help="晋级指定 ID（逗号分隔）")
    p.add_argument("--promote-all", action="store_true", help="晋级全部 PENDING")
    p.add_argument("--reject", default=None, help="驳回指定 ID（逗号分隔）")
    p.add_argument("--revive", default=None, help="复活被驳回的 ID（→PENDING，可再晋级）")
    p.add_argument("--fix-code", default=None, help="晋级前修正 code：'29=NEW-CODE,...'")
    p.add_argument("--dry-run", action="store_true", help="只预演不写库")
    p.add_argument("--yes", action="store_true", help="跳过确认（--promote 不带此参默认预演）")
    p.add_argument("--decided-by", default="FRANK", help="决策人（默认 FRANK）")
    args = p.parse_args(argv)

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    try:
        if args.revive:
            cmd_revive(conn, _parse_ids(args.revive), dry_run=args.dry_run)
        if args.fix_code:
            cmd_fix_code(conn, args.fix_code)
        if args.list:
            cmd_list(conn)
            return 0
        if args.reject:
            cmd_reject(conn, _parse_ids(args.reject), decided_by=args.decided_by)
            return 0
        if args.revive and not (args.promote or args.promote_all):
            return 0  # 仅复活，不继续晋级
        ids: list[int] | None = None
        if args.promote_all:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM kp_candidate WHERE status='PENDING' ORDER BY id"
                )
            ]
            if not args.yes and not args.dry_run:
                print(f"[kp-promote] --promote-all 将晋级 {len(ids)} 个候选：{ids}")
                print("  加 --yes 确认执行，或加 --dry-run 先预演。已停下。")
                return 0
        elif args.promote:
            ids = _parse_ids(args.promote)
        if ids is None:
            print("用法：--list 查看 ｜ --promote IDS ｜ --reject IDS ｜ --promote-all")
            return 0
        # --promote 不带 --yes：先预演（防手滑），提示加 --yes
        dry = args.dry_run or (not args.yes and not args.promote_all)
        cmd_promote(conn, ids, dry_run=dry, decided_by=args.decided_by)
        if dry and not args.dry_run:
            print("  确认无误后加 --yes 正式执行。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

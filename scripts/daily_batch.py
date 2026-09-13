"""04:00 每日批处理入口（MT-02 预聚合 + OPS-06 备份 + VIS 报告刷新）。

launchd/plan：`python scripts/daily_batch.py`（StartInterval 86400 或 StartCalendar 04:00）。
流程：run_daily_batch（R(t) 刷新+时间驱动跃迁+daily_metric 聚合）→ 周聚合 →
SSD 检查 → daily_snapshot（内置盘备份）→ generate_report（报告 HTML 刷新）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fdl_core.db.schema import get_connection

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.metrics.daily import run_daily_batch  # noqa: E402
from fdl_core.metrics.weekly import aggregate_weekly  # noqa: E402
from fdl_core.ops.backup import daily_snapshot, sync_primary_to_mirror  # noqa: E402
from fdl_core.ops.ssd_check import check_ssd  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))


def main() -> int:
    ssd = check_ssd()
    if not ssd.ok:
        print(f"[daily-batch] SSD 异常（{ssd.mode}）——只读模式，跳过写入流程", file=sys.stderr)
        return 1

    p = get_paths()
    # 走 get_connection 以获得 PRAGMA foreign_keys=ON + journal_mode=WAL + busy_timeout=5000
    # （裸 sqlite3.connect 会导致 FK 不生效、并发写 SQLITE_BUSY / 库损坏）
    conn = get_connection(p.primary_db_path)
    summary = run_daily_batch(conn, user_id=1)
    weekly = aggregate_weekly(conn, user_id=1)
    # A3→A2 升级检查（2026-09-12 接线债清偿：a2_upgrade.py:13 与 review.py 的
    # docstring 都写着"应每日 04:00 批处理时调用"，但此调用点从未落过）。
    # 切换结果持久化到 report_meta.scheduler_current，供 fdl_serve 进程经
    # _effective_scheduler() 读取——跨进程生效（原实现只 flip 进程内全局，是假动作）。
    from fdl_core.mistakes.review import run_a2_upgrade_check

    a2 = run_a2_upgrade_check(conn)

    # === kp_state 生产者（方案 B：批处理全量重建，2026-09-13）===
    # 从 mistake_record + review_feedback + review_schedule 全量重算每个 KP 的
    # 状态机与分数并 UPSERT 进 kp_state。幂等（同日重跑无漂移、无重复日志），
    # 不触碰复习热路径。失败不阻断整体批处理（try 包住）。
    kp_rebuild: dict | None = None
    try:
        from fdl_core.srs.kp_state_updater import rebuild_kp_state

        kp_rebuild = rebuild_kp_state(conn, user_id=1)
    except Exception as exc:  # noqa: BLE001 — kp_state 重建失败不阻断主流程
        print(f"[daily-batch] kp_state 重建失败（跳过）：{exc}", file=sys.stderr)

    # === Phase 4 接线（2026-09-12）：知识书 ↔ DB 每日同步 ===
    # 让"新 KP 晋级 → 卡片生成 → 星图/Obsidian 可见"全链自动，无需人工跑 CLI：
    #   ① 四科基线（幂等；缺则建）
    #   ② DB → 卡片（只补缺卡，绝不覆盖人工编辑）
    #   ③ 卡片 → DB（UPDATE + COALESCE，不丢 DB-only 字段）
    # 单科失败不阻断整体批处理（try 包住，逐科容错）。
    kp_sync_summary: list[str] = []
    try:
        from kp_sync import cmd_ensure_subjects, cmd_export, cmd_import

        from fdl_core.notes.kp_classifier import CANONICAL_SUBJECTS

        cmd_ensure_subjects(conn)
        for _code in CANONICAL_SUBJECTS:
            try:
                cmd_export(conn, _code, dry_run=False, force=False)
                cmd_import(conn, _code)
                kp_sync_summary.append(_code)
            except Exception as exc:  # noqa: BLE001 — 单科失败不阻断
                print(f"[daily-batch] KP 同步 {_code} 失败（跳过）：{exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — 整段失败不阻断主流程
        print(f"[daily-batch] KP 知识书同步不可用（跳过）：{exc}", file=sys.stderr)

    conn.close()
    print(f"[daily-batch] 预聚合: {summary} | 周聚合: {weekly}")
    print(
        f"[daily-batch] A2升级检查: ready={a2['ready']} switched={a2['switched']}"
        f" scheduler={a2['scheduler']}"
    )
    print(
        f"[daily-batch] KP 知识书同步: {len(kp_sync_summary)} 科完成"
        f"（{'/'.join(kp_sync_summary) or '无'}）"
    )
    if kp_rebuild is not None:
        print(
            f"[daily-batch] kp_state 重建: {kp_rebuild['kps']} KP"
            f"（新建 {kp_rebuild['created']} / 更新 {kp_rebuild['updated']}"
            f" / 状态变更 {len(kp_rebuild['transitions'])}）"
        )

    out = daily_snapshot(ROOT / "data" / "fdl.db", Path.home() / "FDL-Backup")
    print(f"[daily-batch] 备份: {out}")

    from generate_report import main as gen

    print(f"[daily-batch] 报告: {gen(p.primary_db_path)}")
    # 主库（内置盘）→ 镜像（外置 SSD）每日同步（含写后读校验）
    sync = sync_primary_to_mirror(p.primary_db_path, p.db_path)
    print(f"[daily-batch] 镜像同步: {sync}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

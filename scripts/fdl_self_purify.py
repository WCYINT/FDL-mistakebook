#!/usr/bin/env python3
"""FDL 自我净化体检工具（只读，不修改任何数据）。

逐项暴露数据层的脏数据与僵尸数据，输出可读报告 + 机器可读 JSON。

用法::

    python scripts/fdl_self_purify.py                 # 控制台可读报告
    python scripts/fdl_self_purify.py --json          # 额外写入 data/self_purify_report.json
    python scripts/fdl_self_purify.py --db <path>     # 指定数据库（测试/巡检常用）

硬约束：
- 绝对只读：仅 SELECT，无 INSERT/UPDATE/DELETE。
- 时间统一走 fdl_core.srs.time_layer（禁止 datetime.date.today()）。
- 数据库连接统一走 fdl_core.db.schema.get_connection。
- 单个检测项异常不中断整体。
- 不引入任何新依赖，禁止 emoji。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fdl_core.db.schema import get_connection
from fdl_core.srs.time_layer import LOCAL_TZ, now_utc

# 脚本位于 <fdl_root>/scripts/fdl_self_purify.py → 父目录即项目根（2-FDL）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 严重级别 → 中文标签（控制台用，避免 emoji）。
LEVEL_LABEL = {"high": "高", "warn": "中", "info": "低"}

# error_type 新 4 类口径（含 OTHER 占位）。
_NEW_ERROR_TYPES = ("CONCEPT", "CALC", "MISREAD", "NORM", "OTHER")

# error_type 旧 3 类口径（METHOD/CONFUSION/CARELESS），待迁移到新 4 类。
_LEGACY_ERROR_TYPES = ("METHOD", "CONFUSION", "CARELESS")


def _check_zombie_mistakes(conn):
    """僵尸错题：reappear_count=0 且无任何 DONE 复习记录。"""
    rows = conn.execute(
        """
        SELECT m.id, m.reappear_count, m.subject, m.error_type
        FROM mistake_record m
        WHERE m.reappear_count = 0
          AND NOT EXISTS (
            SELECT 1 FROM review_schedule rs
            WHERE rs.mistake_id = m.id AND rs.status = 'DONE'
          )
        """
    ).fetchall()
    samples = [
        {
            "id": r[0],
            "reappear_count": r[1],
            "subject": r[2],
            "error_type": r[3],
        }
        for r in rows[:5]
    ]
    return len(rows), samples, None


def _check_diagnosis_type_broken(conn):
    """错因断链：error_type 属新口径，但 diagnosis_type 为空/默认 CALC 且 error_type != CALC。"""
    qmarks = ",".join("?" * len(_NEW_ERROR_TYPES))
    rows = conn.execute(
        f"""
        SELECT m.id, m.error_type, m.diagnosis_type, m.subject
        FROM mistake_record m
        WHERE m.error_type IN ({qmarks})
          AND (m.diagnosis_type IS NULL OR m.diagnosis_type = '' OR m.diagnosis_type = 'CALC')
          AND m.error_type != 'CALC'
        """,
        _NEW_ERROR_TYPES,
    ).fetchall()
    samples = [
        {"id": r[0], "error_type": r[1], "diagnosis_type": r[2], "subject": r[3]} for r in rows[:5]
    ]
    return len(rows), samples, None


def _check_kp_unmounted(conn):
    """知识点未挂载：kp_id=0 或 kp_id IS NULL。附挂载率。"""
    total = conn.execute("SELECT COUNT(*) FROM mistake_record").fetchone()[0]
    rows = conn.execute(
        """
        SELECT m.id, m.kp_id, m.subject
        FROM mistake_record m
        WHERE m.kp_id IS NULL OR m.kp_id = 0
        """
    ).fetchall()
    unmounted = len(rows)
    mounted = total - unmounted
    rate = (mounted / total * 100.0) if total else 0.0
    samples = [{"id": r[0], "kp_id": r[1], "subject": r[2]} for r in rows[:5]]
    note = f"知识点挂载率 {rate:.1f}%（已挂载 {mounted} / 总 {total}）"
    return unmounted, samples, note


def _check_orphan_schedules(conn):
    """孤儿复习计划：mistake_id 为空或指向不存在的错题。"""
    rows = conn.execute(
        """
        SELECT rs.id, rs.mistake_id, rs.status
        FROM review_schedule rs
        LEFT JOIN mistake_record m ON m.id = rs.mistake_id
        WHERE rs.mistake_id IS NULL OR m.id IS NULL
        """
    ).fetchall()
    samples = [{"schedule_id": r[0], "mistake_id": r[1], "status": r[2]} for r in rows[:5]]
    return len(rows), samples, None


def _check_broken_image_refs(conn):
    """图片引用缺失：img_original / img_clean 指向不存在的文件。

    背景（2026-09-12 实测）：#77056 的 img_original 指向
    `2-语文/T-听写错题/<hash>.jpg`，该文件从未归档到该目录（同 hash 实际在
    数学档案），复习页图片永远 404。全库扫描 55 条仅此 1 条 → 需要常态化检测。

    路径解析：绝对路径直接用；相对路径按数据根目录拼接
    （与 `/api/image` 的解析口径一致）。只读：仅 SELECT + 文件系统 stat。
    """
    from fdl_core.paths import get_paths

    root = get_paths().root
    broken = []
    rows = conn.execute(
        "SELECT id, subject, source, error_type, img_original, img_clean"
        " FROM mistake_record WHERE img_original IS NOT NULL"
        " OR img_clean IS NOT NULL"
    ).fetchall()
    for mid, subject, _source, etype, orig, clean in rows:
        for field, p in (("img_original", orig), ("img_clean", clean)):
            if not p:
                continue
            abs_p = Path(p) if str(p).startswith("/") else (root / p)
            if not abs_p.exists():
                broken.append(
                    {
                        "mistake_id": mid,
                        "field": field,
                        "path": str(p),
                        "subject": subject,
                        "error_type": etype,
                    }
                )
    note = None
    if broken:
        note = (
            "图片引用缺失会导致复习页原图 404；处置：修正路径指向真实文件、"
            "把文件归档到引用位置，或标记为重复记录后移除。"
        )
    return len(broken), broken[:5], note


def _check_review_queue_backlog(queue_path: Path):
    """待复核积压：读 review_queue.json，统计 status='pending'。

    文件不存在/损坏/非数组时**不报错**，返回 0 并注明原因。
    """
    if not queue_path.exists():
        return 0, [], "review_queue.json 不存在，跳过（计 0）"
    try:
        raw = queue_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 吞掉解析异常，保持只读体检不崩
        return 0, [], f"review_queue.json 解析失败：{type(exc).__name__}: {exc}（计 0）"
    if not isinstance(data, list):
        return 0, [], "review_queue.json 顶层非数组（计 0）"
    pending = [x for x in data if isinstance(x, dict) and x.get("status") == "pending"]
    samples = [
        {"index": i, "src": x.get("src"), "status": x.get("status")}
        for i, x in enumerate(pending[:5])
    ]
    return len(pending), samples, None


def _check_status_conflict(conn):
    """状态矛盾：已 resolved 的错题仍存在 PENDING 复习计划。"""
    rows = conn.execute(
        """
        SELECT m.id, m.resolved_at, rs.id, rs.status
        FROM mistake_record m
        JOIN review_schedule rs ON rs.mistake_id = m.id
        WHERE m.resolved_at IS NOT NULL AND rs.status = 'PENDING'
        """
    ).fetchall()
    samples = [
        {
            "mistake_id": r[0],
            "resolved_at": r[1],
            "schedule_id": r[2],
            "schedule_status": r[3],
        }
        for r in rows[:5]
    ]
    return len(rows), samples, None


def _check_legacy_label_migration(conn):
    """旧口径标签迁移欠账：旧 3 类 error_type 且 diagnosis_type 仍为默认 CALC/空。

    报告统计只读 diagnosis_type，故这部分的错因归类至今是错的。
    """
    qmarks = ",".join("?" * len(_LEGACY_ERROR_TYPES))
    total_legacy = conn.execute(
        f"SELECT COUNT(*) FROM mistake_record WHERE error_type IN ({qmarks})",
        _LEGACY_ERROR_TYPES,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT m.id, m.error_type, m.diagnosis_type, m.source_ref
        FROM mistake_record m
        WHERE m.error_type IN ({qmarks})
          AND (m.diagnosis_type IS NULL OR m.diagnosis_type = 'CALC')
        """,
        _LEGACY_ERROR_TYPES,
    ).fetchall()
    samples = [
        {
            "id": r[0],
            "error_type": r[1],
            "diagnosis_type": r[2],
            "source_ref": (r[3] or "")[:40],
        }
        for r in rows[:5]
    ]
    note = (
        f"旧口径标签共 {total_legacy} 条（METHOD/CONFUSION/CARELESS）尚未迁移到新 4 类；"
        f"其中 {len(rows)} 条 diagnosis_type 仍为默认 CALC，"
        f"报告统计只读 diagnosis_type，故这 {len(rows)} 条的错因归类至今是错的"
    )
    return len(rows), samples, note


def _load_pitfalls(path: Path) -> list[dict]:
    """解析失败模式库 JSONL：跳过以 # 开头的注释行与空行，其余逐行 json.loads。

    单行非法 JSON 不阻断整体体检（跳过该行）。
    """
    out: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            continue
    return out


# ── P2-2 失败模式复发检测：signature → 可机械执行的检测实现 ──────────────
# 每个实现返回 (期望文案, 实测值, 是否复发)。template_path 仅 case6 使用。
def _pit_no_review_plan(conn, template_path: Path | None = None):
    """案例5：无复习计划的错题数，期望 0。"""
    n = conn.execute(
        """
        SELECT COUNT(*) FROM mistake_record m
        WHERE NOT EXISTS (
            SELECT 1 FROM review_schedule rs WHERE rs.mistake_id = m.id
        )
        """
    ).fetchone()[0]
    return "0", n, n > 0


def _pit_fake_success_hook(conn, template_path: Path | None = None):
    """案例6：报告模板中 window.__review_feedback_submit = function 的定义是否存在，期望存在。"""
    if template_path is None or not template_path.exists():
        return "存在定义", "模板缺失", True
    text = template_path.read_text(encoding="utf-8")
    found = "window.__review_feedback_submit = function" in text
    return "存在定义", ("存在定义" if found else "定义缺失"), (not found)


def _pit_dual_field(conn, template_path: Path | None = None):
    """案例7：source='MANUAL' 且 error_type != diagnosis_type 的记录数，期望 0。"""
    n = conn.execute(
        """
        SELECT COUNT(*) FROM mistake_record
        WHERE source = 'MANUAL'
          AND error_type IS NOT NULL
          AND error_type <> diagnosis_type
        """
    ).fetchone()[0]
    return "0", n, n > 0


_PITFALL_DISPATCH: dict[str, object] = {
    "record_no_review_plan": _pit_no_review_plan,
    "fake_success_review_hook": _pit_fake_success_hook,
    "dual_error_field_broken": _pit_dual_field,
}


def _check_pitfall_recurrence(conn):
    """失败模式复发检测（P2-2 失败模式库，对标 Hermes Agent 的自修复机制）。

    逐条读取 docs/pitfalls.jsonl 中带 recurrence_check 的条目，把条件落成可机械执行
    的检测。count = 复发的坑数量（0 表示全部健康）。

    文件缺失时 count=0 且 note 说明"失败模式库尚未建立"，不报错、不写入任何数据。
    """
    pitfalls_path = PROJECT_ROOT / "docs" / "pitfalls.jsonl"
    template_path = PROJECT_ROOT / "fdl_core" / "report" / "template.html"

    if not pitfalls_path.exists():
        return 0, [], "失败模式库尚未建立（docs/pitfalls.jsonl 不存在）"

    entries = _load_pitfalls(pitfalls_path)
    samples = []
    recurred = 0
    evaluated = 0
    for e in entries:
        rc = e.get("recurrence_check")
        if not rc:
            continue
        evaluated += 1
        sig = e.get("signature", "?")
        try:
            fn = _PITFALL_DISPATCH.get(sig)
            if fn is None:
                # 有 recurrence_check 但本工具暂未实现机械检测：不误诊，标记待人工确认
                expected, actual, is_recur = "已落地", "机械检测未实现（需人工确认）", False
            else:
                expected, actual, is_recur = fn(conn, template_path)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 单项失败不中断整体
            expected, actual, is_recur = "—", f"检测异常：{type(exc).__name__}: {exc}", False
        samples.append(
            {
                "signature": sig,
                "expect": expected,
                "actual": actual,
                "recurred": bool(is_recur),
            }
        )
        if is_recur:
            recurred += 1

    if evaluated == 0:
        note = "失败模式库已建立，但无带 recurrence_check 的条目"
    else:
        note = f"已检 {evaluated} 条带复发检测的失败模式，{recurred} 条复发"
    return recurred, samples, note


# 检测清单：(key, title, level, action, func_name, uses_conn)
_CHECKS = [
    (
        "zombie_mistakes",
        "僵尸错题",
        "warn",
        "为僵尸错题补齐复习记录（DONE）或调整 reappear_count 语义；长期无复习触达应触发再学习。",
        "_check_zombie_mistakes",
        True,
    ),
    (
        "diagnosis_type_broken",
        "错因断链",
        "high",
        "按 error_type 重新填写 diagnosis_type，使错因归因与诊断口径一致。",
        "_check_diagnosis_type_broken",
        True,
    ),
    (
        "kp_unmounted",
        "知识点未挂载",
        "info",
        "将错题挂载到 knowledge_point 树的对应节点，提升知识点覆盖率。",
        "_check_kp_unmounted",
        True,
    ),
    (
        "orphan_schedules",
        "孤儿复习计划",
        "high",
        "删除 mistake_id 为空或指向不存在错题的复习计划，或补录对应错题。",
        "_check_orphan_schedules",
        True,
    ),
    (
        "review_queue_backlog",
        "待复核积压",
        "info",
        "及时复核 review_queue.json 中的 pending 条目，避免 OCR/录入积压。",
        "_check_review_queue_backlog",
        False,
    ),
    (
        "status_conflict",
        "状态矛盾",
        "warn",
        "已 resolved 的错题应清除其 PENDING 复习计划，或回滚 resolved_at。",
        "_check_status_conflict",
        True,
    ),
    (
        "broken_image_refs",
        "图片引用缺失",
        "high",
        "修正 img_original / img_clean 指向真实文件（或把文件归档到引用位置）；"
        "若为重复记录，按数据净化流程标记后移除，避免复习页原图 404。",
        "_check_broken_image_refs",
        True,
    ),
    (
        "legacy_label_migration",
        "旧口径标签迁移欠账",
        "warn",
        "人工逐条确认这 11 条的错因应归入 CONCEPT/CALC/MISREAD/NORM 中的哪一类；"
        "确认后回填 diagnosis_type。旧口径标签建议保留在 error_type 作为历史痕迹，不做删除。",
        "_check_legacy_label_migration",
        True,
    ),
    (
        "pitfall_recurrence",
        "失败模式复发",
        "high",
        "对复发的失败模式回到对应根因与修法（docs/pitfalls.jsonl）处置；"
        "新踩的坑追加一条 pitfalls 记录并补 recurrence_check 机械检测，避免重蹈覆辙。",
        "_check_pitfall_recurrence",
        True,
    ),
]

# 模块内可调用函数引用（避免字符串反射）。
_FUNC_MAP = {
    "_check_zombie_mistakes": _check_zombie_mistakes,
    "_check_diagnosis_type_broken": _check_diagnosis_type_broken,
    "_check_kp_unmounted": _check_kp_unmounted,
    "_check_orphan_schedules": _check_orphan_schedules,
    "_check_broken_image_refs": _check_broken_image_refs,
    "_check_review_queue_backlog": _check_review_queue_backlog,
    "_check_status_conflict": _check_status_conflict,
    "_check_legacy_label_migration": _check_legacy_label_migration,
    "_check_pitfall_recurrence": _check_pitfall_recurrence,
}


def run_checks(db_path, *, review_queue_path: str | Path | None = None) -> dict:
    """运行全部检测项，返回机器可读结果 dict（只读，不修改数据）。

    Parameters
    ----------
    db_path:
        待检测数据库路径（字符串或 Path）。
    review_queue_path:
        可选，覆盖 review_queue.json 的默认路径（便于测试隔离）。
    """
    db_path = str(db_path)
    queue_path = (
        Path(review_queue_path)
        if review_queue_path is not None
        else (PROJECT_ROOT / "data" / "review_queue.json")
    )

    result: dict = {
        "generated_at": now_utc().astimezone(LOCAL_TZ).isoformat(),
        "db_path": db_path,
        "summary": {"total_issues": 0, "high": 0, "warn": 0, "info": 0},
        "checks": [],
    }

    conn = get_connection(db_path)
    try:
        for key, title, level, action, func_name, uses_conn in _CHECKS:
            func = _FUNC_MAP[func_name]
            try:
                if uses_conn:
                    count, samples, note = func(conn)
                else:
                    count, samples, note = func(queue_path)
            except Exception as exc:  # noqa: BLE001 - 单项失败不中断整体
                count, samples, note = (
                    0,
                    [],
                    f"检测异常：{type(exc).__name__}: {exc}",
                )
            result["checks"].append(
                {
                    "key": key,
                    "title": title,
                    "level": level,
                    "count": count,
                    "samples": samples,
                    "action": action,
                    "note": note,
                }
            )
            if count > 0:
                result["summary"]["total_issues"] += count
                result["summary"][level] += count
    finally:
        conn.close()
    return result


def _print_report(result: dict) -> None:
    """控制台可读报告（无 emoji，简洁列表）。"""
    print("=" * 60)
    print("FDL 自我净化体检报告（只读）")
    print(f"生成时间：{result['generated_at']}")
    print(f"数据库：{result['db_path']}")
    print("=" * 60)

    for c in result["checks"]:
        label = LEVEL_LABEL.get(c["level"], c["level"])
        head = f"[{label}] {c['title']} ({c['key']})：{c['count']} 条"
        print("\n" + head)
        if c["note"]:
            print(f"  备注：{c['note']}")
        if c["samples"]:
            print("  样例：")
            for s in c["samples"]:
                # 取能定位的 id 字段优先展示
                ident = s.get("id") or s.get("mistake_id") or s.get("schedule_id") or s.get("index")
                rest = {k: v for k, v in s.items() if k not in ("id",)}
                print(f"    - id={ident} {rest}")
        print(f"  建议：{c['action']}")

    s = result["summary"]
    print("\n" + "-" * 60)
    print(
        f"汇总：总计 {s['total_issues']} 项  |  "
        f"高 {s['high']}  |  中 {s['warn']}  |  低 {s['info']}"
    )
    print("-" * 60)


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="FDL 自我净化体检（只读，逐项错误暴露）")
    parser.add_argument(
        "--db",
        default=None,
        help="数据库路径（默认用 fdl_core.paths 主库）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="把结果额外写入 data/self_purify_report.json",
    )
    args = parser.parse_args(argv)

    if args.db:
        db_path = args.db
    else:
        try:
            from fdl_core.paths import get_paths

            db_path = str(get_paths().primary_db_path)
        except Exception:  # noqa: BLE001
            db_path = str(PROJECT_ROOT / "data" / "fdl.db")

    result = run_checks(db_path)
    _print_report(result)

    if args.json:
        out_path = PROJECT_ROOT / "data" / "self_purify_report.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 JSON 报告：{out_path}")

    return result


if __name__ == "__main__":
    main()

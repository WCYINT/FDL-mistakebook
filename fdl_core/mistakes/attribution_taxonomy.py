"""归因类别动态表（taxonomy）——读写、缓存、候选晋级。

为什么需要这个模块（King 2026-09-12 拍板）
------------------------------------------
原设计把归因类别**硬编码在两处代码里**，彼此词汇还不一致：

  1. `fdl_core/mistakes/attribution.py:MONSTERS`（8 类，服务 error_type 旧口径）
  2. `fdl_core/srs/queue_scheduler.py:DIAG_WEIGHT`（6 类，服务 diagnosis_type 新口径）

要想细化或新增一个归因类别，就必须改代码 + 发版。这与 King 的要求
「归因类别需支持持续细化与迭代，不固定为预设类别，随时间和分析逻辑的精细化
动态演进」直接冲突。

本模块把类别**变成数据行**：树形（parent_code）+ 版本（version）+ 可停用（status）
+ 动态权重（weight）。读取侧走 `load_active()` / `load_weights()`，**优先读表**；
表不存在（老库 / 未跑迁移 / 测试临时库）时**降级到内置常量**，保证不炸。

演进路线（King 定：LLM 提候选 + 人工确认晋级）
---------------------------------------------
LLM 发现现有类别覆盖不了的稳定模式 → `register_candidate()` 写候选
→ 人工确认 → `promote_candidate()` 晋级为正式类别（写入主表，status=ACTIVE）。
**绝不自动新增类别**——否则类别会失控膨胀、语义重叠。

时区约定：与仓库一致，存库统一 UTC（time_layer.now_utc）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from fdl_core.srs.time_layer import fmt_ts, now_utc

# ── v1 种子：两套口径的并集（与 migrations/2026_09_12_* 保持一致）──────────
# (code, label, weight, 口径来源)
#   SCHED  → 来自 queue_scheduler.DIAG_WEIGHT（服务 diagnosis_type，有权威权重）
#   LEGACY → 仅来自 attribution.MONSTERS（服务 error_type，从未定权）
SEED_TAXONOMY: list[tuple[str, str, float, str]] = [
    ("CONCEPT", "概念不清", 0.30, "SCHED"),
    ("MISREAD", "审题偏差", 0.20, "SCHED"),
    ("CALC", "计算失误", 0.10, "SCHED"),
    ("NORM", "规范缺失", 0.10, "SCHED"),
    ("CARELESS", "粗心", 0.05, "SCHED"),
    ("OTHER", "其他", 0.00, "SCHED"),
    ("CONFUSION", "混淆", 0.00, "LEGACY"),
    ("METHOD", "方法不会", 0.00, "LEGACY"),
    ("EXPRESSION", "表达不规范", 0.00, "LEGACY"),
    ("FORGOT", "遗忘", 0.00, "LEGACY"),
    ("TIMEOUT", "超时", 0.00, "LEGACY"),
]

_SEED_DESC = {
    "SCHED": "v1 种子·新口径（diagnosis_type）：权重沿用 queue_scheduler.DIAG_WEIGHT 原值",
    "LEGACY": "v1 种子·旧口径（error_type 怪兽图鉴）：从未定过权，weight 留 0.0 待人工定价",
}

# ── 降级内置默认 ────────────────────────────────────────────
# 表不可用（老库/未迁移/临时库）时使用。与 queue_scheduler.DIAG_WEIGHT 逐项对齐，
# 保证「读不到动态表」与「读得到动态表（种子态）」结果一致，行为不突变。
BUILTIN_FALLBACK_WEIGHTS: dict[str, float] = {
    "CONCEPT": 0.30,
    "MISREAD": 0.20,
    "CALC": 0.10,
    "NORM": 0.10,
    "CARELESS": 0.05,
    "OTHER": 0.00,
}


@dataclass(frozen=True)
class TaxonomyEntry:
    """一个归因类别（taxonomy 的一行）。"""

    code: str
    label: str
    parent_code: str | None
    weight: float
    version: int
    status: str  # ACTIVE / DEPRECATED
    is_seed: int = 0


# 进程内缓存：{db_path: (signature, entries)}。
# signature = (总数, 最大 version, 最大 updated_at)——任一变化即失效重载。
# 为什么用"签名"而不是手动失效：调用方可能直接 SQL 改表，靠手动失效会读到脏值。
_CACHE: dict[str, tuple[tuple, dict[str, TaxonomyEntry]]] = {}


def clear_cache() -> None:
    """清空 taxonomy 进程内缓存（测试 / 人工改表后调用）。"""
    _CACHE.clear()


def _db_path(conn: sqlite3.Connection) -> str:
    """取连接对应的库文件路径（缓存键）。内存库返回 ':memory:'。"""
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            # row = (seq, name, file)
            if row[1] == "main":
                return row[2] or ":memory:"
    except Exception:  # noqa: BLE001
        pass
    return ":memory:"


def has_taxonomy_table(conn: sqlite3.Connection) -> bool:
    """taxonomy 主表是否存在（老库 / 未跑迁移时为 False）。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attribution_taxonomy'"
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _signature(conn: sqlite3.Connection) -> tuple:
    """taxonomy 内容签名，用于缓存失效判定。"""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(version),0), COALESCE(MAX(updated_at),'')"
        " FROM attribution_taxonomy"
    ).fetchone()
    return (int(row[0]), int(row[1]), str(row[2] or ""))


def ensure_seeded(conn: sqlite3.Connection) -> int:
    """把 v1 种子幂等写入 taxonomy（已存在则跳过）。返回新写入行数。

    用途：新建库（create_schema 之后）若未跑迁移，也能立即有可用类别。
    不覆盖任何已存在的行——人工调过的 weight/version 不会被冲掉。
    """
    if not has_taxonomy_table(conn):
        return 0
    n = 0
    for code, label, weight, scope in SEED_TAXONOMY:
        cur = conn.execute(
            "INSERT INTO attribution_taxonomy"
            " (code, label, parent_code, weight, version, status, is_seed, description)"
            " VALUES (?, ?, NULL, ?, 1, 'ACTIVE', 1, ?)"
            " ON CONFLICT(code) DO NOTHING",
            (code, label, float(weight), _SEED_DESC.get(scope, "v1 种子")),
        )
        n += cur.rowcount or 0
    if n:
        clear_cache()
    return n


def load_active(conn: sqlite3.Connection, *, use_cache: bool = True) -> dict[str, TaxonomyEntry]:
    """读全部 ACTIVE 类别 → {code: TaxonomyEntry}。

    表不存在时返回空 dict（调用方自行降级到 BUILTIN_FALLBACK_WEIGHTS）。
    """
    if not has_taxonomy_table(conn):
        return {}
    dbp = _db_path(conn)
    if use_cache:
        sig = _signature(conn)
        hit = _CACHE.get(dbp)
        if hit and hit[0] == sig:
            return hit[1]
    rows = conn.execute(
        "SELECT code, label, parent_code, weight, version, status, is_seed"
        " FROM attribution_taxonomy WHERE status='ACTIVE' ORDER BY weight DESC, code"
    ).fetchall()
    entries = {
        r[0]: TaxonomyEntry(
            code=r[0],
            label=r[1],
            parent_code=r[2],
            weight=float(r[3]),
            version=int(r[4]),
            status=r[5],
            is_seed=int(r[6]),
        )
        for r in rows
    }
    if not entries:
        # 表存在但为空（新库建了表、还没跑迁移/种子）→ 回落内置默认。
        # 为什么：否则闸门 is_valid_code 对一切 code 都返回 False，
        # 引擎会"永久转人工"，系统自愈能力归零。与 load_weights 的降级口径一致。
        entries = {
            code: TaxonomyEntry(
                code=code,
                label=code,
                parent_code=None,
                weight=float(w),
                version=1,
                status="ACTIVE",
                is_seed=0,
            )
            for code, w in BUILTIN_FALLBACK_WEIGHTS.items()
        }
    if use_cache:
        _CACHE[dbp] = (_signature(conn), entries)
    return entries


def load_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """读 {code: weight}，供调度器算综合优先级。

    **降级保证**：表不存在或无任何 ACTIVE 行 → 返回 BUILTIN_FALLBACK_WEIGHTS 的副本。
    这样老库、未迁移库、离线场景的行为与改造前完全一致。
    """
    active = load_active(conn)
    if not active:
        return dict(BUILTIN_FALLBACK_WEIGHTS)
    return {code: e.weight for code, e in active.items()}


def get_entry(conn: sqlite3.Connection, code: str) -> TaxonomyEntry | None:
    """按 code 取单个类别（含 DEPRECATED），不存在返回 None。"""
    if not has_taxonomy_table(conn):
        return None
    row = conn.execute(
        "SELECT code, label, parent_code, weight, version, status, is_seed"
        " FROM attribution_taxonomy WHERE code=?",
        (code,),
    ).fetchone()
    if not row:
        return None
    return TaxonomyEntry(
        code=row[0],
        label=row[1],
        parent_code=row[2],
        weight=float(row[3]),
        version=int(row[4]),
        status=row[5],
        is_seed=int(row[6]),
    )


def is_valid_code(conn: sqlite3.Connection, code: str) -> bool:
    """code 是否为**当前生效**的类别（闸门放行前必须校验）。"""
    return code in load_active(conn)


# ── 人工维护 API（类别演进）──────────────────────────────────
def add_category(
    conn: sqlite3.Connection,
    *,
    code: str,
    label: str,
    parent_code: str | None = None,
    weight: float = 0.0,
    description: str | None = None,
) -> int:
    """新增一个类别（人工/晋级用）。返回 rowcount（0=已存在未改动）。"""
    cur = conn.execute(
        "INSERT INTO attribution_taxonomy"
        " (code, label, parent_code, weight, version, status, is_seed, description)"
        " VALUES (?, ?, ?, ?, 1, 'ACTIVE', 0, ?)"
        " ON CONFLICT(code) DO NOTHING",
        (code, label, parent_code, float(weight), description),
    )
    conn.commit()
    clear_cache()
    return cur.rowcount or 0


def update_weight(conn: sqlite3.Connection, code: str, weight: float) -> int:
    """调整权重并 version+1（保留演进痕迹）。返回 rowcount。"""
    cur = conn.execute(
        "UPDATE attribution_taxonomy SET weight=?, version=version+1, updated_at=? WHERE code=?",
        (float(weight), fmt_ts(now_utc()), code),
    )
    conn.commit()
    clear_cache()
    return cur.rowcount or 0


def deprecate(conn: sqlite3.Connection, code: str) -> int:
    """停用类别（不再被 LLM 选择，但历史数据仍可追溯）。"""
    cur = conn.execute(
        "UPDATE attribution_taxonomy SET status='DEPRECATED', version=version+1,"
        " updated_at=? WHERE code=?",
        (fmt_ts(now_utc()), code),
    )
    conn.commit()
    clear_cache()
    return cur.rowcount or 0


# ── 候选（LLM 提候选 + 人工确认晋级）────────────────────────
def register_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_code: str,
    candidate_label: str,
    parent_code: str | None = None,
    rationale: str | None = None,
    sample: dict | None = None,
) -> int:
    """登记一个"新类别候选"。

    同一 candidate_code 重复出现时**累加证据数**（evidence_count+1）并存活样本，
    而不是插入重复行——这样"证据不足"的候选不会淹没复核队列。
    返回候选行 id。
    """
    stamp = fmt_ts(now_utc())
    row = conn.execute(
        "SELECT id, evidence_count, sample_json FROM taxonomy_candidate WHERE candidate_code=?",
        (candidate_code,),
    ).fetchone()
    if row:
        cid, cnt, old_sample = row[0], int(row[1]), row[2]
        try:
            samples = json.loads(old_sample) if old_sample else []
        except Exception:  # noqa: BLE001
            samples = []
        if sample:
            samples.append(sample)
        conn.execute(
            "UPDATE taxonomy_candidate SET evidence_count=?, sample_json=?,"
            " rationale=COALESCE(?, rationale), updated_at=? WHERE id=?",
            (cnt + 1, json.dumps(samples[-20:], ensure_ascii=False), rationale, stamp, cid),
        )
        conn.commit()
        return cid
    cur = conn.execute(
        "INSERT INTO taxonomy_candidate"
        " (candidate_code, candidate_label, parent_code, rationale, evidence_count,"
        "  sample_json, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, ?, 'PENDING', ?, ?)",
        (
            candidate_code,
            candidate_label,
            parent_code,
            rationale,
            json.dumps([sample] if sample else [], ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_candidates(conn: sqlite3.Connection, *, status: str = "PENDING") -> list[dict]:
    """列出候选（默认待复核）。复核队列的取数入口。"""
    rows = conn.execute(
        "SELECT id, candidate_code, candidate_label, parent_code, rationale,"
        " evidence_count, status, created_at FROM taxonomy_candidate"
        " WHERE status=? ORDER BY evidence_count DESC, created_at",
        (status,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "candidate_code": r[1],
            "candidate_label": r[2],
            "parent_code": r[3],
            "rationale": r[4],
            "evidence_count": int(r[5]),
            "status": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]


def promote_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    decided_by: str = "PARENT",
    weight: float = 0.0,
    label: str | None = None,
) -> dict:
    """人工确认 → 候选晋级为正式类别（写入主表，status=ACTIVE）。"""
    row = conn.execute(
        "SELECT candidate_code, candidate_label, parent_code FROM taxonomy_candidate WHERE id=?",
        (candidate_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"候选不存在：{candidate_id}"}
    code, cand_label, parent = row[0], row[1], row[2]
    final_label = label or cand_label
    added = add_category(
        conn,
        code=code,
        label=final_label,
        parent_code=parent,
        weight=weight,
        description=f"由候选 #{candidate_id} 晋级（decided_by={decided_by}）",
    )
    stamp = fmt_ts(now_utc())
    conn.execute(
        "UPDATE taxonomy_candidate SET status='PROMOTED', promoted_code=?,"
        " decided_by=?, decided_at=?, updated_at=? WHERE id=?",
        (code, decided_by, stamp, stamp, candidate_id),
    )
    conn.commit()
    clear_cache()
    return {"ok": True, "promoted_code": code, "label": final_label, "added": added}


def reject_candidate(
    conn: sqlite3.Connection, candidate_id: int, *, decided_by: str = "PARENT"
) -> dict:
    """人工驳回候选（保留痕迹，不删除）。"""
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE taxonomy_candidate SET status='REJECTED', decided_by=?, decided_at=?,"
        " updated_at=? WHERE id=? AND status='PENDING'",
        (decided_by, stamp, stamp, candidate_id),
    )
    conn.commit()
    return {"ok": bool(cur.rowcount), "candidate_id": candidate_id}

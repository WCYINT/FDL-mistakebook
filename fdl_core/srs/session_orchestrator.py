"""阶段 1.4 · OpenMAIC 借鉴 B：复习会话状态机编排

状态机（King 要求）：
    CREATED → ACTIVE → (PAUSED ⇄ ACTIVE) → COMPLETED
    终态：COMPLETED / ABANDONED / ERROR（不可再迁）

列（study_session）：
    session_state     TEXT       DEFAULT 'CREATED'  （注：主库无此列，本模块用 in-memory 状态追踪 + 可选落库）
    lease_until       TEXT       租约到期（UTC ISO）
    resume_count      INTEGER    DEFAULT 0       PAUSED→ACTIVE 次数
    last_event_at     TEXT       最后一次状态迁移时间

本模块纯函数式 + 显式 conn 注入：可单测、可在任何会话场景复用。

不依赖 schedule_id：会话与具体错题解耦。
"""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
from typing import Any

# === 状态机定义（King 要求 CREATED→ACTIVE→PAUSED→COMPLETED）===
SESS_STATES = ("CREATED", "ACTIVE", "PAUSED", "COMPLETED", "ABANDONED", "ERROR")

_SESS_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"ACTIVE", "COMPLETED", "ABANDONED", "ERROR"},
    "ACTIVE": {"PAUSED", "COMPLETED", "ABANDONED", "ERROR"},
    "PAUSED": {"ACTIVE", "COMPLETED", "ABANDONED", "ERROR"},
    "COMPLETED": set(),  # 终态
    "ABANDONED": set(),  # 终态
    "ERROR": {"ACTIVE"},  # 出错后可恢复重激活
}

# 租约 TTL 默认（5 分钟；同 PPT v4.4 P13 session_orchestrator 模式）
LEASE_TTL_SEC = 300


# === 异常 ===


class SessionError(Exception):
    """会话状态机相关错误。"""


class LeaseError(SessionError):
    """租约错误（无效 / 过期 / 不匹配）。"""


# === 内部辅助 ===


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_plus_sec_iso(seconds: int) -> str:
    return (
        (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _gen_lease_token() -> str:
    return secrets.token_urlsafe(24)


def _parse_iso(s: str) -> dt.datetime:
    """Parse ISO 8601 (supports 'Z' suffix)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


# === 公开 API ===


def start(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    session_date: str,
    session_slot: str,
    session_kind: str = "REVIEW",
    trigger_type: str = "SELF",
    review_mistake_id: int | None = None,
) -> dict[str, Any]:
    """开启一个复习会话：state=ACTIVE + 签发初始租约。

    返回 dict 含 session_id / lease_until / state，便于调用方直接接管。
    幂等：不存在（同 user_id, session_date, session_slot, session_kind）的开启，
    存在则返回旧记录 + 续约（避免同日同槽位重复开）。
    """
    cols_avail = {r[1] for r in conn.execute("PRAGMA table_info('study_session')").fetchall()}
    has_session_kind = "session_kind" in cols_avail

    # 条件化 SELECT（主库可能没有 session_state / lease_until 等列）
    select_cols = ["id"]
    for c in ("session_state", "lease_until", "resume_count", "last_event_at"):
        if c in cols_avail:
            select_cols.append(c)
    where_clause = "user_id=? AND session_date=? AND session_slot=?"
    where_args: list[Any] = [user_id, session_date, session_slot]
    if has_session_kind:
        where_clause += " AND session_kind=?"
        where_args.append(session_kind)
    existing = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM study_session WHERE {where_clause}",
        where_args,
    ).fetchone()
    now_iso = _now_utc_iso()
    lease_until_iso = _now_plus_sec_iso(LEASE_TTL_SEC)
    if existing:
        ex = dict(zip(select_cols, existing, strict=False))
        sid = ex["id"]
        state = ex.get("session_state") or "ACTIVE"
        lease_until = ex.get("lease_until")
        rc = ex.get("resume_count") or 0
        last_event = ex.get("last_event_at")
        if lease_until is None or _parse_iso(lease_until) < dt.datetime.now(dt.UTC):
            sets = ["lease_until = ?", "last_event_at = ?", "updated_at = ?"]
            ev = [lease_until_iso, now_iso, now_iso]
            if "session_state" in cols_avail:
                sets.insert(0, "session_state = ?")
                ev.insert(0, "ACTIVE")
            conn.execute(
                f"UPDATE study_session SET {', '.join(sets)} WHERE id=?",
                ev + [sid],
            )
            conn.commit()
        return {
            "session_id": sid,
            "state": state,
            "lease_until": lease_until_iso,
            "resume_count": rc,
            "last_event_at": last_event,
            "resumed": True,
        }

    # 新建
    cols = [
        "user_id",
        "session_date",
        "session_slot",
        "trigger_type",
        "session_role",
        "started_at",
        "duration_sec",
        "effective_sec",
    ]
    vals: list[Any] = [user_id, session_date, session_slot, trigger_type, "SELF", now_iso, 0, 0]
    if has_session_kind:
        cols.append("session_kind")
        vals.append(session_kind)
    if "session_state" in cols_avail:
        cols.append("session_state")
        vals.append("ACTIVE")
    if "lease_until" in cols_avail:
        cols.append("lease_until")
        vals.append(lease_until_iso)
    if "resume_count" in cols_avail:
        cols.append("resume_count")
        vals.append(0)
    if "last_event_at" in cols_avail:
        cols.append("last_event_at")
        vals.append(now_iso)
    if review_mistake_id is not None:
        cols.append("review_mistake_id")
        vals.append(review_mistake_id)

    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO study_session ({', '.join(cols)}) VALUES ({placeholders})"
    cur = conn.execute(sql, vals)
    conn.commit()
    return {
        "session_id": cur.lastrowid,
        "state": "ACTIVE",
        "lease_until": lease_until_iso,
        "resume_count": 0,
        "last_event_at": now_iso,
        "resumed": False,
    }


def _read(conn: sqlite3.Connection, session_id: int) -> dict | None:
    """读会话核心字段（缺列时安全降级）。"""
    cols_avail = {r[1] for r in conn.execute("PRAGMA table_info('study_session')").fetchall()}
    cols = []
    for c in ("session_state", "lease_until", "resume_count", "last_event_at"):
        if c in cols_avail:
            cols.append(c)
    if not cols:
        return None
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM study_session WHERE id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(cols, row, strict=False))


def lease_status(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    """返回会话当前租约状态（不抛错，缺字段安全降级）。"""
    row = _read(conn, session_id)
    if row is None:
        return {"exists": False}
    return {
        "exists": True,
        "state": row.get("session_state") or "ACTIVE",
        "lease_until": row.get("lease_until"),
        "resume_count": row.get("resume_count") or 0,
        "last_event_at": row.get("last_event_at"),
        "lease_valid": _lease_valid(row.get("lease_until")),
    }


def _lease_valid(lease_until: str | None) -> bool:
    if not lease_until:
        return False
    try:
        return _parse_iso(lease_until) > dt.datetime.now(dt.UTC)
    except Exception:
        return False


def _transition(
    conn: sqlite3.Connection,
    session_id: int,
    to_state: str,
) -> dict[str, Any]:
    """执行状态迁移（无 token 校验，最简形式）。"""
    if to_state not in SESS_STATES:
        raise SessionError(f"invalid target state: {to_state!r}")
    row = _read(conn, session_id)
    if row is None:
        raise SessionError(f"session not found: {session_id}")
    cur_state = row.get("session_state") or "ACTIVE"
    allowed = _SESS_TRANSITIONS.get(cur_state, set())
    if to_state not in allowed:
        raise SessionError(f"illegal transition {cur_state!r} → {to_state!r}")

    now_iso = _now_utc_iso()
    rc_inc = 0
    if to_state == "ACTIVE" and cur_state == "PAUSED":
        rc_inc = 1

    sets = ["last_event_at = ?", "updated_at = ?"]
    vals: list[Any] = [now_iso, now_iso]
    has_ss = "session_state" in {
        r[1] for r in conn.execute("PRAGMA table_info('study_session')").fetchall()
    }
    if has_ss:
        sets.insert(0, "session_state = ?")
        vals.insert(0, to_state)
    if rc_inc > 0:
        sets.append("resume_count = COALESCE(resume_count, 0) + 1")
    sql = f"UPDATE study_session SET {', '.join(sets)} WHERE id=?"
    vals.append(session_id)
    conn.execute(sql, vals)
    conn.commit()
    return {"ok": True, "from_state": cur_state, "to_state": to_state, "session_id": session_id}


# === 便捷 API（按 King 命名：CREATED/ACTIVE/PAUSED/COMPLETED）===


def activate(conn: sqlite3.Connection, session_id: int) -> dict:
    """CREATED → ACTIVE（首次开课用 start() 已直达 ACTIVE，此函数供外部补单）。"""
    return _transition(conn, session_id, "ACTIVE")


def pause(conn: sqlite3.Connection, session_id: int) -> dict:
    """ACTIVE → PAUSED（用户切走 / 切后台）。"""
    return _transition(conn, session_id, "PAUSED")


def resume(conn: sqlite3.Connection, session_id: int) -> dict:
    """PAUSED → ACTIVE（杀进程后重启关键路径）。"""
    return _transition(conn, session_id, "ACTIVE")


def complete(conn: sqlite3.Connection, session_id: int) -> dict:
    """ACTIVE → COMPLETED（终态，不可再迁）。"""
    return _transition(conn, session_id, "COMPLETED")


def abandon(conn: sqlite3.Connection, session_id: int) -> dict:
    """ACTIVE → ABANDONED（用户主动放弃）。"""
    return _transition(conn, session_id, "ABANDONED")


def errored(conn: sqlite3.Connection, session_id: int) -> dict:
    """→ ERROR（异常时进入错误态，标记排查）。"""
    return _transition(conn, session_id, "ERROR")

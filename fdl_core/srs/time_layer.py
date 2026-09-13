"""统一 UTC 时间处理层（P2-06 / SRS-01）。

🔴 PRD 明标"本地时区/每日分界易踩坑"。规则（PRD §6.1 通用约定）：
- **存储一律 UTC**（aware datetime / ISO-8601 字符串，与 P2-01 schema 的
  `strftime('%Y-%m-%dT%H:%M:%SZ','now')` 格式对齐）；
- **每日分界一律本地日期**（Frank 在深圳 → `Asia/Shanghai`）：
  UTC 16:00 之后仍算"前一天的学习日"，同日去重 / 批处理 / due 判定全部基于本地日期。

所有模块禁止直接用 `datetime.now()` / `datetime.utcnow()`（CI 可扩展扫描）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

UTC = UTC
LOCAL_TZ = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8))

# 与 P2-01 schema 默认值一致的存储格式
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    """当前 UTC 时间（aware）。测试可 monkeypatch 此函数。"""
    return datetime.now(tz=UTC)


def to_utc(dt: datetime) -> datetime:
    """任意 datetime → aware UTC（naive 视为本地时间）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(UTC)


def fmt_ts(dt: datetime) -> str:
    """aware datetime → 存库字符串（ISO-8601 UTC，秒级 Z 后缀）。"""
    return to_utc(dt).strftime(TS_FORMAT)


def parse_ts(s: str) -> datetime:
    """存库字符串 → aware UTC datetime（兼容带偏移的 ISO 格式）。"""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return to_utc(dt)


def parse_ts_lenient(s: str) -> datetime:
    """宽容解析：同时兼容 ISO（`T`/`Z`）与 SQLite `CURRENT_TIMESTAMP`（空格）格式。

    🔴 语义说明（2026-09-13）：
    - ISO 格式（schema 默认 `strftime('%Y-%m-%dT%H:%M:%SZ')`）→ 直接解析；
    - 空格格式（如 `2026-09-12 15:18:28`，来自 `DEFAULT CURRENT_TIMESTAMP`）
      **语义是 UTC**——SQLite 官方定义 CURRENT_TIMESTAMP 为 UTC；
      因此 naive 解析后必须显式补 UTC，**不能**走 `to_utc()` 的
      "naive 视为本地" 约定（那会错 8 小时）。
    - `review_feedback.created_at` 在生产库用的是 CURRENT_TIMESTAMP →
      调用方（复习日期展示 / kp_state 重建）一律用本函数，禁止 `s[:10]` 切片。
    """
    t = str(s).strip()
    if not t:
        raise ValueError("empty timestamp")
    iso = t.replace(" ", "T")
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_date_of_lenient(s: str) -> date:
    """宽容解析 → 本地日期（学习日）。"""
    return parse_ts_lenient(s).astimezone(LOCAL_TZ).date()


def local_date(dt: datetime | None = None) -> date:
    """学习日 = 本地（Asia/Shanghai）日期。每日分界的唯一口径。"""
    dt = to_utc(dt) if dt is not None else now_utc()
    return dt.astimezone(LOCAL_TZ).date()


def local_date_range(d: date) -> tuple[datetime, datetime]:
    """本地日期 d 的 [00:00, 次日 00:00) 闭开区间（UTC aware），用于同日筛选。"""
    start = datetime.combine(d, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def days_between(d1: date, d2: date) -> int:
    """本地日期差（d2 − d1，天）。"""
    return (d2 - d1).days


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)

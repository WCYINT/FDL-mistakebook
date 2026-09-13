"""L1-C 云端响应缓存（P2-23 / D-2）。

独立 SQLite：`data/fdl_l2_cache.sqlite`（与主库隔离，可随时重建）。
- 缓存键：`prompt_hash = sha256(system_prompt + user_input)`（D-2 落地值）
- 值：JSON（answer + meta）；`version_stamp` 标参数/图谱版本；`ttl` 到期不返回
- M2 验收 AC-3：命中率 ≥ 70%
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from fdl_core.srs.time_layer import fmt_ts, now_utc, parse_ts

_DDL = """
CREATE TABLE IF NOT EXISTS cache (
    key           TEXT PRIMARY KEY,
    value         TEXT    NOT NULL,
    version_stamp TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache (expires_at);
"""


def cache_key(system_prompt: str, user_input: str) -> str:
    """缓存键 = sha256(system + user)（D-2 落地值）。"""
    return hashlib.sha256((system_prompt + "\x00" + user_input).encode("utf-8")).hexdigest()


class L1Cache:
    """L1-C 缓存存取（SQLite k/v + TTL + 版本戳）。"""

    def __init__(self, db_path: Path | str, version_stamp: str = "v0"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.version_stamp = version_stamp
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.executescript(_DDL)
        self._conn.commit()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        """命中返回 JSON 值；过期/版本不符/未命中返回 None（并计 miss）。"""
        row = self._conn.execute(
            "SELECT value, version_stamp, expires_at FROM cache WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        value, version, expires_at = row
        if version != self.version_stamp:  # 版本漂移视为失效
            self.misses += 1
            return None
        if parse_ts(expires_at) <= now_utc():
            self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
            self._conn.commit()
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(value)

    def put(self, key: str, value: dict, ttl_seconds: int = 7 * 86400) -> None:
        """写入缓存（默认 TTL 7 天）。"""
        expires = now_utc() + timedelta(seconds=ttl_seconds)
        self._conn.execute(
            "INSERT INTO cache (key, value, version_stamp, expires_at, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " version_stamp=excluded.version_stamp, expires_at=excluded.expires_at,"
            " created_at=excluded.created_at",
            (
                key,
                json.dumps(value, ensure_ascii=False),
                self.version_stamp,
                fmt_ts(expires),
                fmt_ts(now_utc()),
            ),
        )
        self._conn.commit()

    @property
    def hit_rate(self) -> float:
        """命中率（AC-3 验收 ≥0.70）。"""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def close(self) -> None:
        self._conn.close()

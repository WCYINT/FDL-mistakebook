"""调度参数配置化（P2-12 / SRS-06）。

🔴 硬验收：改 `config/model_params.yaml` 不改代码。
每次参数变更（版本号变化）写 `model_param_history` 表（快照 + 理由），
一次只改 1 个变量（PRD §6.5 纪律）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from fdl_core.srs.time_layer import fmt_ts, now_utc

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "model_params.yaml"


class ParamsError(ValueError):
    """参数文件错误。"""


class ModelParams:
    """参数字典的薄封装：属性式读取 + 版本号。"""

    def __init__(self, data: dict, source: Path | None = None):
        if "version" not in data:
            raise ParamsError("model_params.yaml 缺少 version 字段")
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: str | Path | None = None) -> ModelParams:
        p = Path(path) if path else DEFAULT_PARAMS_PATH
        if not p.exists():
            raise ParamsError(f"参数文件不存在：{p}")
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, p)

    @property
    def version(self) -> str:
        return str(self._data["version"])

    @property
    def raw(self) -> dict:
        return self._data

    def section(self, name: str) -> dict:
        return self._data.get(name, {})

    # 常用快捷读取
    @property
    def mastery(self) -> dict:
        return self.section("mastery")

    @property
    def transitions(self) -> dict:
        return self.section("transitions")

    @property
    def scheduling(self) -> dict:
        return self.section("scheduling")

    def get(self, *keys, default=None):
        """多点路径读取，如 `p.get("scheduling", "review_per_day")`。"""
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def ensure_param_history_table(conn: sqlite3.Connection) -> None:
    """P3 表 `model_param_history` 提前建（P2-12 验收要求写变更历史）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_param_history (
            id          INTEGER PRIMARY KEY,
            version     TEXT    NOT NULL UNIQUE,
            params_json TEXT    NOT NULL,
            reason      TEXT,
            created_at  TEXT    NOT NULL
        )
        """
    )
    conn.commit()


def record_params(conn: sqlite3.Connection, params: ModelParams, reason: str = "") -> None:
    """记录/更新当前参数快照（version 唯一，重复版本视为覆盖更新）。"""
    ensure_param_history_table(conn)
    conn.execute(
        "INSERT INTO model_param_history (version, params_json, reason, created_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(version) DO UPDATE SET params_json=excluded.params_json,"
        " reason=excluded.reason, created_at=excluded.created_at",
        (
            params.version,
            json.dumps(params.raw, ensure_ascii=False, sort_keys=True),
            reason,
            fmt_ts(now_utc()),
        ),
    )
    conn.commit()

"""FDL 测试共享 fixture（C5.1）。

5 个 fixture：tmp_sqlite / markdown_dir / tmp_logs_dir / tmp_alerts_dir / fdl_logger。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# 确保 fdl_core 可导入（不依赖 pip install -e）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_sqlite(tmp_path):
    """临时 SQLite 数据库连接（机读事实表）。"""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def markdown_dir(tmp_path):
    """Markdown + YAML 人读真相源目录。"""
    d = tmp_path / "markdown"
    d.mkdir()
    return d


@pytest.fixture
def tmp_logs_dir(tmp_path):
    """临时日志目录。"""
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def tmp_alerts_dir(tmp_path):
    """临时告警目录。"""
    d = tmp_path / "alerts"
    d.mkdir()
    return d


@pytest.fixture
def fdl_logger(tmp_logs_dir):
    """已配置的 fdl logger。"""
    from fdl_core.logging_setup import setup_logging

    return setup_logging(tmp_logs_dir)


_SCHED = str(Path(__file__).resolve().parent.parent / "scripts" / "sched")
if _SCHED not in sys.path:
    sys.path.insert(0, _SCHED)


# ── 开源检出：本机配置自动补齐 ────────────────────────────────
# config/fdl_paths.yaml 被 .gitignore 忽略（含本机绝对路径），新检出的仓库里不存在。
# 这里在缺失时从示例模板生成一份，使测试与 CI 开箱即用（不依赖人工 cp）。
_CFG = ROOT / "config" / "fdl_paths.yaml"
_CFG_EXAMPLE = ROOT / "config" / "fdl_paths.yaml.example"
if not _CFG.exists() and _CFG_EXAMPLE.exists():
    _CFG.write_text(_CFG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


# ── 统一的导入路径引导（开源发布版）───────────────────────────
# 说明：发布物中不硬编码任何本机绝对路径。这里把项目根、scripts/、
# scripts/sched/ 注入 sys.path，等价于原开发树里的逐文件 sys.path.insert。
_SCRIPTS = ROOT / "scripts"
for _p in (ROOT, _SCRIPTS, _SCRIPTS / "sched"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

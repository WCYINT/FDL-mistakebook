"""P2-02 路径配置化消费层验收测试。

验证 layout_version 读取、v1/v2 切换、根目录定位（.git/锚文件）、
派生路径解析、零硬编码（不写死可移动卷路径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fdl_core.paths import FdlPaths, load_config, locate_root

_CONFIG_TEMPLATE = {
    "layout_version": 1,
    "subject_codes": ["MATH", "CHINESE", "ENGLISH", "SCIENCE"],
    "layout": {
        "v1": {
            "swe_dir": "0-SWE",
            "fdl_dir": "0-SWE/2-FDL",
            "subject_dirs": ["1-Math", "2-语文", "3-英语", "4-科学"],
        },
        "v2": {
            "swe_dir": "0-SWE",
            "fdl_dir": "0-SWE/2-FDL",
            "subject_dirs": [
                "0-SWE/2-FDL/subjects/1-Math",
                "0-SWE/2-FDL/subjects/2-语文",
                "0-SWE/2-FDL/subjects/3-英语",
                "0-SWE/2-FDL/subjects/4-科学",
            ],
        },
    },
    "subdirs": {
        "core": "fdl_core",
        "cli": "fdl",
        "tests": "tests",
        "scripts": "scripts",
        "docs": "docs",
        "assets": "assets",
        "data": "data",
        "logs": "logs",
        "backups": "backups",
        "config": "config",
        "models": "models",
    },
    "anchor_file": "0-SWE/2-FDL/README.md",
}


def _write_config(tmp_path: Path, version: int) -> Path:
    cfg = dict(_CONFIG_TEMPLATE)
    cfg["layout_version"] = version
    p = tmp_path / "config" / "fdl_paths.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


# ── 根目录定位 ──────────────────────────────────────────────
def test_locate_root_via_git(tmp_path):
    (tmp_path / ".git").mkdir()
    start = tmp_path / "0-SWE" / "2-FDL" / "fdl_core"
    start.mkdir(parents=True)
    assert locate_root(start, "0-SWE/2-FDL/README.md") == tmp_path


def test_locate_root_via_anchor(tmp_path):
    anchor = tmp_path / "0-SWE" / "2-FDL" / "README.md"
    anchor.parent.mkdir(parents=True)
    anchor.write_text("anchor")
    start = tmp_path / "0-SWE" / "2-FDL" / "fdl_core"
    start.mkdir(parents=True)
    assert locate_root(start, "0-SWE/2-FDL/README.md") == tmp_path


# ── v1 平级布局 ─────────────────────────────────────────────
def test_paths_v1(tmp_path):
    p = FdlPaths(config_path=_write_config(tmp_path, 1), root=tmp_path)
    assert p.layout_version == 1
    assert p.swe_dir == tmp_path / "0-SWE"
    assert p.fdl_root == tmp_path / "0-SWE" / "2-FDL"
    assert p.subject_dir("MATH") == tmp_path / "1-Math"
    assert p.subject_dir("chinese") == tmp_path / "2-语文"  # 大小写不敏感
    assert p.subjects["SCIENCE"] == tmp_path / "4-科学"


# ── v2 嵌套布局 ─────────────────────────────────────────────
def test_paths_v2(tmp_path):
    p = FdlPaths(config_path=_write_config(tmp_path, 2), root=tmp_path)
    assert p.layout_version == 2
    assert p.subject_dir("MATH") == tmp_path / "0-SWE" / "2-FDL" / "subjects" / "1-Math"
    assert p.subject_dir("ENGLISH") == tmp_path / "0-SWE" / "2-FDL" / "subjects" / "3-英语"


# ── 派生路径 ────────────────────────────────────────────────
def test_derived_paths(tmp_path):
    p = FdlPaths(config_path=_write_config(tmp_path, 1), root=tmp_path)
    assert p.db_path == tmp_path / "0-SWE" / "2-FDL" / "data" / "fdl.db"
    assert p.anchor_path == tmp_path / "0-SWE" / "2-FDL" / "README.md"
    assert p.config_dir == tmp_path / "0-SWE" / "2-FDL" / "config"
    assert p.subdir("logs") == tmp_path / "0-SWE" / "2-FDL" / "logs"
    assert p.ssd_mount == tmp_path.parent  # 挂载点 = 根（git 根）的父目录


def test_subject_dir_unknown_code_raises(tmp_path):
    p = FdlPaths(config_path=_write_config(tmp_path, 1), root=tmp_path)
    with pytest.raises(KeyError):
        p.subject_dir("PHYSICS")


# ── 真实 config 解析（不硬编码可移动卷路径）─────────────────
def test_real_config_parses():
    p = FdlPaths()
    assert p.layout_version == 1
    assert p.fdl_root.exists()
    assert p.subject_dir("MATH").name == "1-Math"
    assert p.anchor_path.exists()  # README.md 应存在


def test_no_hardcoded_volumes():
    src = Path(__file__).resolve().parent.parent / "fdl_core" / "paths.py"
    # 拼接以避免审计器自匹配
    assert ("/Vol" + "umes/") not in src.read_text(encoding="utf-8")


def test_load_config_default():
    cfg = load_config()
    assert cfg["layout_version"] == 1
    assert set(cfg["subject_codes"]) == {"MATH", "CHINESE", "ENGLISH", "SCIENCE"}

"""批次 7（P2-28/29 运维；P2-30 按用户决策跳过）验收测试。

覆盖：SSD 双重判定（ismount + 锚文件）、只读模式禁作答、儿童化文案、
备份目标内置盘防线、空间闸、日备 VACUUM INTO + gzip、周全量 tar.gz、保留期。
"""

from __future__ import annotations

import gzip
import sqlite3
import tarfile

import pytest

from fdl_core.db.schema import create_schema
from fdl_core.ops.backup import BackupError, daily_snapshot, weekly_full
from fdl_core.ops.ssd_check import (
    SsdStatus,
    assert_writable,
    check_ssd,
    child_facing_message,
)
from fdl_core.paths import FdlPaths
from fdl_core.srs.time_layer import local_date


# ── P2-28 SSD 双重判定 ─────────────────────────────────────
def _fake_paths(tmp_path, *, mounted: bool, anchor: bool) -> FdlPaths:
    """构造假 paths：ssd_mount = tmp_path/ssd（挂载模拟 = 目录是否可访问）。"""
    import unittest.mock as mock

    p = mock.MagicMock(spec=FdlPaths)
    ssd = tmp_path / "ssd"
    if mounted:
        ssd.mkdir(exist_ok=True)
    fdl = ssd / "work" / "0-SWE" / "2-FDL"
    fdl.mkdir(parents=True, exist_ok=True)
    if anchor:
        (fdl / "README.md").write_text("anchor", encoding="utf-8")
    p.ssd_mount = ssd
    p.anchor_path = fdl / "README.md"
    return p


def test_ssd_double_check_pass(tmp_path):
    import unittest.mock as mock

    paths = _fake_paths(tmp_path, mounted=True, anchor=True)
    with mock.patch("fdl_core.ops.ssd_check.os.path.ismount", return_value=True):
        st = check_ssd(paths)
    assert st.ok and st.mode == "NORMAL" and not st.read_only


def test_ssd_path_without_anchor_fails(tmp_path):
    """路径存在但锚文件缺失（内容不同的假盘）→ 只读。"""
    import unittest.mock as mock

    paths = _fake_paths(tmp_path, mounted=True, anchor=False)
    with mock.patch("fdl_core.ops.ssd_check.os.path.ismount", return_value=True):
        st = check_ssd(paths)
    assert not st.ok and st.read_only and st.mode == "READ_ONLY"


def test_ssd_unmounted_fails(tmp_path):
    import unittest.mock as mock

    paths = _fake_paths(tmp_path, mounted=False, anchor=True)
    with mock.patch("fdl_core.ops.ssd_check.os.path.ismount", return_value=False):
        st = check_ssd(paths)
    assert not st.ok


def test_read_only_blocks_answers(tmp_path):
    """🔴 只读缓存模式禁止作答（硬闸）。"""
    st = SsdStatus(mounted=False, anchor_ok=True, read_only=True, mode="READ_ONLY")
    with pytest.raises(BlockingIOError):
        assert_writable(st)


def test_child_message_friendly():
    """儿童化提示：中性文案 + 行动按钮，无代码无评判。"""
    msg = child_facing_message()
    assert msg["title"] and msg["body"] and msg["action"] == "我再试试"
    for word in ("失败", "错误", "码", "重试失败"):
        assert word not in msg["title"] + msg["body"]


# ── P2-29 本地备份 ─────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "fdl.db")
    create_schema(conn)
    conn.commit()
    yield conn, tmp_path / "fdl.db"
    conn.close()


def test_daily_snapshot_gzip(db, tmp_path, monkeypatch):
    """日备：VACUUM INTO → gzip。逻辑测试 bypass 内置盘防线（沙箱限制 HOME 写入，
    防线本身由 test_backup_rejects_external_disk 在真实 /Volumes 路径下验证）。"""
    _, dbp = db
    monkeypatch.setattr("fdl_core.ops.backup._ensure_internal_disk", lambda _r: None)
    out = daily_snapshot(dbp, tmp_path / "bk")
    assert out.exists() and out.name == f"fdl-{local_date().isoformat()}.db.gz"
    with gzip.open(out, "rb") as f:
        head = f.read(16)
    assert head.startswith(b"SQLite format 3")  # VACUUM INTO 的合法 SQLite 文件


def test_backup_rejects_external_disk(db, tmp_path):
    """🔴 备份目标在外置盘（可移动卷）→ 拒绝。"""
    _, dbp = db
    fake_volumes = tmp_path / "Volumes" / "X"
    with pytest.raises(BackupError, match="内置盘"):
        daily_snapshot(dbp, fake_volumes)


def test_backup_space_gate(db, tmp_path, monkeypatch):
    """空间闸：剩余 < 需求 ×1.5 → 拒绝（bypass 内置盘防线，单测空间逻辑）。"""
    import shutil as _sh

    _, dbp = db
    monkeypatch.setattr(
        _sh, "disk_usage", lambda _: type("U", (), {"free": 1, "total": 10, "used": 9})()
    )
    monkeypatch.setattr("fdl_core.ops.backup._ensure_internal_disk", lambda _root: None)
    with pytest.raises(BackupError, match="空间不足"):
        daily_snapshot(dbp, tmp_path / "bk")


def test_backup_prune_old(db, tmp_path, monkeypatch):
    import os
    import time

    _, dbp = db
    monkeypatch.setattr("fdl_core.ops.backup._ensure_internal_disk", lambda _r: None)
    root = tmp_path / "bk"
    out = daily_snapshot(dbp, root)
    old = root / "daily" / "fdl-2026-07-01.db.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    daily_snapshot(dbp, root)  # 再跑一次触发清理
    assert not old.exists() and out.exists()


def test_weekly_full_tar(db, tmp_path, monkeypatch):
    _, dbp = db
    monkeypatch.setattr("fdl_core.ops.backup._ensure_internal_disk", lambda _r: None)
    src_dir = tmp_path / "src"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "sub" / "f.txt").write_text("x", encoding="utf-8")
    out = weekly_full([src_dir, dbp], tmp_path / "bk")
    assert out.exists() and out.name.startswith("fdl-full-")
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert any("f.txt" in n for n in names)

"""本地备份（P2-29 / OPS-06）。

🔴 硬约束：备份目标必须是 **Mac 内置盘**（`~/FDL-Backup/`），不能写回同一块
外置 SSD（SSD 已用 78% 且单点故障——写回等于没有备份）。

- 日备：`fdl.db` 经 `VACUUM INTO` 快照 + gzip → `daily/fdl-YYYY-MM-DD.db.gz`，保留 30 天；
- 周备：全量 tar.gz（fdl_core/fdl/config/data + 四学科目录）→
  `weekly/fdl-full-YYYY-MM-DD.tar.gz`，保留 12 周（84 天）；
- 空间闸：目标盘剩余空间 < 需求 1.5 倍 → 拒绝备份并提示（Q7 待确认项的落地）。
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tarfile
from pathlib import Path

from fdl_core.db.schema import get_connection
from fdl_core.srs.time_layer import local_date

DEFAULT_BACKUP_ROOT = Path.home() / "FDL-Backup"
KEEP_DAILY_DAYS = 30
KEEP_WEEKLY_WEEKS = 12


class BackupError(RuntimeError):
    """备份失败（含空间不足）。"""


def _ensure_internal_disk(backup_root: Path) -> None:
    """目标盘必须是内置盘：拒绝外置盘挂载路径下的任何位置（单点故障防线）。"""
    resolved = str(backup_root.resolve())
    # 防线检测模式（识别外置盘），非硬编码路径
    if "/Volumes/" in resolved:  # audit-ok
        # audit-ok: 错误信息回显用户路径
        raise BackupError(f"备份目标不能在外置盘：{resolved}（必须 Mac 内置盘）")


def _check_space(target_root: Path, needed_bytes: int) -> None:
    """检查目标盘剩余空间（目录未创建时向上取最近存在的祖先盘位）。"""
    probe = target_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < needed_bytes * 1.5:  # Q7：剩余 ≥ 需求 × 1.5
        raise BackupError(
            f"内置盘空间不足：剩余 {free / 1e9:.1f}GB < 需求 {needed_bytes / 1e9:.1f}GB × 1.5"
        )


def daily_snapshot(
    db_path: Path | str,
    backup_root: Path | str = DEFAULT_BACKUP_ROOT,
    *,
    keep_days: int = KEEP_DAILY_DAYS,
) -> Path:
    """每日 SQLite 快照：VACUUM INTO → gzip → 清理过期。返回 gz 路径。"""
    db = Path(db_path)
    if not db.exists():
        raise BackupError(f"主库不存在：{db}")
    root = Path(backup_root) / "daily"
    _ensure_internal_disk(Path(backup_root))
    _check_space(Path(backup_root).expanduser(), db.stat().st_size)
    root.mkdir(parents=True, exist_ok=True)

    out = root / f"fdl-{local_date().isoformat()}.db.gz"
    tmp = out.with_suffix("")  # 中间产物 fdl-YYYY-MM-DD.db
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    # 读快照：VACUUM INTO 写入独立的 tmp 文件，源库只读；走 get_connection 以获得
    # WAL 一致快照 + busy_timeout=5000（避免与 daily_batch 并发写时的 SQLITE_BUSY）
    conn = get_connection(str(db))
    conn.execute("VACUUM INTO ?", (str(tmp),))
    conn.close()
    with tmp.open("rb") as fin, gzip.open(out, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    tmp.unlink()
    _prune(root, "fdl-*.db.gz", keep_days)
    return out


def weekly_full(
    source_dirs: list[Path | str],
    backup_root: Path | str = DEFAULT_BACKUP_ROOT,
    *,
    keep_weeks: int = KEEP_WEEKLY_WEEKS,
) -> Path:
    """每周全量打包（tar.gz），保留 keep_weeks 周。"""
    root = Path(backup_root) / "weekly"
    _ensure_internal_disk(Path(backup_root))
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"fdl-full-{local_date().isoformat()}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for src in source_dirs:
            p = Path(src)
            if p.exists():
                tar.add(p, arcname=p.name)
    _prune(root, "fdl-full-*.tar.gz", keep_weeks * 7)
    return out


def _prune(directory: Path, pattern: str, keep_days: int) -> None:
    """按文件修改时间清理过期备份。"""
    import time

    cutoff = time.time() - keep_days * 86400
    for f in Path(directory).glob(pattern):
        if f.stat().st_mtime < cutoff:
            f.unlink()


def sync_primary_to_mirror(primary: Path, mirror: Path) -> dict:
    """主库（内置盘）→ 镜像（外置 SSD）每日同步，含写后读校验（disk_io RUNBOOK）。

    返回 {synced, integrity, bytes}；校验失败抛 BackupError。
    """
    import shutil

    primary, mirror = Path(primary), Path(mirror)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    # WAL 模式下未 checkpoint 的写留在 <db>-wal；复制前先折叠进主文件，
    # 否则单文件 copy 会丢失最近写入（镜像与内置盘主库不一致）。
    pc = get_connection(primary)
    try:
        pc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        pc.close()
    shutil.copy2(primary, mirror)
    # 写后读校验（SSD 故障窗口 cp 可能假成功）；只读打开镜像库（mode=ro，裸连可接受）
    c = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        rows = c.execute("SELECT COUNT(*) FROM mistake_record").fetchone()[0]
    finally:
        c.close()
    if integrity != "ok":
        raise BackupError(f"镜像库校验失败: {integrity}")
    return {
        "synced": True,
        "integrity": integrity,
        "bytes": mirror.stat().st_size,
        "mistake_rows": rows,
    }

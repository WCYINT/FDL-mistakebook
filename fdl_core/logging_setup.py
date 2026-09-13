"""FDL 日志配置（C6.1）。

按月轮转：logs/fdl-{YYYY-MM}.log，月初自动新建，保留 24 个月。
对应 NFR-10 可审计要求的前置基建。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

LOGGER_NAME = "fdl"


def _prune_old_logs(logs_dir: Path, keep_months: int = 24) -> None:
    """清理超过 keep_months 个月的旧日志文件（fdl-YYYY-MM.log）。"""
    logs_dir = Path(logs_dir)
    if not logs_dir.exists():
        return
    now = datetime.now()
    cutoff_year = now.year - (now.month - 1 + keep_months) // 12
    cutoff_month = (now.month - 1 - keep_months) % 12 + 1
    # 简化：用文件名排序，只保留最近 keep_months 个不同月份的文件
    month_files: dict[str, Path] = {}
    for p in logs_dir.glob("fdl-*.log"):
        stem = p.stem  # fdl-2026-08
        parts = stem.split("-")
        if len(parts) == 3:
            month_files[stem] = p
    # 按月份字符串排序，删除最旧的
    sorted_months = sorted(month_files.keys())
    to_remove = sorted_months[:-keep_months] if len(sorted_months) > keep_months else []
    for stem in to_remove:
        try:
            month_files[stem].unlink()
        except OSError:
            pass
    del cutoff_year, cutoff_month  # 保留供未来严格按日期裁剪


def setup_logging(logs_dir: Path | str, level: int = logging.INFO) -> logging.Logger:
    """配置 fdl 根日志器。

    返回 logger，可重复调用（幂等）。主日志文件按月命名，跨月自动新建。
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    month = datetime.now().strftime("%Y-%m")
    log_file = logs_dir / f"fdl-{month}.log"

    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    _prune_old_logs(logs_dir)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取 fdl 子 logger。"""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)

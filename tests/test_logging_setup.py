"""日志配置单元测试（C6.1）。"""

from __future__ import annotations

from datetime import datetime

from fdl_core.logging_setup import get_logger, setup_logging


def test_setup_logging_creates_monthly_file(tmp_logs_dir):
    logger = setup_logging(tmp_logs_dir)
    logger.info("测试日志")
    month = datetime.now().strftime("%Y-%m")
    log_file = tmp_logs_dir / f"fdl-{month}.log"
    assert log_file.exists(), "应按月生成日志文件"
    assert "测试日志" in log_file.read_text(encoding="utf-8")


def test_setup_logging_idempotent(tmp_logs_dir):
    logger_a = setup_logging(tmp_logs_dir)
    logger_b = setup_logging(tmp_logs_dir)
    assert logger_a is logger_b, "重复调用应返回同一 logger"


def test_get_logger_namespace(tmp_logs_dir):
    setup_logging(tmp_logs_dir)
    child = get_logger("srs")
    assert child.name == "fdl.srs"

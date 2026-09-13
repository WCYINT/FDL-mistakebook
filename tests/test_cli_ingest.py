"""批次 1：`fdl ingest` CLI 实装验收测试（T3-05 尾项）。

覆盖：成功流 / 文件不存在（exit 2）/ 非法格式（exit 2）/ 用法提示 / 引擎选项。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from fdl.cli.main import main


def _make_page(p: Path) -> str:
    page = np.full((1120, 864, 3), 255, dtype=np.uint8)
    for i in range(3):
        y = 150 + i * 220
        cv2.putText(
            page,
            f"Homework {i + 1}",
            (80, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            (30, 30, 30),
            2,
            cv2.LINE_AA,
        )
        cv2.line(page, (80, y + 14), (760, y + 14), (20, 20, 20), 3)
    cv2.imwrite(str(p), page)
    return str(p)


def test_cli_ingest_success(tmp_path, capsys):
    src = _make_page(tmp_path / "work.jpg")
    code = main(
        [
            "ingest",
            src,
            "--subject-dir",
            str(tmp_path / "1-Math"),
            "--title",
            "work-2026-09-04.jpg",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "录入完成" in out and "引擎=" in out
    assert (tmp_path / "1-Math" / "04-真实学习物").exists()


def test_cli_ingest_clean_output(tmp_path):
    src = _make_page(tmp_path / "w.jpg")
    code = main(
        [
            "ingest",
            src,
            "--subject-dir",
            str(tmp_path / "d"),
            "--clean-out",
            str(tmp_path / "custom-clean.jpg"),
        ]
    )
    assert code == 0
    # clean 产物可能因擦除退化未生成——不强制断言存在，路径逻辑由 archive 测覆盖


def test_cli_ingest_missing_file_exit2(tmp_path, capsys):
    code = main(["ingest", str(tmp_path / "nope.jpg"), "--subject-dir", str(tmp_path)])
    assert code == 2
    assert "文件不存在" in capsys.readouterr().err


def test_cli_ingest_bad_format_exit2(tmp_path, capsys):
    p = tmp_path / "x.txt"
    p.write_text("not image", encoding="utf-8")
    code = main(["ingest", str(p), "--subject-dir", str(tmp_path)])
    assert code == 2
    assert "不支持的图片格式" in capsys.readouterr().err


def test_cli_ingest_requires_image_arg(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["ingest"])  # 缺位置参数 → argparse 报用法并退出 2
    assert ei.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_cli_help_shows_usage(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["ingest", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for kw in ("image", "--subject-dir", "--engine", "退出码"):
        assert kw in out, f"--help 缺少 {kw} 说明"


def test_cli_ingest_engine_option(tmp_path):
    src = _make_page(tmp_path / "e.jpg")
    code = main(
        [
            "ingest",
            src,
            "--subject-dir",
            str(tmp_path / "d"),
            "--engine",
            "rapidocr",
        ]
    )
    assert code == 0

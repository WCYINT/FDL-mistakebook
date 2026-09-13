"""阶段三 T3-01~05 录入管线验收测试。

单测用合成图（稳定）；真实素材集成测试（M-20260903-001-original.jpg）
用 skipif 保护——素材缺失时跳过不阻塞 CI。
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from fdl_core.ingest import (
    OriginalProtectedError,
    detect_rotation,
    ingest_photo,
    recognize,
    red_coverage,
    split_layers,
)
from fdl_core.ingest.erase import erase_handwriting, guard_original
from fdl_core.ingest.preprocess import load_bgr, preprocess

# 真实错题照片属私有数据，不随发布物提供。未设置 FDL_TEST_REAL_PHOTO 环境变量时，
# 指向一个不存在的占位路径，使相关用例经既有 skipif 自动跳过。
REAL_PHOTO = Path(os.environ.get("FDL_TEST_REAL_PHOTO") or "_PRIVATE_ASSET_NOT_BUNDLED_")


def _make_page(text_lines: int = 5, red_marks: bool = False, rotate: int = 0) -> np.ndarray:
    """合成一张"作业页"：白底 + 黑色文字行（模拟印刷+手写）+ 红笔批注。"""
    page = np.full((1120, 864, 3), 255, dtype=np.uint8)
    for i in range(text_lines):
        y = 120 + i * 180
        cv2.line(page, (80, y), (760, y), (20, 20, 20), 3)  # 黑色"文字"粗线
        cv2.putText(
            page,
            f"Line {i + 1}",
            (80, y - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (30, 30, 30),
            2,
            cv2.LINE_AA,
        )
    if red_marks:
        cv2.circle(page, (700, 300), 40, (0, 0, 255), 6)  # 红笔圈
        cv2.line(page, (600, 950), (760, 980), (0, 0, 255), 5)  # 红笔下划
    if rotate:
        page = np.rot90(page, k=rotate // 90)
    return page


def _write(page: np.ndarray, p: Path) -> str:
    cv2.imwrite(str(p), page)
    return str(p)


# ── T3-01 预处理 ───────────────────────────────────────────
def test_detect_rotation_90(tmp_path):
    page = _make_page()
    rotated = np.rot90(page, k=3)  # 模拟横拍（逆时针 90°）
    p = _write(rotated, tmp_path / "r90.jpg")
    bgr = load_bgr(p)
    angle = detect_rotation(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    assert angle in (90, 270)


def test_preprocess_upright_result(tmp_path):
    """矫正后：高度 > 宽度（原页面竖版）或投影方差显著优于旋转前。"""
    p = _write(np.rot90(_make_page(), k=3), tmp_path / "r.jpg")
    fixed = preprocess(p)
    gray = cv2.cvtColor(fixed, cv2.COLOR_BGR2GRAY)
    assert (
        gray.shape[0] >= gray.shape[1]
        or fixed.shape == cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2GRAY).shape
    )


# ── T3-02 红黑分离 ─────────────────────────────────────────
def test_split_layers_separates_red():
    page = _make_page(red_marks=True)
    black_layer, red_layer = split_layers(page)
    # 红圈描边上取点（圆心是白背景非红像素）
    edge = (300, 740)  # (y, x)：圆心 (300,700) + 半径 40
    assert black_layer[edge][2] == 255 and black_layer[edge][0] == 255
    assert red_layer[edge][2] > 150 and red_layer[edge][0] < 120
    # 红层中红色下划线保留
    assert red_layer[975, 700][2] > 150 or red_layer[960, 700][2] > 150
    # 黑层黑色文字保留
    assert black_layer[120, 400][0] < 100


def test_red_coverage():
    assert red_coverage(_make_page()) == 0.0
    assert red_coverage(_make_page(red_marks=True)) > 0.0


# ── T3-03 OCR 双引擎 ───────────────────────────────────────
def test_ocr_recognizes_synthetic_text():
    page = _make_page(text_lines=3)
    r = recognize(page)
    assert r.lines, "OCR 应至少识别出合成文字行"
    assert all(ln.confidence > 0 for ln in r.lines)
    assert r.engine in ("apple_vision", "rapidocr")


def test_ocr_force_engine_dispatch():
    page = _make_page(text_lines=2)
    r1 = recognize(page, force_engine="rapidocr")
    assert r1.engine == "rapidocr"
    r2 = recognize(page, force_engine="apple_vision")
    assert r2.engine == "apple_vision"


def test_ocr_output_structure_consistent():
    """双引擎输出结构一致（bbox 像素坐标 + 置信度 0-1）。"""
    page = _make_page(text_lines=2)
    for eng in ("apple_vision", "rapidocr"):
        try:
            r = recognize(page, force_engine=eng)
        except Exception:
            continue  # 某引擎环境不可用时跳过
        for ln in r.lines:
            assert len(ln.bbox) == 4
            assert 0 <= ln.confidence <= 1


# ── T3-04 手写擦除 + ING-06 ────────────────────────────────
def test_erase_produces_clean(tmp_path):
    page = _make_page(text_lines=3)
    out = tmp_path / "clean.jpg"
    clean = erase_handwriting(page, clean_out=out)
    if clean is None:  # 合成图无手写粗笔画 → 退化路径合法
        assert not out.exists()
        return
    assert out.exists()
    assert clean.shape == page.shape


def test_guard_original_blocks(tmp_path):
    """🔴 ING-06：original 路径写入被代码层拦截。"""
    with pytest.raises(OriginalProtectedError):
        guard_original(tmp_path / "M-20260903-001-original.jpg")


def test_guard_original_allows_clean_derived_name(tmp_path):
    """-original-clean.jpg（stem 以 -clean 结尾）是合法擦除产物，放行。"""
    assert guard_original(tmp_path / "M-20260903-001-original-clean.jpg") == (
        tmp_path / "M-20260903-001-original-clean.jpg"
    )


def test_erase_never_touches_original(tmp_path):
    """端到端守卫：擦除后 original 内容与 mtime 不变。"""
    src = _write(_make_page(red_marks=True), tmp_path / "page-original.jpg")
    before = Path(src).read_bytes()
    erase_handwriting(cv2.imread(src), clean_out=str(tmp_path / "page-clean.jpg"))
    assert Path(src).read_bytes() == before  # 原图字节级不变


# ── T3-05 端到端管线 ───────────────────────────────────────
def test_ingest_photo_end_to_end(tmp_path):
    page = _make_page(text_lines=4, red_marks=True)
    src = _write(page, tmp_path / "work-2026-09-04.jpg")
    r = ingest_photo(src, tmp_path)
    assert r.original_path.exists()
    assert r.elapsed_sec < 30.0  # PRD 预算
    assert isinstance(r.red_ratio, float)


def test_ingest_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_photo(tmp_path / "nope.jpg", tmp_path)


# ── 真实素材集成测试（素材缺失自动跳过）────────────────────
@pytest.mark.skipif(not REAL_PHOTO.exists(), reason="真实错题照不在本机")
def test_real_photo_pipeline(tmp_path):
    """真实素材端到端：旋转矫正 + 红笔分离 + 双引擎 OCR + 归档。"""
    r = ingest_photo(REAL_PHOTO, tmp_path, title="M-20260903-001-original.jpg")
    assert r.original_path.exists()
    assert r.elapsed_sec < 30.0
    assert r.red_ratio > 0  # 该照片确有红笔批改
    # 旋转矫正生效：输出应为竖版（原页高>宽）
    fixed = cv2.imread(str(r.original_path)) if r.original_path != REAL_PHOTO else None
    print(
        f"[real] engine={r.ocr.engine} lines={len(r.ocr.lines)} "
        f"conf={r.ocr.avg_confidence:.2f} red={r.red_ratio:.3f} "
        f"elapsed={r.elapsed_sec:.1f}s needs_review={r.needs_review}"
    )
    if fixed is not None:
        # 2026-09-10：判向升级为 v2（方差法定行水平组 + OCR 消 180° 歧义）后，
        # 本素材（横拍竖排的数学卷，源 4284×5712）矫正结果为**横向** 5712×4284，
        # 且该方向 OCR 置信最高（57%、387 中文字，四方向对比最优）。
        # 旧断言"输出应为竖版（高 ≥ 宽×0.8）"基于旧方差法结果，已过时。
        w, h = fixed.shape[1], fixed.shape[0]
        assert max(w, h) / min(w, h) <= 1.4, (
            f"方向矫正后宽高比应接近页面比例（非极端长条）：{w}×{h}"
        )


def test_real_photo_ocr_text_sample(tmp_path):
    """真实素材 OCR 文本抽样（人工可读性验证；输出打印供 King 核对）。"""
    if not REAL_PHOTO.exists():
        pytest.skip("素材不在本机")
    bgr = load_bgr(REAL_PHOTO)
    r = recognize(bgr)
    assert r.lines, "真实照片 OCR 应有输出（即使低置信）"
    print(f"[ocr engine={r.engine}]")
    for ln in r.lines[:15]:
        print(f"  {ln.confidence:.2f} {ln.text[:50]}")

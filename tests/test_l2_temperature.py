"""MiniMaxClient.temperature 单测（2026-09-12 King 拍板：默认低温）。

背景：实测同一证据两次调用置信度 0.75 vs 0.92，波动破坏
"高置信自动写入"闸门的可复现性。低温收敛采样随机性。

只测配置逻辑，不打真实网络。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "fdl_test_minimax", _ROOT / "fdl_core" / "l2" / "minimax.py"
)
_mm = importlib.util.module_from_spec(_SPEC)
# 必须先注册进 sys.modules：dataclass 处理时会按 __module__ 反查模块字典，
# 未注册会抛 AttributeError('NoneType' object has no attribute '__dict__')
sys.modules["fdl_test_minimax"] = _mm
_SPEC.loader.exec_module(_mm)

MiniMaxClient = _mm.MiniMaxClient


def test_default_temperature_is_low():
    c = MiniMaxClient(api_key="dummy")
    assert c.temperature == 0.1, "默认必须低温（闸门可复现）"


def test_temperature_env_override(monkeypatch):
    monkeypatch.setenv("FDL_LLM_TEMPERATURE", "0.7")
    assert MiniMaxClient(api_key="dummy").temperature == 0.7
    monkeypatch.delenv("FDL_LLM_TEMPERATURE")
    assert MiniMaxClient(api_key="dummy").temperature == 0.1


def test_temperature_explicit_and_clamped():
    assert MiniMaxClient(api_key="dummy", temperature=1.5).temperature == 1.5
    assert MiniMaxClient(api_key="dummy", temperature=-3).temperature == 0.0
    assert MiniMaxClient(api_key="dummy", temperature=99).temperature == 2.0


def test_temperature_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("FDL_LLM_TEMPERATURE", "not-a-number")
    assert MiniMaxClient(api_key="dummy").temperature == 0.1


def test_body_carries_temperature(monkeypatch):
    """请求体必须带 temperature（防"参数存在但没发出去"的假实现）。"""
    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 1, "completion_tokens": 1},
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json or {})
        return FakeResp()

    monkeypatch.setattr(_mm.requests, "post", fake_post)
    c = MiniMaxClient(api_key="dummy", temperature=0.2)
    c.ask("sys", "user")
    assert captured.get("temperature") == 0.2
    assert captured.get("data_handling") == "no_training"  # G2 守门不受影响


def test_attributed_by_includes_vision_analysis():
    """VISION_ANALYSIS 必须是合法值（生产数据早已在用，枚举此前漏收）。"""
    from fdl_core.mistakes.attribution import ATTRIBUTED_BY_VALUES, validate_monster_tag

    assert "VISION_ANALYSIS" in ATTRIBUTED_BY_VALUES
    validate_monster_tag("METHOD", "VISION_ANALYSIS")  # 不抛 = 合法
    with pytest.raises(ValueError, match="LLM_SUGGEST"):
        validate_monster_tag("METHOD", "LLM_SUGGEST")  # 既有禁令不变

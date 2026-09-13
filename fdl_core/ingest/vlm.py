"""VLM（多模态大模型）整页直读 —— 错题结构化提取适配器。

2026-09-10：调研 agent-lightning（Agentic RL 训练框架，需 GPU + torch/verl +
训练数据 + 奖励函数，且 FDL 零标注数据无法训练）后判定**不适用**。
正确路线是 PPT「输入层」已定原则：**多模态直读，跳过脆弱的 OCR/版面预处理**——
由 VLM 直接看整页作业，输出结构化题目（题号/题干/作答/批改/是否错/错因）。

设计：
- OpenAI 兼容 `/v1/chat/completions` vision 接口（Qwen-VL / GPT-4o / 本地 vLLM 等均可）
- **零新增依赖**：仅用标准库 urllib（不引入 openai SDK，不破坏无 torch 约束）
- **配置驱动**：读 config/vlm.yaml；未启用/未配 key/调用失败 → 一律返回 None，
  调用方回退到现有 CV 切分流程（绝不阻塞主流程）
- **不压缩**：图片以原图 base64 发送（King 规则：FDL 图片零压缩）

配置示例（config/vlm.yaml）：
```yaml
enabled: true
base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"  # audit-ok: 文档示例
api_key_env: DASHSCOPE_API_KEY     # 从环境变量读，不落盘密钥
model: "qwen2.5-vl-72b-instruct"
timeout_sec: 120
max_side: 2000                      # 发送前等比缩放上限（仅影响传输，不改原图）
```
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = _ROOT / "config" / "vlm.yaml"

SYSTEM_PROMPT = (
    "你是小学作业批改助手。给你一张已作答并批改过的作业页照片"
    "（红笔为教师批改：打叉/圈画=答错；手写黑字为学生作答；印刷体为原题）。\n"
    "请逐题识别并只输出 JSON，不要任何解释文字，格式：\n"
    '{"questions":[{"no":"题号如 一、1 / 二、2", "stem":"题干原文",'
    ' "answer":"学生作答", "marked":"红笔批改情况", "is_wrong":true/false,'
    ' "reason":"错因（概念不清/计算失误/审题偏差/规范缺失/其他）",'
    ' "y_range":[起始y比例, 结束y比例]}]}\n'
    "要求：只输出真实存在的题目；y_range 为该题在图中的垂直范围（0-1 比例），"
    "用于裁剪子图；无法判断时 is_wrong 填 false 并在 reason 写“需人工确认”。"
)


def _load_config() -> dict:
    """读 config/vlm.yaml（不存在/损坏 → 返回 {}，视为未启用）。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception:
        return {}
    cfg: dict = {}
    # 极简 YAML 解析（避免引入 pyyaml 依赖；只支持 key: value 与 # 注释）
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        if v.lower() in ("true", "false"):
            cfg[k.strip()] = v.lower() == "true"
        elif v.isdigit():
            cfg[k.strip()] = int(v)
        else:
            cfg[k.strip()] = v
    return cfg


def is_enabled() -> bool:
    cfg = _load_config()
    if not cfg.get("enabled"):
        return False
    key_env = cfg.get("api_key_env") or "VLM_API_KEY"
    return bool(os.environ.get(key_env))


def _encode_image(bgr: np.ndarray, max_side: int = 2000) -> str:
    """原图编码为 data URI（仅等比缩放用于传输，不改变已落盘的原图）。"""
    h, w = bgr.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", bgr)  # PNG 无损
    if not ok:
        raise RuntimeError("图片编码失败")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def read_page(bgr: np.ndarray) -> list[dict] | None:
    """整页直读 → 结构化题目列表；未启用/失败一律返回 None（调用方回退）。"""
    cfg = _load_config()
    if not cfg.get("enabled"):
        return None
    base_url = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    key_env = cfg.get("api_key_env") or "VLM_API_KEY"
    api_key = os.environ.get(key_env, "")
    if not (base_url and model and api_key):
        return None
    timeout = int(cfg.get("timeout_sec") or 120)
    max_side = int(cfg.get("max_side") or 2000)

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _encode_image(bgr, max_side)}},
                    {"type": "text", "text": "请按格式输出本页全部题目的 JSON。"},
                ],
            },
        ],
    }
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[vlm] 调用失败，回退本地流程：{type(e).__name__}: {e}")
        return None

    # 抽取 JSON（模型可能带 ```json 包裹）
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    qs = data.get("questions")
    return qs if isinstance(qs, list) and qs else None

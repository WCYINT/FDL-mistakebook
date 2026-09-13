#!/usr/bin/env bash
# 禁 PyTorch 检查（C3.3，PRD §4.4 #15 否决项）
# 阻断 torch / funasr 出现在依赖树，保证 ASR 走 GGUF/sherpa-onnx 路线
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

fail=0

# 解析 Python：本地优先 venv，CI 回退系统 python3
PYTHON="$ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi

# 1. 检查 Python 环境中是否已装 torch
if "$PYTHON" -c "import torch" >/dev/null 2>&1; then
  echo "[FAIL] 依赖树中检测到 torch 已安装，违反 §4.4 #15 否决项"
  fail=1
else
  echo "[OK] 依赖树无 torch"
fi

# 2. 检查 requirements 是否声明 torch/funasr
if grep -qiE "^(torch|funasr|fun-asr)" "$ROOT/requirements.txt" 2>/dev/null; then
  echo "[FAIL] requirements.txt 含 torch/funasr"
  fail=1
else
  echo "[OK] requirements.txt 无 torch/funasr"
fi

# 3. 检查 lock 文件是否含 torch
if grep -qiE "^(torch|funasr)==" "$ROOT/requirements.lock" 2>/dev/null; then
  echo "[FAIL] requirements.lock 含 torch/funasr"
  fail=1
else
  echo "[OK] requirements.lock 无 torch/funasr"
fi

if [ "$fail" -eq 0 ]; then
  echo "== 禁 PyTorch 检查通过 =="
  exit 0
else
  echo "== 禁 PyTorch 检查失败 =="
  exit 1
fi

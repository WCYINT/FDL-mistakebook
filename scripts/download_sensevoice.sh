#!/usr/bin/env bash
# SenseVoice-Small int8 模型下载脚本（C1.5）
# 走 sherpa-onnx 路线（禁 PyTorch，见 PRD §4.4 #15 否决项）
# 产出：models/sensevoice/model.int8.onnx + tokens.txt
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MODEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/models/sensevoice"
mkdir -p "$MODEL_DIR"
cd "$MODEL_DIR"

URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
EXPECTED_SHA256="7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
ARCHIVE="sensevoice-int8.tar.bz2"
EXTRACT_DIR="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"

echo "[sensevoice] 开始下载（约 163MB）..."
curl -SL --retry 3 --retry-delay 5 -o "$ARCHIVE" "$URL"
echo "[sensevoice] 下载完成，校验 sha256..."

if command -v shasum >/dev/null 2>&1; then
  echo "$EXPECTED_SHA256  $ARCHIVE" | shasum -a 256 -c -
else
  echo "$EXPECTED_SHA256  $ARCHIVE" | sha256sum -c -
fi

echo "[sensevoice] 解压..."
tar xjf "$ARCHIVE"

echo "[sensevoice] 规范化文件..."
mv "$EXTRACT_DIR/model.int8.onnx" ./model.int8.onnx
mv "$EXTRACT_DIR/tokens.txt" ./tokens.txt
rm -rf "$EXTRACT_DIR" "$ARCHIVE"

echo "[sensevoice] 完成，模型文件："
ls -lh "$MODEL_DIR/"

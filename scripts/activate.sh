#!/usr/bin/env bash
# FDL 虚拟环境激活脚本（C1.2）
# 用法：source scripts/activate.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
VENV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/.venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[activate] 未找到虚拟环境 $VENV_DIR，请先运行 scripts/bootstrap.sh" >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "[activate] 已激活 FDL 虚拟环境：$VENV_DIR"

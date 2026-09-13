#!/usr/bin/env bash
# FDL 一键初始化脚本（C7.3）
# 验收：新机器 5 分钟内见 Frank UI 占位
# 流程：venv -> pip -> mkdir -> git init -> offline_audit -> fdl doctor
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"           # 项目根
GIT_ROOT="$(cd "$ROOT/../.." && pwd)"          # 项目根的上一级（数据根）

PYTHON="${PYTHON:-python3}"

echo "== FDL bootstrap 开始 =="

# 0. 检查 Python 3.11+
"$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || {
  echo "[FAIL] 需要 Python 3.11+，当前：$("$PYTHON" --version 2>&1)" >&2
  exit 1
}

# 1. venv
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[1/6] 创建 venv..."
  "$PYTHON" -m venv "$ROOT/.venv"
else
  echo "[1/6] venv 已存在，跳过"
fi

# 2. pip 安装依赖
echo "[2/6] 安装依赖..."
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" -r "$ROOT/requirements-dev.txt"

# 3. 安装 fdl 包（可编辑模式，供 fdl CLI 使用）
echo "[3/6] 安装 fdl 包..."
"$ROOT/.venv/bin/pip" install -q -e "$ROOT"

# 4. 创建目录
echo "[4/6] 创建目录..."
mkdir -p "$ROOT"/data \
  "$ROOT"/logs/audit/state_transitions \
  "$ROOT"/logs/audit/param_changes \
  "$ROOT"/logs/audit/auth_access \
  "$ROOT"/logs/alerts \
  "$ROOT"/backups \
  "$ROOT"/models/sensevoice \
  "$ROOT"/assets/fonts

# 5. git init（幂等）
if [ ! -d "$GIT_ROOT/.git" ]; then
  echo "[5/6] git init 于 $GIT_ROOT..."
  (cd "$GIT_ROOT" && git init -q)
else
  echo "[5/6] git 已初始化，跳过"
fi

# 6. 离线审计 + fdl doctor
echo "[6/6] 离线审计 + fdl doctor..."
"$ROOT/.venv/bin/python" "$ROOT/scripts/offline_audit.py" || echo "[warn] 离线审计未全过（见上方输出）"
"$ROOT/.venv/bin/fdl" doctor

echo ""
echo "== FDL bootstrap 完成 =="
echo "下一步：source scripts/activate.sh 激活环境"

#!/usr/bin/env bash
# 禁用词扫描（C4.3 + P2-19 / UX-05，儿童友好）
# P2-19 扩展：精确扫描 **Frank 触达面** = fdl/ui/（页面+组件）+ fdl/ui/i18n/（集中文案表）。
# fdl_core 是 L0 内核（Frank 不可见），其技术性 docstring 不在本扫描范围。
# 🔴 文案必须集中在 fdl/ui/i18n/zh.yaml——页面硬编码 Frank 可见文案同样会被扫描命中。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# PRD §2.3 原则 5/6 禁用词（考试/评价隐喻 + 负面评判 + 红叉语气）
BANNED=("失败" "错误" "不行" "笨蛋" "太笨" "愚蠢" "你不会" "做错了" "不合格" "差劲" "不及格" "正确率" "你错了")

# 扫描范围：Frank 触达面（UI 层 + 集中文案表）
TARGETS=(
  "$ROOT/fdl/ui"
)

fail=0
for word in "${BANNED[@]}"; do
  hits=$(grep -rIn --exclude-dir=__pycache__ --exclude-dir=.venv \
    -e "$word" "${TARGETS[@]}" 2>/dev/null | grep -v "禁用词" || true)
  if [ -n "$hits" ]; then
    echo "[FAIL] 发现禁用词「$word」："
    echo "$hits"
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "== 禁用词扫描通过（UX-05 文案层 + 集中文案表）=="
  exit 0
else
  echo "== 禁用词扫描失败 =="
  exit 1
fi

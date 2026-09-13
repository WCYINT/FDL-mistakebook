#!/usr/bin/env bash
# FDL macOS 系统依赖检查（C1.3）
# 检查 7 项，全部通过退出 0，任一失败退出非零
# 用途：阶段三 ING-02 Apple Vision OCR 的前置检查
set -u

PASS=0
FAIL=0

check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  [OK]   $name"
    PASS=$((PASS + 1))
  else
    echo "  [FAIL] $name"
    FAIL=$((FAIL + 1))
  fi
}

echo "== FDL macOS 依赖检查（7 项）=="

# 1. Xcode Command Line Tools（Apple Vision 底层依赖）
check "Xcode CLT" xcode-select -p

# 2. Homebrew
check "Homebrew" brew --version

# 3. macOS 版本 >= 13（Vision 框架要求）
check "macOS >= 13" bash -c '[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 13 ]'

# 4. Python >= 3.11
check "Python >= 3.11" bash -c 'python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"'

# 5. ImageMagick（图片处理，阶段三 OCR 预处理）
check "ImageMagick" bash -c 'command -v magick >/dev/null || command -v convert >/dev/null'

# 6. pyobjc Vision（Apple Vision OCR，需 pip 安装 pyobjc-framework-Vision 后通过）
check "pyobjc Vision" bash -c 'python3 -c "import Vision"'

# 7. pyobjc Quartz（Vision 图像帧处理，需 pip 安装后通过）
check "pyobjc Quartz" bash -c 'python3 -c "import Quartz"'

echo ""
echo "== 结果：$PASS 通过，$FAIL 失败 =="

if [ "$FAIL" -eq 0 ]; then
  echo "== 全部 7 项通过，Apple Vision OCR 可调用 =="
  exit 0
else
  echo "== 存在失败项（pyobjc 相关需先 pip install -r requirements.txt）=="
  exit 1
fi

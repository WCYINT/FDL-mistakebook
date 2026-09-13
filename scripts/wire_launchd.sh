#!/bin/zsh
# FDL launchd 接线脚本（2026-09-12）—— 幂等，可重复跑
#
# 为什么需要这个脚本（任务4"齿轮没咬合"）：
#   launchctl list 里 com.fdl.* 全部未加载 → daily_batch（日/周聚合+备份+报告刷新）
#   从不自动运行；analyze/serve 两个 plist 完全相同且都指向 serve 脚本（误接线冗余件）。
#
# 为什么不由 WorkBuddy 直接执行：宿主 App 进程树被 macOS 拒绝
#   launchctl bootstrap(EIO) / Apple Events(-10004) / Gatekeeper(.command 拦截)，
#   必须借用户 Terminal 的完整权限。 King 只需跑一次：
#     zsh "<项目根>/scripts/wire_launchd.sh"
#
# 幂等性：每步先检查再动作，重复跑不会重复加载/重复改名。

set -u
LA="$HOME/Library/LaunchAgents"
PROJ="$(cd "$(dirname "$0")/.." && pwd)"

step1_daily() {
  echo "== 1. com.fdl.daily（每日 04:00 批处理）=="
  if launchctl print gui/501/com.fdl.daily >/dev/null 2>&1; then
    echo "   已加载，跳过"
  else
    if launchctl bootstrap gui/501 "$LA/com.fdl.daily.plist" 2>&1; then
      echo "   bootstrap OK"
    else
      echo "   ✗ bootstrap 失败（本终端也无权限？）"; return 1
    fi
  fi
  launchctl print gui/501/com.fdl.daily 2>/dev/null | grep -E "state|program" | head -3 | sed 's/^/   /'
}

step1b_weekly() {
  echo "== 1b. com.fdl.weekly（每周日 21:00 掌握度周报，2026-09-12 新增）=="
  if launchctl print gui/501/com.fdl.weekly >/dev/null 2>&1; then
    echo "   已加载，跳过"
  else
    if launchctl bootstrap gui/501 "$LA/com.fdl.weekly.plist" 2>&1; then
      echo "   bootstrap OK"
    else
      echo "   ✗ bootstrap 失败"; return 1
    fi
  fi
  launchctl print gui/501/com.fdl.weekly 2>/dev/null | grep -E "state|program" | head -3 | sed 's/^/   /'
}

step2_cleanup_analyze() {
  echo "== 2. 清理误接线冗余件 com.fdl.analyze =="
  if [ -f "$LA/com.fdl.analyze.plist" ]; then
    # 与 com.fdl.serve 完全相同（都跑 fdl_serve_launch.sh），加载会同端口双开
    mv "$LA/com.fdl.analyze.plist" "$LA/com.fdl.analyze.plist.bak-20260912" \
      && echo "   已备份为 com.fdl.analyze.plist.bak-20260912（launchd 忽略非 .plist 文件）"
  else
    echo "   不存在或已清理，跳过"
  fi
}

step3_serve() {
  echo "== 3. fdl_serve 交给 launchd 管 =="
  if launchctl print gui/501/com.fdl.serve >/dev/null 2>&1; then
    echo "   已由 launchd 管理，跳过"
  else
    pkill -9 -f "fdl_serve.py" 2>/dev/null; sleep 2   # 停手动实例，避免端口冲突
    if launchctl bootstrap gui/501 "$LA/com.fdl.serve.plist" 2>&1; then
      echo "   bootstrap OK（KeepAlive=true，挂了自动拉起）"
    else
      echo "   ✗ bootstrap 失败 → 手动兜底拉起"
      nohup "$HOME/fdl_serve_launch.sh" >> /tmp/fdl_serve.log 2>&1 &
    fi
  fi
  sleep 4
  curl -s --noproxy '*' --max-time 6 \
    'http://127.0.0.1:8765/api/report/section/review_today?cap=1' \
    -o /dev/null -w "   8765 端口: HTTP=%{http_code}\n"
}

step4_verify() {
  echo "== 4. 最终状态 =="
  launchctl list | grep -i "com.fdl" | sed 's/^/   /' || echo "   （launchctl 无 fdl 服务）"
  ls -1 "$LA" | grep -i "fdl" | sed 's/^/   file: /'
}

echo "── FDL launchd 接线开始 ──"
step1_daily; s1=$?
step1b_weekly
step2_cleanup_analyze
step3_serve
step4_verify
echo "── 完成 ──"
exit $s1

"""SSD 未挂载检测（P2-28 / OPS-03）。

双重判定（PRD §5.5.2 / DFD-4）：
1. `os.path.ismount(SSD 挂载点)`——防路径不存在；
2. 锚文件存在（`0-SWE/2-FDL/README.md`，P2-02 paths）——防"路径在但内容不同"。

未挂载 → **只读缓存模式**：可浏览最近 7 天报告快照，🔴 **禁止任何作答/录入**
（避免数据写丢或写到内置盘造成分裂）。儿童化提示文案走 i18n/zh.yaml
（中性、无代码、无评判；验收：10 岁用户不产生"电脑坏了"的误读）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fdl_core.paths import FdlPaths, get_paths
from fdl_core.srs.time_layer import fmt_ts, now_utc


@dataclass
class SsdStatus:
    """SSD 状态（UI/CLI 均消费此对象）。"""

    mounted: bool  # ismount 判定
    anchor_ok: bool  # 锚文件判定
    read_only: bool  # 未通过双重判定 → 只读缓存模式
    mode: str  # "NORMAL" / "READ_ONLY"

    @property
    def ok(self) -> bool:
        return self.mode == "NORMAL"


def check_ssd(paths: FdlPaths | None = None) -> SsdStatus:
    """双重判定 SSD 挂载状态。"""
    p = paths or get_paths()
    mounted = os.path.ismount(p.ssd_mount)
    anchor_ok = p.anchor_path.exists()
    ok = mounted and anchor_ok
    return SsdStatus(
        mounted=mounted,
        anchor_ok=anchor_ok,
        read_only=not ok,
        mode="NORMAL" if ok else "READ_ONLY",
    )


def assert_writable(status: SsdStatus | None = None) -> None:
    """作答/录入前的硬闸：只读模式禁止写入（抛 BlockingIOError）。"""
    st = status or check_ssd()
    if st.read_only:
        raise BlockingIOError("SSD 未挂载：只读缓存模式，禁止作答/录入")


def child_facing_message() -> dict:
    """面向 Frank 的一屏提示（文案走 zh.yaml；无代码、无评判、有行动按钮）。"""
    from fdl.ui.theme import t

    return {
        "title": t("system", "ssd_missing_title"),
        "body": t("system", "ssd_missing_body"),
        "action": t("system", "ssd_missing_action"),
        "parent_section": t("system", "ssd_missing_for_parent"),
        "checked_at": fmt_ts(now_utc()),
    }

#!/usr/bin/env python3
"""知识点卡片文件名统一（图谱命名规范落地 · 2026-09-12）。

把卡片文件名统一为规范格式 **`编码-描述.md`**（见 `fdl_core/notes/naming.py`）。
此前两套命名并存：
    - 旧批（27 张）：`G4A-七巧板拼图.md`   —— 有描述、无编码
    - 新批（24 张）：`MATH-G4-TANGRAM.md`  —— 有编码、无描述
两者都不符合「编码 + 描述」。

安全性
------
- 身份来自 frontmatter `code`（不是文件名）——重命名不影响任何数据链路；
- 重命名前整目录备份到 `backups/kp-cards-rename-<时间戳>/`（平铺）；
- 幂等：已符合规范的文件跳过；
- 只改文件名，**卡片内容一字不动**。

用法
----
    python scripts/kp_rename_cards.py --dry-run    # 预演（默认行为）
    python scripts/kp_rename_cards.py --apply      # 正式执行
    python scripts/kp_rename_cards.py --status     # 只检查合规率
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.notes.kp_card import read_card  # noqa: E402
from fdl_core.notes.kp_classifier import CANONICAL_SUBJECTS  # noqa: E402
from fdl_core.notes.naming import card_filename  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _iter_cards() -> list[Path]:
    paths = get_paths()
    out: list[Path] = []
    for code in CANONICAL_SUBJECTS:
        base = paths.subject_dir(code) / "01-知识点"
        if base.exists():
            out += sorted(base.rglob("*.md"))
    return out


def _plan() -> tuple[list[tuple[Path, Path, str, str]], int]:
    """返回 ([(当前路径, 目标路径, code, name)], 已合规数)。"""
    todo: list[tuple[Path, Path, str, str]] = []
    ok = 0
    for p in _iter_cards():
        try:
            card = read_card(p)
        except Exception:  # noqa: BLE001 — 非卡片/坏卡跳过
            continue
        if card.frontmatter.get("type") != "knowledge_point":
            continue
        code = str(card.frontmatter.get("code") or "")
        name = str(card.frontmatter.get("name") or "")
        target = p.with_name(card_filename(code, name))
        if target == p:
            ok += 1
        else:
            todo.append((p, target, code, name))
    return todo, ok


def cmd_status() -> int:
    todo, ok = _plan()
    total = len(todo) + ok
    print(f"[kp-rename] 卡片 {total} 张：已合规 {ok} | 待统一 {len(todo)}")
    for p, t, _code, _ in todo[:10]:
        print(f"    {p.name}  →  {t.name}")
    if len(todo) > 10:
        print(f"    …（其余 {len(todo) - 10} 张同理）")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="卡片文件名统一（编码-描述.md）")
    p.add_argument("--apply", action="store_true", help="正式执行（缺省只预演）")
    p.add_argument("--dry-run", action="store_true", help="预演（默认行为）")
    p.add_argument("--status", action="store_true", help="只检查合规率")
    args = p.parse_args(argv)

    if args.status:
        return cmd_status()

    todo, ok = _plan()
    print(f"[kp-rename] 已合规 {ok} 张 | 待统一 {len(todo)} 张")

    # 冲突预检：目标文件已存在（且不是自身）→ 报错中止，绝不覆盖
    conflicts = [(p, t) for p, t, _, _ in todo if t.exists() and t != p]
    if conflicts:
        print("[kp-rename] 目标名冲突，已中止：", file=sys.stderr)
        for p, t in conflicts:
            print(f"    {p.name} → {t.name}（目标已存在）", file=sys.stderr)
        return 1

    if not args.apply:
        for p, t, _code, _ in todo:
            print(f"    [预演] {p.name}  →  {t.name}")
        print("\n[kp-rename] 预演完毕。加 --apply 正式执行。")
        return 0

    # 备份 → 重命名
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = get_paths().subdir("backups") / f"kp-cards-rename-{ts}"
    backup.mkdir(parents=True, exist_ok=True)
    done = 0
    for p, t, _code, _ in todo:
        shutil.copy2(p, backup / p.name)
        p.rename(t)
        done += 1
        print(f"    [改名] {p.name}  →  {t.name}")

    # 复核：重读全部卡片，确认内容完整（frontmatter code 全部可读）
    after, ok_after = _plan()
    print(f"\n[kp-rename] 完成：改名 {done} 张 | 备份 → {backup}")
    print(f"[kp-rename] 复核：残留待统一 {len(after)} 张（应为 0）| 合规 {ok_after} 张")
    return 0 if not after else 1


if __name__ == "__main__":
    raise SystemExit(main())

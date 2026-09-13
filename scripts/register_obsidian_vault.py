#!/usr/bin/env python3
"""把知识书根目录注册为 Obsidian vault（Star Map Obsidian 链接前置）。

背景
----
星图节点的「在 Obsidian 打开」链接格式：
    obsidian://open?vault=<vault名>&file=<卡片相对路径>
Obsidian 只接受**已注册 vault 内**的文件（官方 URI 规范：open 的 vault/file 与
path 两种写法都要求文件位于已注册 vault 中）。知识书四科
（学科目录，见 `config/fdl_paths.yaml` 的 `layout.subject_dirs`）位于数据根目录下，
此前不是 vault → 链接点了没反应。本脚本把该目录注册为 vault。

做什么（幂等、可回退）
----------------------
1. 在 `<root>/.obsidian/` 建立最小 vault 目录（已存在则跳过）；
2. 在 `~/Library/Application Support/obsidian/obsidian.json` 注册
   `<root>`（已注册则跳过；写入前自动备份 obsidian.json）。

注意
----
- Obsidian 启动时读取 vault 列表：若 Obsidian 正在运行，注册需**重启 Obsidian**
  后才生效（或直接用 URI 触发，视版本而定）。
- `--remove` 可完整回退（仅移除本脚本注册的条目，不动其他 vault）。

用法
----
    python scripts/register_obsidian_vault.py --status     # 查看当前状态
    python scripts/register_obsidian_vault.py              # 注册（幂等）
    python scripts/register_obsidian_vault.py --remove     # 回退
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from fdl_core.paths import get_paths  # noqa: E402

OBSIDIAN_CFG = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"


def _load_cfg() -> dict:
    if not OBSIDIAN_CFG.exists():
        return {"vaults": {}}
    return json.loads(OBSIDIAN_CFG.read_text(encoding="utf-8"))


def _find_vault_id(cfg: dict, path: str) -> str | None:
    target = str(Path(path).expanduser().resolve())
    for vid, meta in (cfg.get("vaults") or {}).items():
        vp = str(Path(meta.get("path", "")).expanduser().resolve())
        if vp == target:
            return vid
    return None


def status(vault_root: Path) -> dict:
    cfg = _load_cfg()
    vid = _find_vault_id(cfg, str(vault_root))
    return {
        "vault_root": str(vault_root),
        "vault_name": vault_root.name,
        "dot_obsidian_exists": (vault_root / ".obsidian").exists(),
        "registered": bool(vid),
        "vault_id": vid,
        "config_path": str(OBSIDIAN_CFG),
    }


def register(vault_root: Path, *, dry_run: bool = False) -> dict:
    vault_root = Path(vault_root)
    if not vault_root.exists():
        return {"ok": False, "error": f"目录不存在：{vault_root}"}
    st = status(vault_root)
    if st["registered"] and st["dot_obsidian_exists"]:
        return {"ok": True, "action": "already", **st}

    actions: list[str] = []
    if not (vault_root / ".obsidian").exists():
        actions.append(f"mkdir {vault_root / '.obsidian'}")
    actions.append(f"register '{vault_root.name}' → {OBSIDIAN_CFG}")
    if dry_run:
        return {"ok": True, "action": "dry_run", "actions": actions, **st}

    if not (vault_root / ".obsidian").exists():
        (vault_root / ".obsidian").mkdir(parents=True, exist_ok=True)

    cfg = _load_cfg()
    if not _find_vault_id(cfg, str(vault_root)):
        if OBSIDIAN_CFG.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(OBSIDIAN_CFG, f"{OBSIDIAN_CFG}.bak-{ts}")
        vid = secrets.token_hex(8)  # 与 Obsidian 既有 id 同格式（16 hex）
        cfg.setdefault("vaults", {})[vid] = {
            "path": str(vault_root),
            "ts": int(time.time() * 1000),
        }
        OBSIDIAN_CFG.parent.mkdir(parents=True, exist_ok=True)
        OBSIDIAN_CFG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "action": "registered", "actions": actions, **status(vault_root)}


def remove(vault_root: Path, *, dry_run: bool = False) -> dict:
    cfg = _load_cfg()
    vid = _find_vault_id(cfg, str(vault_root))
    if not vid:
        return {"ok": True, "action": "not_registered"}
    if dry_run:
        return {"ok": True, "action": "dry_run_remove", "vault_id": vid}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(OBSIDIAN_CFG, f"{OBSIDIAN_CFG}.bak-{ts}")
    del cfg["vaults"][vid]
    OBSIDIAN_CFG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "action": "removed", "vault_id": vid}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="注册知识书根目录为 Obsidian vault")
    p.add_argument("--root", default=None, help="vault 根目录（默认为数据根目录）")
    p.add_argument("--status", action="store_true", help="查看状态")
    p.add_argument("--remove", action="store_true", help="回退（移除注册）")
    p.add_argument("--dry-run", action="store_true", help="只预演")
    args = p.parse_args(argv)

    vault_root = Path(args.root) if args.root else get_paths().root
    if args.status:
        r = status(vault_root)
    elif args.remove:
        r = remove(vault_root, dry_run=args.dry_run)
    else:
        r = register(vault_root, dry_run=args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

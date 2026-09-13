"""Schema 迁移运行器（幂等版本追踪，Fix 2）。

职责：
- 扫描本目录 `2026_*.py` 迁移脚本，按文件名排序（保证执行顺序）；
- 对每个迁移：若 `schema_migrations` 无记录 → 执行其 `migrate()` → 成功后登记；
  已有记录 → 跳过（幂等，可重复运行无误）；
- `--dry-run`：只打印将执行的迁移名，不执行、不改库；
- `--backfill`：把已手工跑过（但库未登记）的迁移名直接登记为已应用，
  不重跑其逻辑（用于历史迁移补登记）；
- 可直接运行：`python fdl_core/migrations/runner.py [--dry-run|--backfill]`，打印 JSON 结果。

返回结构：{applied: [...], skipped: [...], errors: [...]}

迁移脚本约定（与现有 2026_*.py 一致）：
- 模块级 `migrate() -> dict`，自行打开主库连接并执行 DDL，返回结构化结果 dict；
- 内部若吞掉异常，会把错误放进返回 dict 的 `errors` 字段，本运行器据此判定失败。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

# 路径引导：保证从仓库任意位置 `python fdl_core/migrations/runner.py` 直跑
_ROOT = Path(__file__).resolve().parent.parent.parent  # .../0-SWE/2-FDL
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent
_TS_DEFAULT = "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """确保 schema_migrations 存在（幂等；兼容未跑过 create_schema 的老库）。"""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS schema_migrations ("
        f" name TEXT PRIMARY KEY,"
        f" applied_at TEXT NOT NULL DEFAULT {_TS_DEFAULT})"
    )
    conn.commit()


def discover_migrations() -> list[str]:
    """返回按文件名排序的迁移名（不含 .py），排除本运行器自身。"""
    return sorted(p.stem for p in MIGRATIONS_DIR.glob("2026_*.py") if p.stem != "runner")


def _load_module(name: str):
    """按文件名加载迁移模块（避免污染 sys.modules）。"""
    path = MIGRATIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"fdl_migrations.{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(
    db_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    backfill: bool = False,
) -> dict:
    db = str(db_path or get_paths().primary_db_path)
    conn = get_connection(db)
    _ensure_migrations_table(conn)
    names = discover_migrations()

    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    existing = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}

    for name in names:
        if name in existing:
            skipped.append(name)
            continue
        if dry_run:
            # dry-run：仅登记"将执行"，不触碰库
            applied.append(name)
            continue
        if backfill:
            try:
                conn.execute(
                    f"INSERT INTO schema_migrations (name, applied_at) VALUES (?, {_TS_DEFAULT})",
                    (name,),
                )
                conn.commit()
                applied.append(name)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                errors.append(f"{name}: backfill 失败 {exc}")
            continue
        # 真实执行：跑 migrate() 后登记
        try:
            module = _load_module(name)
            result = module.migrate()
            # 迁移脚本可能内部吞异常并把错误放进返回 dict
            if isinstance(result, dict) and result.get("errors"):
                raise RuntimeError(f"migrate() 返回错误：{result['errors']}")
            conn.execute(
                f"INSERT INTO schema_migrations (name, applied_at) VALUES (?, {_TS_DEFAULT})",
                (name,),
            )
            conn.commit()
            applied.append(name)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            errors.append(f"{name}: {exc}")

    conn.close()
    return {"applied": applied, "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="FDL schema 迁移运行器（幂等）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的迁移名，不执行、不改库",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="把已手工跑过的迁移名登记为已应用（不重跑逻辑）",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        help="数据库路径（默认 fdl_core.paths 主库）",
    )
    args = parser.parse_args()
    result = run(db_path=args.db, dry_run=args.dry_run, backfill=args.backfill)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

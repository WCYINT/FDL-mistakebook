"""批次 4 · G14 双写一致性：Markdown 错题卡（人读真相源）↔ mistake_record（机读）。

`note_id` 双向索引：mistake_record.note_id = 卡片 frontmatter 的 id。
`verify_dual_write` 扫描两侧集合，报告缺卡/缺库的 note_id（不猜测，交人工）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fdl_core.mistakes.tables import ensure_mistake_table


def link_note(conn: sqlite3.Connection, *, mistake_id: int, note_id: str) -> None:
    """绑定 mistake_record ↔ Markdown 卡片（note_id）。"""
    ensure_mistake_table(conn)
    conn.execute(
        "UPDATE mistake_record SET note_id=?, updated_at="
        "strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        (note_id, mistake_id),
    )
    conn.commit()


def _card_ids(notes_dir: Path) -> set[str]:
    """扫描目录下全部错题卡的 frontmatter id。"""
    import yaml

    ids: set[str] = set()
    for p in sorted(Path(notes_dir).rglob("*.md")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("---"):
            continue
        try:
            fm = yaml.safe_load(text.split("---", 2)[1]) or {}
        except Exception:
            continue
        if fm.get("type") == "mistake" and fm.get("id") is not None:
            ids.add(str(fm["id"]))
    return ids


def verify_dual_write(conn: sqlite3.Connection, notes_dir: str | Path) -> dict:
    """双写一致性核验：返回 `{db_without_file, file_without_db}`（两侧差集）。"""
    ensure_mistake_table(conn)
    db_ids = {
        str(r[0])
        for r in conn.execute(
            "SELECT note_id FROM mistake_record WHERE note_id IS NOT NULL"
        ).fetchall()
    }
    file_ids = _card_ids(Path(notes_dir))
    return {
        "db_without_file": sorted(db_ids - file_ids),  # DB 有记录但缺卡片
        "file_without_db": sorted(file_ids - db_ids),  # 有卡片但未入库
    }

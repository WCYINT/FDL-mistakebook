"""知识书批次维护操作（Phase 2 · 2026-09-12）。

三个幂等批次操作（由 `scripts/kp_batch_rewrite.py` 调用）：

1. `normalize_domain_vocabulary` —— 领域术语课标化：DB 与卡片里的旧称
   （如"数与运算"）批量改写为课标名（"数与代数"），映射表唯一来源
   `kp_classifier.DOMAIN_ALIASES`。
2. `retag_olympiad_cards` —— 奥数卡学期桶：`grade_term` 统一为"小A"，
   卡片文件移入 `01-知识点/小A/`（判定复用 `db_sync.detect_olympiad`）。
3. `backfill_card_domains` —— 卡片域回填：卡片缺 `knowledge_domain` 时
   从 DB 回填（**仅填空，绝不覆盖**已有值）。

安全约定
--------
- 全部支持 `dry_run`（只报告不写）；
- 写文件/移文件前把受影响卡片备份到 `backup_dir`（平铺按文件名）；
- 幂等：重复执行不产生重复写入/移动；
- 只增改字段，不删除卡片内容；移动 = 写新路径 + 删旧路径（内容不变）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fdl_core.notes.db_sync import OLYMPIAD_TERM, detect_olympiad
from fdl_core.notes.kp_card import read_card, write_card
from fdl_core.notes.kp_classifier import CANONICAL_SUBJECTS, DOMAIN_ALIASES
from fdl_core.paths import get_paths
from fdl_core.srs.time_layer import fmt_ts, now_utc


def _default_kp_dir(subject_code: str) -> Path:
    return get_paths().subject_dir(subject_code) / "01-知识点"


def _iter_cards(kp_dir: Path) -> list[Path]:
    d = Path(kp_dir)
    return sorted(d.rglob("*.md")) if d.exists() else []


def _backup(card_path: Path, backup_dir: str | Path | None) -> str | None:
    """受影响卡片备份（平铺到 backup_dir；文件名即 code/中文名，唯一）。"""
    if not backup_dir:
        return None
    b = Path(backup_dir)
    b.mkdir(parents=True, exist_ok=True)
    dst = b / Path(card_path).name
    shutil.copy2(card_path, dst)
    return str(dst)


# ── 1. 领域术语课标化 ────────────────────────────────────────
def normalize_domain_vocabulary(
    conn,
    *,
    subjects=tuple(CANONICAL_SUBJECTS),
    kp_dirs: dict | None = None,
    dry_run: bool = False,
    backup_dir: str | Path | None = None,
) -> dict:
    """把 DB 与卡片中的领域旧称批量改写为课标名（DB + 卡片双侧）。

    只改"命中别名且与目标不同"的值；canonical 值原样保留。
    """
    out: dict = {
        "db_updated": 0,
        "db_items": [],
        "cards_scanned": 0,
        "cards_updated": [],
        "errors": [],
    }
    # —— DB 侧 ——
    hits = []
    for r in conn.execute(
        "SELECT id, code, knowledge_domain FROM knowledge_point"
        " WHERE knowledge_domain IS NOT NULL AND knowledge_domain != ''"
    ).fetchall():
        old = r[2]
        new = DOMAIN_ALIASES.get(old)
        if new and new != old:
            hits.append((r[0], r[1], old, new))
    if hits and not dry_run:
        stamp = fmt_ts(now_utc())
        for kp_id, _code, _old, new in hits:
            conn.execute(
                "UPDATE knowledge_point SET knowledge_domain=?, updated_at=? WHERE id=?",
                (new, stamp, kp_id),
            )
        conn.commit()
    out["db_updated"] = len(hits)
    out["db_items"] = [
        {"id": i, "code": c, "from": o, "to": n, "dry": dry_run} for i, c, o, n in hits
    ]

    # —— 卡片侧 ——
    for sub in subjects:
        kp_dir = (kp_dirs or {}).get(sub) or _default_kp_dir(sub)
        for p in _iter_cards(kp_dir):
            out["cards_scanned"] += 1
            try:
                card = read_card(p)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{p}: {type(exc).__name__}: {exc}")
                continue
            old = card.frontmatter.get("knowledge_domain")
            new = DOMAIN_ALIASES.get(old) if old else None
            if not new or new == old:
                continue
            if not dry_run:
                _backup(p, backup_dir)
                card.frontmatter["knowledge_domain"] = new
                write_card(p, card)
            out["cards_updated"].append({"path": str(p), "from": old, "to": new, "dry": dry_run})
    return out


# ── 2. 奥数卡学期桶 ──────────────────────────────────────────
def retag_olympiad_cards(
    conn,
    *,
    subject_code: str = "MATH",
    kp_dir: str | Path | None = None,
    term: str = OLYMPIAD_TERM,
    dry_run: bool = False,
    backup_dir: str | Path | None = None,
) -> dict:
    """奥数卡 `grade_term` 统一为 `term`（默认"小A"）并移入 `01-知识点/<term>/`。

    判定：`db_sync.detect_olympiad`（文本信号 + 候选血缘），逐 KP 扫描。
    幂等：已在目标桶且字段一致的卡片计 `already`，不重复写。
    """
    out: dict = {"detected": [], "moved": [], "already": [], "missing": [], "errors": []}
    sd = Path(kp_dir) if kp_dir else _default_kp_dir(subject_code)
    rows = conn.execute(
        "SELECT code, name, source_ref, description FROM knowledge_point"
        " WHERE subject_id=(SELECT id FROM subject WHERE code=?)",
        (subject_code,),
    ).fetchall()

    by_code: dict[str, tuple[Path, object]] = {}
    for p in _iter_cards(sd):
        try:
            c = read_card(p)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{p}: {type(exc).__name__}: {exc}")
            continue
        code = c.frontmatter.get("code")
        if code and code not in by_code:
            by_code[str(code)] = (p, c)

    target_dir = sd / term
    for code, name, source_ref, desc in rows:
        if not detect_olympiad(conn, code=code, source_ref=source_ref, description=desc, name=name):
            continue
        out["detected"].append(code)
        hit = by_code.get(str(code))
        if not hit:
            out["missing"].append(code)
            continue
        p, card = hit
        target = target_dir / p.name
        if card.frontmatter.get("grade_term") == term and p.parent.name == term:
            out["already"].append(code)
            continue
        if not dry_run:
            _backup(p, backup_dir)
            card.frontmatter["grade_term"] = term
            target_dir.mkdir(parents=True, exist_ok=True)
            write_card(target, card)
            if target != p:
                p.unlink()
        out["moved"].append({"code": code, "from": str(p), "to": str(target), "dry": dry_run})
    return out


# ── 3. 卡片域回填 ────────────────────────────────────────────
def backfill_card_domains(
    conn,
    *,
    subject_code: str = "MATH",
    kp_dir: str | Path | None = None,
    dry_run: bool = False,
    backup_dir: str | Path | None = None,
) -> dict:
    """卡片缺 `knowledge_domain` 时从 DB 回填（仅填空；DB 也空 → 跳过）。"""
    out: dict = {"filled": [], "skipped_no_db": [], "kept": 0, "errors": []}
    sd = Path(kp_dir) if kp_dir else _default_kp_dir(subject_code)
    for p in _iter_cards(sd):
        try:
            card = read_card(p)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{p}: {type(exc).__name__}: {exc}")
            continue
        if card.frontmatter.get("knowledge_domain"):
            out["kept"] += 1
            continue
        code = card.frontmatter.get("code")
        row = conn.execute(
            "SELECT knowledge_domain FROM knowledge_point WHERE code=?", (code,)
        ).fetchone()
        dom = row[0] if row else None
        if not dom:
            out["skipped_no_db"].append(code)
            continue
        if not dry_run:
            _backup(p, backup_dir)
            card.frontmatter["knowledge_domain"] = dom
            write_card(p, card)
        out["filled"].append({"code": code, "domain": dom, "path": str(p), "dry": dry_run})
    return out

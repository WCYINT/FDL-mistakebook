"""KB ↔ DB 双向同步器。

- **卡片 → DB**（`sync_kp_to_db`，2026-09-05 建）：Markdown 知识点卡 → knowledge_point 表。
- **DB → 卡片**（`export_kp_to_cards`，2026-09-12 Phase 1 建）：把 LLM 提炼等只存 DB
  的知识点补写成 md 卡片，打通双向、消除星图 Obsidian 链接 404。

🔴 2026-09-12 修复数据丢失 bug（Phase 1 接线前实测发现）：
原 `sync_kp_to_db` 用 `INSERT OR REPLACE`——SQLite 语义是 **DELETE + INSERT**，
未列出的列会被重置为默认值/NULL。实测导致卡片同步后 DB 侧的
`knowledge_domain` / `description` / `source_ref` / `tier_reason` 全部被清空。
改为「已存在 → UPDATE（COALESCE 保底）；不存在 → INSERT」，
使卡片字段为权威、DB-only 字段不被误伤（幂等往返安全）。

幂等：按 `code` 对齐（同 code 更新，不新增行）。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fdl_core.notes.kp_card import read_card
from fdl_core.notes.kp_tree import scan_kp_cards
from fdl_core.srs.time_layer import fmt_ts, now_utc

# ── 奥数卡学期桶（2026-09-12 Phase 2，King 拍板）────────────────
# 奥数内容横跨年级、不归属单一半期 → grade_term 统一为"小A"（独立学期桶），
# 目录落 `01-知识点/小A/`，与 G3A/G4A/G4B 并列。
OLYMPIAD_TERM = "小A"
_OLYMPIAD_KEYWORDS = ("奥数", "奥赛")
_LINEAGE_RE = re.compile(r"候选\s*#(\d+)")


def detect_olympiad(conn, *, code=None, source_ref=None, description=None, name=None) -> bool:
    """奥数卡判定（决定导出卡片的 grade_term 是否走"小A"桶）。

    双信号（任一命中即判奥数）：
    ① 文本信号：source_ref / description / name 含"奥数"或"奥赛"；
    ② 血缘信号：description 形如"由 LLM 候选 #N 晋级" → 回查
       `kp_candidate.sample_json` 是否含"奥数"（覆盖 source_ref 无关键词、
       但证据题源是奥数课的 KP，如候选 #14/#17/#20）。
    """
    text = " ".join(str(x or "") for x in (source_ref, description, name))
    if any(k in text for k in _OLYMPIAD_KEYWORDS):
        return True
    m = _LINEAGE_RE.search(str(description or ""))
    if not m or conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT candidate_code, candidate_name, rationale, sample_json"
            " FROM kp_candidate WHERE id=?",
            (int(m.group(1)),),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not row:
        return False
    return any(k in " ".join(str(x or "") for x in row) for k in _OLYMPIAD_KEYWORDS)


def sync_kp_to_db(conn: sqlite3.Connection, subject_dir: str | Path) -> dict:
    """扫描学科目录全部知识点卡 → 同步 knowledge_point 表。返回统计。

    - `kp_id` 由 `code` 幂等对齐（同 code 更新既有行，否则新插入）；
    - 🔴 已存在时走 UPDATE 而非 REPLACE：卡片提供**权威**字段值（name/tier/
      难度/权重等），**卡片未提供的字段（如 LLM 写入的 knowledge_domain）
      用 COALESCE 保留 DB 现值**，避免"同步一次抹掉一次"的数据丢失；
    - 同步后错题卡可按 `kp_code → kp_id` 挂载。
    """
    cards = scan_kp_cards(subject_dir)
    inserted, updated = 0, 0
    stamp = fmt_ts(now_utc())
    for card in cards:
        fm = card.frontmatter
        code = fm["code"]
        # parent_code → parent_id（卡片提供才解析，否则留给 COALESCE 保留 DB 值）
        parent_code = fm.get("parent_code")
        parent_id = None
        if parent_code:
            p = conn.execute(
                "SELECT id FROM knowledge_point WHERE code=?", (parent_code,)
            ).fetchone()
            parent_id = p[0] if p else None
        existing = conn.execute("SELECT id FROM knowledge_point WHERE code=?", (code,)).fetchone()

        if existing:
            kp_id = existing[0]
            updated += 1
            # 已存在：UPDATE。卡片有值的字段覆盖；卡片无值（None）的字段
            # 用 COALESCE 保留 DB 现值——防 DB-only 字段（domain/description等）被清空。
            conn.execute(
                "UPDATE knowledge_point SET"
                " subject_id = COALESCE((SELECT id FROM subject WHERE code=?), subject_id),"
                " name = ?,"
                " description = COALESCE(?, description),"
                " grade_level = ?,"
                " semester = COALESCE(?, semester),"
                " knowledge_domain = COALESCE(?, knowledge_domain),"
                " parent_id = COALESCE(?, parent_id),"
                " bloom_level = ?,"
                " abstraction_level = ?,"
                " importance_weight = ?,"
                " exam_frequency = COALESCE(?, exam_frequency),"
                " base_difficulty = ?,"
                " est_learn_minutes = ?,"
                " est_review_seconds = ?,"
                " kp_type = ?,"
                " source = COALESCE(?, source),"
                " source_ref = COALESCE(?, source_ref),"
                " tier = ?,"
                " tier_reason = COALESCE(?, tier_reason),"
                " graph_version = ?,"
                " valid_from = ?,"
                " updated_at = ?"
                " WHERE id = ?",
                (
                    fm.get("subject", "MATH"),
                    fm["name"],
                    fm.get("description"),
                    fm.get("grade_level", 4),
                    fm.get("semester"),
                    fm.get("knowledge_domain"),
                    parent_id,
                    fm.get("bloom_level", 2),
                    fm.get("abstraction_level", 2),
                    fm.get("importance_weight", 1.0),
                    fm.get("exam_frequency"),
                    fm.get("base_difficulty", 5.0),
                    fm.get("est_learn_minutes", 1.5),
                    fm.get("est_review_seconds", 45),
                    fm.get("kp_type", "SKILL"),
                    fm.get("source"),
                    fm.get("source_ref"),
                    fm.get("tier", "L0"),
                    fm.get("tier_reason"),
                    fm.get("graph_version", "2026.1"),
                    fm.get("valid_from", "2026-09-01"),
                    stamp,
                    kp_id,
                ),
            )
        else:
            inserted += 1
            conn.execute(
                "INSERT INTO knowledge_point (subject_id, code, name, description,"
                " grade_level, semester, knowledge_domain, parent_id, bloom_level,"
                " abstraction_level, importance_weight, exam_frequency,"
                " base_difficulty, est_learn_minutes, est_review_seconds, kp_type,"
                " source, source_ref, tier, tier_reason, graph_version, valid_from,"
                " created_at, updated_at)"
                " VALUES ((SELECT id FROM subject WHERE code=?), ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fm.get("subject", "MATH"),
                    code,
                    fm["name"],
                    fm.get("description"),
                    fm.get("grade_level", 4),
                    fm.get("semester"),
                    fm.get("knowledge_domain"),
                    parent_id,
                    fm.get("bloom_level", 2),
                    fm.get("abstraction_level", 2),
                    fm.get("importance_weight", 1.0),
                    fm.get("exam_frequency"),
                    fm.get("base_difficulty", 5.0),
                    fm.get("est_learn_minutes", 1.5),
                    fm.get("est_review_seconds", 45),
                    fm.get("kp_type", "SKILL"),
                    fm.get("source"),
                    fm.get("source_ref"),
                    fm.get("tier", "L0"),
                    fm.get("tier_reason"),
                    fm.get("graph_version", "2026.1"),
                    fm.get("valid_from", "2026-09-01"),
                    stamp,
                    stamp,
                ),
            )
    conn.commit()
    return {"cards": len(cards), "inserted": inserted, "updated": updated}


def export_kp_to_cards(
    conn: sqlite3.Connection,
    subject_code: str = "MATH",
    *,
    subject_dir: str | Path | None = None,
    only_missing: bool = True,
    dry_run: bool = False,
    graph_version_default: str = "2026.1",
) -> dict:
    """DB → Markdown 卡片**反向导出**（2026-09-12 Phase 1：补齐缺卡）。

    为什么需要反向导出
    ------------------
    `sync_kp_to_db` 是单向 card → DB。但 LLM 提炼的新知识点直接进 DB
    （无卡片），导致 DB 有 47 条而卡片只有 27 张 —— 星图的 Obsidian 链接
    会对 20 条 404。此函数把 DB 侧的知识点补写成 md 卡片，打通双向。

    **筛选标准（only_missing 的真实含义）**
    ------------------------------------
    默认 `only_missing=True`：**只导出缺卡的 KP，绝不覆盖已有卡片**。
    理由：卡片是"人读真相源"，人工可能在正文补充了概念解释/易错点；
    DB 字段只是机读快照，覆盖会丢人工成果。需要全量重建时才显式传 False。

    **补齐来源（导出的字段取值）**
    ---------------------------
    - 必填项（name/grade_level/kp_type/bloom_level/importance_weight/
      base_difficulty/est_learn_minutes/est_review_seconds/tier/
      graph_version/valid_from）：直接取 DB 行；
    - `subject`：由 subject_id 反查 subject.code；
    - `grade_term`：由 grade_level + semester 推导（`G4A` = 四年级上学期）；
      semester 为 NULL 时按 source_ref 关键词嗅探（"上册/三上"→1，"下册"→2），
      仍不确定则默认 1（上学期）——**这是有意的保守默认，非精确断言**；
      **奥数卡例外**：`detect_olympiad()` 命中 → 直接落 `小A` 桶（不猜学期）；
    - `source_ref`/`tier_reason`：取 DB 值（LLM 提炼理由）；
    - `tags`：LLM 提炼的补 `["LLM-EXTRACTED"]`，便于与教材卡区分；
    - 正文：写骨架（是什么/怎么用/易错点 + 提炼依据），供人工/后续 LLM 填充。

    写入走 `write_card()` → 强制 kp_card.schema.json 校验（不合格即抛错，
    不会写入脏卡）。返回统计 {cards_total, exported, skipped, errors}。
    """
    from fdl_core.notes.kp_card import KpCard, write_card
    from fdl_core.paths import get_paths

    sd = Path(subject_dir) if subject_dir else (get_paths().subject_dir(subject_code) / "01-知识点")
    srow = conn.execute(
        "SELECT id, code, name FROM subject WHERE code=? AND deleted_at IS NULL",
        (subject_code,),
    ).fetchone()
    if not srow:
        return {"ok": False, "error": f"学科不存在：{subject_code}"}
    subject_id, subj_code, subj_name = srow

    existing = _existing_card_codes(sd)
    rows = conn.execute(
        "SELECT id, code, name, description, grade_level, semester,"
        " knowledge_domain, parent_id, bloom_level, abstraction_level,"
        " importance_weight, exam_frequency, base_difficulty,"
        " est_learn_minutes, est_review_seconds, kp_type, source, source_ref,"
        " tier, tier_reason, graph_version, valid_from"
        " FROM knowledge_point WHERE subject_id=? ORDER BY code",
        (subject_id,),
    ).fetchall()
    cols = [
        "id",
        "code",
        "name",
        "description",
        "grade_level",
        "semester",
        "knowledge_domain",
        "parent_id",
        "bloom_level",
        "abstraction_level",
        "importance_weight",
        "exam_frequency",
        "base_difficulty",
        "est_learn_minutes",
        "est_review_seconds",
        "kp_type",
        "source",
        "source_ref",
        "tier",
        "tier_reason",
        "graph_version",
        "valid_from",
    ]

    exported, skipped, errors = [], 0, []
    for r in rows:
        kp = dict(zip(cols, r, strict=False))
        if only_missing and kp["code"] in existing:
            skipped += 1
            continue
        # parent_code（DB 存 parent_id → 反查 code）
        parent_code = None
        if kp.get("parent_id"):
            p = conn.execute(
                "SELECT code FROM knowledge_point WHERE id=?", (kp["parent_id"],)
            ).fetchone()
            parent_code = p[0] if p else None

        if detect_olympiad(
            conn,
            code=kp["code"],
            source_ref=kp.get("source_ref"),
            description=kp.get("description"),
            name=kp.get("name"),
        ):
            grade_term = OLYMPIAD_TERM  # 奥数卡统一学期桶（Phase 2）
        else:
            grade_term = _derive_grade_term(
                kp["grade_level"], kp.get("semester"), kp.get("source_ref") or ""
            )
        fm = {
            "type": "knowledge_point",
            "code": kp["code"],
            "subject": subj_code,
            "name": kp["name"],
            "grade_level": kp["grade_level"],
            "semester": kp.get("semester"),
            "grade_term": grade_term,
            "knowledge_domain": kp.get("knowledge_domain"),
            "parent_code": parent_code,
            "kp_type": kp.get("kp_type") or "SKILL",
            "bloom_level": kp.get("bloom_level") or 2,
            "abstraction_level": kp.get("abstraction_level") or 2,
            "importance_weight": kp.get("importance_weight") or 1.0,
            "exam_frequency": kp.get("exam_frequency"),
            "base_difficulty": kp.get("base_difficulty") or 5.0,
            "est_learn_minutes": kp.get("est_learn_minutes") or 1.5,
            "est_review_seconds": kp.get("est_review_seconds") or 45,
            "tier": kp.get("tier") or "L1",
            "tier_reason": kp.get("tier_reason"),
            "source": kp.get("source") or "CUSTOM",
            "source_ref": kp.get("source_ref"),
            "graph_version": kp.get("graph_version") or graph_version_default,
            "valid_from": kp.get("valid_from") or "2026-09-01",
            "tags": (["LLM-EXTRACTED"] if (kp.get("source") == "CUSTOM") else []),
        }
        body = _export_body(kp)
        # 文件命名统一走命名规范（编码-描述.md，见 fdl_core/notes/naming.py）
        from fdl_core.notes.naming import card_filename

        target = Path(sd) / grade_term / card_filename(kp["code"], kp.get("name") or "")
        if dry_run:
            exported.append({"code": kp["code"], "path": str(target), "dry": True})
            continue
        try:
            write_card(target, KpCard(frontmatter=fm, body=body))
            exported.append({"code": kp["code"], "path": str(target)})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kp['code']}: {type(exc).__name__}: {exc}")
    return {
        "ok": not errors,
        "subject_dir": str(sd),
        "cards_total": len(rows),
        "exported": len(exported),
        "skipped": skipped,
        "errors": errors,
        "items": exported,
    }


def _existing_card_codes(subject_dir: Path) -> set[str]:
    """扫描目录下已有卡片的 code 集合（用于只补缺、不覆盖）。"""
    codes: set[str] = set()
    for p in Path(subject_dir).rglob("*.md"):
        try:
            card = read_card(p)
        except Exception:  # noqa: BLE001
            continue
        c = card.frontmatter.get("code")
        if c:
            codes.add(str(c))
    return codes


def _derive_grade_term(grade_level: int, semester, source_ref: str) -> str:
    """推导 grade_term（如 G4A）。semester 缺失时按 source_ref 关键词嗅探。

    默认 1（上学期）——保守默认：宁可归到上学期，也不猜错下学期。
    """
    sem = semester
    if not sem:
        t = str(source_ref or "")
        if any(k in t for k in ("下册", "三下", "四下", "第二学期")):
            sem = 2
        elif any(k in t for k in ("上册", "三上", "四上", "第一学期")):
            sem = 1
        else:
            sem = 1
    return f"G{int(grade_level)}{'A' if int(sem) == 1 else 'B'}"


def _export_body(kp: dict) -> str:
    """导出卡片的正文骨架（供人工/LLM 后续填充；含提炼依据便于溯源）。"""
    lines = [
        f"# {kp['name']}",
        "",
        "## 是什么",
        f"（待补充：{kp['name']} 的概念说明）",
        "",
        "## 怎么用",
        "（待补充：解题步骤 / 使用场景）",
        "",
        "## 易错点",
        "（待补充：常见错误与纠正）",
    ]
    if kp.get("description"):
        lines += ["", "## 提炼依据", str(kp["description"])]
    elif kp.get("source_ref"):
        lines += ["", "## 提炼依据", str(kp["source_ref"])]
    return "\n".join(lines) + "\n"


def remount_mistakes(conn: sqlite3.Connection, notes_root: str | Path | None = None) -> int:
    """把 kp_id=0 占位的错题按 kp_code 挂载到真实知识点（返回挂载数）。"""
    # mistake_record 无 kp_code 列——按 note_id 找到卡片文件读 kp_code
    n = 0
    rows = conn.execute("SELECT id, note_id FROM mistake_record WHERE kp_id = 0").fetchall()
    root = notes_root or _default_notes_root()
    for mid, note_id in rows:
        card = _find_card_by_note_id(note_id, root)
        if card is None:
            continue
        kp_code = card.frontmatter.get("kp_code", "")
        if kp_code.startswith("PENDING"):
            continue  # 占位码无法挂载
        row = conn.execute("SELECT id FROM knowledge_point WHERE code=?", (kp_code,)).fetchone()
        if row:
            conn.execute("UPDATE mistake_record SET kp_id=? WHERE id=?", (row[0], mid))
            n += 1
    conn.commit()
    return n


def _default_notes_root() -> Path:
    """错题快照根目录（走 paths，不硬编码）。"""
    from fdl_core.paths import get_paths

    return get_paths().subject_dir("MATH") / "03-错题快照"


def _find_card_by_note_id(note_id: str, notes_root: str | Path):
    """按 note_id 在错题快照目录找卡片。"""
    for p in Path(notes_root).rglob("M-*.md"):
        try:
            card = read_card(p)
        except Exception:
            continue
        if str(card.frontmatter.get("id")) == str(note_id):
            return card
    return None

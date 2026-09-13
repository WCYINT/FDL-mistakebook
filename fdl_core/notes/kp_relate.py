"""知识点关联边构建（Phase 2 · 连边构图，2026-09-12）。

把知识点连成一张"学习星图"的边数据：
    LLM 提议 → 三重校验 → 双跑一致闸门 → 写入 kp_relation

职责边界（与 kp_prerequisite 严格分开）
----------------------------------------
    kp_relation      : 学习关联（同法/类比/应用/对比）——星图渲染用
    kp_prerequisite  : 学习先决（门禁）——kp_gate 用它拦"未达先决不得学"
两表语义不同，绝不混写。

关系类型白名单（只此四类）
--------------------------
    SHARED_METHOD  同法：共用同一方法/思想
    ANALOGY        类比：结构相似、方法可迁移
    APPLICATION    应用：一个是另一个的典型运用/深化
    CONTRAST       对比：易混、需对比辨析才能正确区分使用

三重校验（防幻觉）
------------------
1. 两端 code 必须存在于 knowledge_point（LLM 自造 code 直接丢弃）；
2. type 必须在白名单内（大小写归一后）；
3. 作用域一致：within 边两端同科；cross 边两端异科。

闸门（沿用项目纪律"两次独立分析结论一致才自动"）
-------------------------------------------------
    双跑命中 + min(conf) ≥ 0.80  → source='LLM_AUTO'（自动生效）
    单跑命中 或 0.60 ≤ conf < 0.80 → source='LLM_SUGGEST'（进人工复核队列）
    conf < 0.60                  → 丢弃（只计数）
    （env FDL_ATTR_AUTO_ACCEPT 调自动阈值；FDL_REL_SUGGEST_FLOOR 调建议下限）

无向边规范化
------------
写库前 from_kp_id < to_kp_id（与建表 CHECK 一致）；**同一对节点只保留一条边**
（跨跑幂等：库里已有任一同对边 → 跳过；同跑内类型冲突 → AUTO > SUGGEST、
再按置信度裁决）。

复核流
------
`list_relations(source='LLM_SUGGEST')` 出清单 → `confirm_relations` 标
HUMAN_VERIFIED / `drop_relations` 删除。
"""

from __future__ import annotations

import logging
import os
import sqlite3

from fdl_core.l2.fallback import run_chain
from fdl_core.l2.minimax import MiniMaxClient
from fdl_core.mistakes.attribution_engine import (
    AUTO_ACCEPT_THRESHOLD,
    AUTO_CONFIRM_RUNS,
    _clamp_conf,
    get_default_client,
    parse_llm_json,
)
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.notes.kp_relate")

PROMPT_VERSION = "kp-relate-v1"

# ── 关系类型白名单 ───────────────────────────────────────────
RELATION_TYPES: dict[str, str] = {
    "SHARED_METHOD": "同法（共用同一方法/思想）",
    "ANALOGY": "类比（结构相似、方法可迁移）",
    "APPLICATION": "应用（一个知识点在另一个中的典型运用）",
    "CONTRAST": "对比（易混、需对比辨析）",
}

# 建议下限：低于此值直接丢弃（不占复核队列）
SUGGEST_FLOOR: float = float(os.environ.get("FDL_REL_SUGGEST_FLOOR", "0.60"))

# source 枚举
SRC_AUTO = "LLM_AUTO"
SRC_SUGGEST = "LLM_SUGGEST"
SRC_VERIFIED = "HUMAN_VERIFIED"


# ── 目录加载 ────────────────────────────────────────────────
def load_kp_catalog(conn, subject_codes: list[str] | None = None) -> list[dict]:
    """知识点清单（供 prompt 与 code→id 映射）。按 subject 排序、code 排序。"""
    sql = (
        "SELECT k.id, k.code, k.name, COALESCE(k.knowledge_domain,'') AS domain,"
        " s.code AS subject, s.sort_order"
        " FROM knowledge_point k JOIN subject s ON s.id = k.subject_id"
    )
    params: list = []
    if subject_codes:
        q = ",".join("?" * len(subject_codes))
        sql += f" WHERE s.code IN ({q})"
        params += list(subject_codes)
    sql += " ORDER BY s.sort_order, k.code"
    cols = ("id", "code", "name", "domain", "subject", "sort_order")
    return [dict(zip(cols, r, strict=False)) for r in conn.execute(sql, params)]


# ── Prompt 构建 ─────────────────────────────────────────────
_REL_TYPES_BLOCK = "\n".join(f"- {k}：{v}" for k, v in RELATION_TYPES.items())
_JSON_SHAPE = (
    "只输出一个 JSON 对象（不要 markdown 代码块）：\n"
    '{"relations":[{"from":"<code>","to":"<code>","type":"<类型>",'
    '"confidence":0.9,"reason":"<一句话依据>"}]}'
)


def _build_prompts(items: list[dict], *, scope: str) -> tuple[str, str]:
    """scope=within|cross → (system, user)。"""
    if scope == "cross":
        lines: list[str] = []
        cur = None
        for it in items:
            if it["subject"] != cur:
                cur = it["subject"]
                lines.append(f"【{cur}】")
            lines.append(f"- {it['code']} | {it['name']} | {it['domain'] or '未分类'}")
        system = (
            "你是小学跨学科知识图谱专家。给定两个学科的知识点清单，"
            "找出**跨学科**的关联。\n\n"
            "关系类型（只能选其一）：\n" + _REL_TYPES_BLOCK + "\n\n"
            "规则：\n"
            "1. 跨学科关联必须**克制**：只有共享同一思维方法或真实应用场景才连；\n"
            "2. 没有合适的关联就返回空数组（宁缺毋滥）；\n"
            "3. 每条边两端必须**分属不同学科**；\n"
            "4. 同一对知识点最多给一条边（选最贴切的类型）；\n"
            "5. from/to 必须原样使用清单中的 code，禁止自造；\n"
            "6. confidence ∈ [0,1]，反映“该关联是否经得起推敲”。\n" + _JSON_SHAPE
        )
        user = (
            "【知识点清单】格式：- code | 名称 | 细分领域\n"
            + "\n".join(lines)
            + "\n\n请找出上列知识点之间的**跨学科**关联。"
        )
    else:
        lines = [f"- {it['code']} | {it['name']} | {it['domain'] or '未分类'}" for it in items]
        system = (
            "你是小学知识图谱构建专家。给定同一个学科的一批知识点，"
            "找出之间**有教学价值**的关联，并判定关系类型。\n\n"
            "关系类型（只能选其一）：\n" + _REL_TYPES_BLOCK + "\n\n"
            "规则：\n"
            "1. 只连**强关联**：掌握该关系能同时理解两者、或能避免一类常见错误；"
            "牵强的不连；\n"
            "2. 宁缺毋滥——不必为每个知识点连线，没有把握就不连；\n"
            "3. 同一对知识点最多给一条边（选最贴切的类型）；\n"
            "4. from/to 必须原样使用清单中的 code，禁止自造；\n"
            "5. confidence ∈ [0,1]，反映“该关联是否经得起推敲”。\n" + _JSON_SHAPE
        )
        user = (
            "【知识点清单】格式：- code | 名称 | 细分领域\n"
            + "\n".join(lines)
            + "\n\n请找出上述知识点之间的关联。"
        )
    return system, user


def _single_run(items: list[dict], *, scope: str, client) -> dict:
    """一次 LLM 提议 + 解析。返回 {source_layer, edges:[raw dict]}。"""
    system, user = _build_prompts(items, scope=scope)
    chain = run_chain(system, user, client=client, version_stamp=PROMPT_VERSION)
    parsed = parse_llm_json(chain.answer)
    edges: list[dict] = []
    if isinstance(parsed, dict) and isinstance(parsed.get("relations"), list):
        edges = [e for e in parsed["relations"] if isinstance(e, dict)]
    return {"source_layer": chain.source, "edges": edges}


# ── 校验 ────────────────────────────────────────────────────
def _validate(
    raw: dict, *, item_by_code: dict, scope: str, scope_subjects: list[str]
) -> tuple[dict | None, str | None]:
    """规范化 + 三重校验。返回 (边, None) 或 (None, 丢弃原因)。

    - `hallucinated_code`：code 不在**全库**知识点中（真幻觉）；
    - `scope_mismatch`  ：code 真实存在但越出了本次作用域
      （within 收到跨科边 / cross 收到同科或域外边）。
    """
    f = str(raw.get("from") or "").strip()
    t = str(raw.get("to") or "").strip()
    typ = str(raw.get("type") or "").strip().upper()
    if f not in item_by_code or t not in item_by_code:
        return None, "hallucinated_code"
    if f == t:
        return None, "self_edge"
    if typ not in RELATION_TYPES:
        return None, "bad_type"
    a, b = item_by_code[f], item_by_code[t]
    if scope == "within":
        if a["subject"] != b["subject"] or a["subject"] != scope_subjects[0]:
            return None, "scope_mismatch"
    else:
        if a["subject"] == b["subject"]:
            return None, "scope_mismatch"
        if a["subject"] not in scope_subjects or b["subject"] not in scope_subjects:
            return None, "scope_mismatch"
    key = (f, t) if f < t else (t, f)
    return {
        "from": key[0],
        "to": key[1],
        "type": typ,
        "confidence": _clamp_conf(raw.get("confidence")),
        "reason": str(raw.get("reason") or "").strip()[:200],
    }, None


# ── 双跑合并 + 闸门 ─────────────────────────────────────────
def _merge_and_gate(
    runs: list[dict], *, item_by_code: dict, scope: str, scope_subjects: list[str], out: dict
) -> list[dict]:
    """把多跑原始提议 → 校验 → 合并 → 定档。返回 [候选边]（未做 pair 去重）。

    丢弃计数按**唯一项**（reason + 边三元组）去重——双跑命中的同一坏提议只计一次。
    """
    merged: dict = {}
    dropped_keys: dict[str, set] = {}
    for idx, run in enumerate(runs):
        for raw in run["edges"]:
            out["raw_edges"] += 1
            e, why = _validate(
                raw,
                item_by_code=item_by_code,
                scope=scope,
                scope_subjects=scope_subjects,
            )
            if e is None:
                dropped_keys.setdefault(why, set()).add(
                    (
                        str(raw.get("from") or "").strip(),
                        str(raw.get("to") or "").strip(),
                        str(raw.get("type") or "").strip().upper(),
                    )
                )
                continue
            key = (e["from"], e["to"], e["type"])
            rec = merged.get(key)
            if rec is None:
                merged[key] = {"edge": e, "runs": {idx}, "confs": [e["confidence"]]}
            else:
                rec["runs"].add(idx)
                rec["confs"].append(e["confidence"])
    for why, keys in dropped_keys.items():
        out["dropped"][why] = out["dropped"].get(why, 0) + len(keys)

    out["unique_edges"] = len(merged)
    ranked: list[dict] = []
    for rec in merged.values():
        e = rec["edge"]
        n = len(rec["runs"])
        conf = min(rec["confs"]) if n >= 2 else rec["confs"][0]
        if n >= 2 and conf >= AUTO_ACCEPT_THRESHOLD:
            tier = "auto"
        elif conf >= SUGGEST_FLOOR:
            tier = "suggest"
        else:
            out["dropped"]["low_confidence"] = out["dropped"].get("low_confidence", 0) + 1
            continue
        mark = "[双跑一致]" if n >= 2 else "[单跑命中]"
        ranked.append(
            {
                "tier": tier,
                "edge": e,
                "conf": conf,
                "n_runs": n,
                "note": (f"{mark} {e['reason']}" if e["reason"] else mark),
            }
        )

    # 同一对节点只留一条边：AUTO 优先，再按置信度
    best: dict = {}
    for rec in sorted(ranked, key=lambda r: (r["tier"] == "auto", r["conf"]), reverse=True):
        pair = (rec["edge"]["from"], rec["edge"]["to"])
        if pair in best:
            out["dropped"]["pair_conflict"] = out["dropped"].get("pair_conflict", 0) + 1
            continue
        best[pair] = rec
    return list(best.values())


# ── 主入口：提议 → 写入 ─────────────────────────────────────
def propose_relations(
    conn,
    *,
    scope: str,
    subject_codes: list[str],
    client: MiniMaxClient | None = None,
    confirm_runs: int | None = None,
    dry_run: bool = False,
) -> dict:
    """对给定学科（within 单科 / cross 两科）跑 LLM 提议并落库。

    返回 {ok, scope, subjects, items, raw_edges, unique_edges, auto[], suggest[],
          dropped{}, existing, inserted, source_layers[], errors[]}
    """
    if scope not in ("within", "cross"):
        return {"ok": False, "error": f"未知 scope：{scope}"}
    out: dict = {
        "ok": False,
        "scope": scope,
        "subjects": list(subject_codes),
        "items": 0,
        "raw_edges": 0,
        "unique_edges": 0,
        "auto": [],
        "suggest": [],
        "dropped": {},
        "existing": 0,
        "inserted": 0,
        "source_layers": [],
        "errors": [],
    }
    items = load_kp_catalog(conn, subject_codes)
    out["items"] = len(items)
    if len(items) < 2:
        out["ok"] = True
        out["errors"].append("知识点不足 2 条，跳过")
        return out

    client = client or get_default_client()
    n_runs = AUTO_CONFIRM_RUNS if confirm_runs is None else max(1, int(confirm_runs))
    runs: list[dict] = []
    for _ in range(n_runs):
        r = _single_run(items, scope=scope, client=client)
        out["source_layers"].append(r["source_layer"])
        if r["source_layer"] == "L0":
            out["errors"].append("LLM 不可达（降级 L0），本次不写任何边")
            return out
        runs.append(r)

    item_by_code = {it["code"]: it for it in load_kp_catalog(conn)}  # 全库（判幻觉 vs 越界）
    candidates = _merge_and_gate(
        runs,
        item_by_code=item_by_code,
        scope=scope,
        scope_subjects=list(subject_codes),
        out=out,
    )

    # 已有边（任一同对）→ 跨跑幂等：不重复、不覆盖
    existing_pairs = {
        tuple(sorted((r[0], r[1])))
        for r in conn.execute("SELECT from_kp_id, to_kp_id FROM kp_relation")
    }
    id_by_code = {it["code"]: it["id"] for it in items}
    stamp = fmt_ts(now_utc())
    for rec in candidates:
        e = rec["edge"]
        fi, ti = id_by_code[e["from"]], id_by_code[e["to"]]
        if fi > ti:
            fi, ti = ti, fi
        source = SRC_AUTO if rec["tier"] == "auto" else SRC_SUGGEST
        item = {
            "from": e["from"],
            "to": e["to"],
            "type": e["type"],
            "confidence": rec["conf"],
            "n_runs": rec["n_runs"],
            "note": rec["note"],
        }
        if (fi, ti) in existing_pairs:
            out["existing"] += 1
            item["skipped"] = "existing"
        elif dry_run:
            item["dry"] = True
        else:
            try:
                conn.execute(
                    "INSERT INTO kp_relation"
                    " (from_kp_id, to_kp_id, relation_type, note, confidence,"
                    "  source, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(from_kp_id, to_kp_id, relation_type) DO NOTHING",
                    (fi, ti, e["type"], rec["note"], rec["conf"], source, stamp, stamp),
                )
                out["inserted"] += 1
                existing_pairs.add((fi, ti))
            except sqlite3.IntegrityError as exc:  # FK/CHECK 异常不吞
                out["errors"].append(f"{e['from']}~{e['to']}: {exc}")
                continue
        (out["auto"] if rec["tier"] == "auto" else out["suggest"]).append(item)
    if not dry_run:
        conn.commit()
    out["ok"] = not out["errors"]
    return out


def propose_all(
    conn,
    *,
    client: MiniMaxClient | None = None,
    confirm_runs: int | None = None,
    dry_run: bool = False,
    only_subjects: list[str] | None = None,
) -> list[dict]:
    """全网构建：各科内部边（within）+ 各科对跨科边（cross）。"""
    rows = conn.execute(
        "SELECT s.code, COUNT(*) c FROM knowledge_point k"
        " JOIN subject s ON s.id = k.subject_id"
        " GROUP BY s.code HAVING c >= 2 ORDER BY s.sort_order"
    ).fetchall()
    all_codes = [r[0] for r in rows]
    reports: list[dict] = []
    for c in all_codes:
        if only_subjects and c not in only_subjects:
            continue
        reports.append(
            propose_relations(
                conn,
                scope="within",
                subject_codes=[c],
                client=client,
                confirm_runs=confirm_runs,
                dry_run=dry_run,
            )
        )
    for i in range(len(all_codes)):
        for j in range(i + 1, len(all_codes)):
            reports.append(
                propose_relations(
                    conn,
                    scope="cross",
                    subject_codes=[all_codes[i], all_codes[j]],
                    client=client,
                    confirm_runs=confirm_runs,
                    dry_run=dry_run,
                )
            )
    return reports


# ── 查询 / 复核 ─────────────────────────────────────────────
_SELECT_REL = (
    "SELECT r.id, r.relation_type, r.confidence, r.source, r.note,"
    " kf.code AS from_code, kf.name AS from_name,"
    " kt.code AS to_code, kt.name AS to_name,"
    " sf.code AS from_subject, st.code AS to_subject"
    " FROM kp_relation r"
    " JOIN knowledge_point kf ON kf.id = r.from_kp_id"
    " JOIN knowledge_point kt ON kt.id = r.to_kp_id"
    " JOIN subject sf ON sf.id = kf.subject_id"
    " JOIN subject st ON st.id = kt.subject_id"
)
_REL_COLS = (
    "id",
    "relation_type",
    "confidence",
    "source",
    "note",
    "from_code",
    "from_name",
    "to_code",
    "to_name",
    "from_subject",
    "to_subject",
)


def list_relations(conn, *, source: str | None = None, limit: int = 500) -> list[dict]:
    sql = _SELECT_REL
    params: list = []
    if source:
        sql += " WHERE r.source = ?"
        params.append(source)
    sql += " ORDER BY r.confidence DESC, r.id LIMIT ?"
    params.append(limit)
    return [dict(zip(_REL_COLS, r, strict=False)) for r in conn.execute(sql, params)]


def edge_stats(conn) -> dict:
    """边统计：总数 / 按来源 / 按类型 / 跨科边数（含明细）。"""
    total = conn.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0]
    by_source = dict(
        conn.execute("SELECT source, COUNT(*) FROM kp_relation GROUP BY source").fetchall()
    )
    by_type = dict(
        conn.execute(
            "SELECT relation_type, COUNT(*) FROM kp_relation GROUP BY relation_type"
        ).fetchall()
    )
    cross = [d for d in list_relations(conn) if d["from_subject"] != d["to_subject"]]
    return {
        "total": total,
        "by_source": by_source,
        "by_type": by_type,
        "cross": len(cross),
        "cross_items": cross,
    }


def confirm_relations(conn, ids: list[int]) -> dict:
    """LLM_SUGGEST → HUMAN_VERIFIED（人工确认）。"""
    n = 0
    for i in ids:
        cur = conn.execute(
            "UPDATE kp_relation SET source=?, updated_at=? WHERE id=? AND source=?",
            (SRC_VERIFIED, fmt_ts(now_utc()), i, SRC_SUGGEST),
        )
        n += cur.rowcount
    conn.commit()
    return {"confirmed": n}


def drop_relations(conn, ids: list[int], *, dry_run: bool = False) -> dict:
    """删除边（人工驳回）。返回受影响明细。"""
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        _SELECT_REL.replace("FROM kp_relation r", "FROM kp_relation r") + f" WHERE r.id IN ({q})",
        ids,
    ).fetchall()
    items = [dict(zip(_REL_COLS, r, strict=False)) for r in rows]
    if not dry_run and items:
        conn.execute(f"DELETE FROM kp_relation WHERE id IN ({q})", ids)
        conn.commit()
    return {"dropped": len(items), "items": items, "dry": dry_run}

"""LLM 知识点匹配引擎 —— 读题面从知识点体系中提候选，双跑一致闸门后挂载。

背景（King 2026-09-12 拍板）
---------------------------
错题挂载知识点的链路此前两处断裂（confirm 端点硬编码 kp_id=0、前端无选择控件），
挂载率长期 0%（0 / 55）。King 决定：**知识点匹配改为由 LLM 自动提炼**，
不再依赖人工圈选。

本模块 1:1 平移归因引擎（attribution_engine.py）的架构：

    attribution_engine              kp_matcher（本模块）
    ─────────────────────           ─────────────────────────
    attribution_proposal     ←→     kp_match_proposal
    taxonomy_candidate       ←→     kp_candidate
    analyze_feedback()       ←→     match_kp_for_mistake()
    双跑一致闸门              ←→     同款（AUTO_CONFIRM_RUNS）
    LLM_ASSISTED 审计标签      ←→     同款

闸门（继承归因引擎纪律）
------------------------
1. 双跑一致：两次独立分析同 code 且各次置信 >= 阈值（默认 0.80）才自动写入
   `mistake_record.kp_id`。
2. 任一不一致 / 低置信 / code 不在 knowledge_point 中 → 落 PROPOSED 待人工。
3. LLM 提议新知识点 → 写 `kp_candidate`（**绝不自动新增**，人工确认后晋级）。
4. LLM 不可达（L0 降级）→ 落 PROPOSED、不猜、不写权威字段。

写入范围（三处，保持数据一致）
------------------------------
1. `mistake_record.kp_id` —— 权威挂载边
2. `review_schedule.kp_id` —— 回填该错题所有 kp_id 为空/0 的计划（PENDING 与历史），
   避免"错题已挂载但计划还悬空"的半挂载态（Phase 2 kp_state 生产者依赖此）。
3. `kp_match_proposal` —— 审计链（无论自动/人工）

离线安全：任何异常都不抛出（复习/录入主流程不能被匹配打断）。
"""

from __future__ import annotations

import json
import logging
import threading
import time as _time
from pathlib import Path

from fdl_core.db.schema import get_connection
from fdl_core.l2.fallback import run_chain
from fdl_core.l2.minimax import MiniMaxClient
from fdl_core.mistakes.attribution_engine import (  # 同包内复用（双跑闸门工具）
    AUTO_ACCEPT_THRESHOLD,
    AUTO_CONFIRM_RUNS,
    CONFIRM_RETRY_MAX,
    _clamp_conf,
    get_default_client,
    is_technical_failure,
    parse_llm_json,
)
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.mistakes.kp_matcher")

logger = logging.getLogger("fdl.mistakes.kp_matcher")

PROMPT_VERSION = "kp-v1"

# 提案状态机（与 attribution_proposal 同款取值）
STATUS_PROPOSED = "PROPOSED"
STATUS_AUTO = "AUTO_ACCEPTED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_SUPERSEDED = "SUPERSEDED"


# ── 数据读取 ────────────────────────────────────────────────
def _load_mistake(conn, mistake_id: int) -> dict | None:
    """读归因所需错题字段。"""
    row = conn.execute(
        "SELECT id, subject, source_ref, wrong_answer, correct_answer,"
        " diagnosis_type, error_type, reappear_count, is_tamed, kp_id"
        " FROM mistake_record WHERE id=?",
        (mistake_id,),
    ).fetchone()
    if not row:
        return None
    return dict(
        zip(
            [
                "id",
                "subject",
                "source_ref",
                "wrong_answer",
                "correct_answer",
                "diagnosis_type",
                "error_type",
                "reappear_count",
                "is_tamed",
                "kp_id",
            ],
            row,
            strict=False,
        )
    )


def load_kp_catalog(conn) -> list[dict]:
    """读全部知识点（匹配候选池）。按 code 排序保证提示词稳定（利于复现）。"""
    rows = conn.execute(
        "SELECT id, code, name, kp_type, tier, grade_level, parent_id,"
        " importance_weight, base_difficulty"
        " FROM knowledge_point ORDER BY code"
    ).fetchall()
    return [
        {
            "id": r[0],
            "code": r[1],
            "name": r[2],
            "kp_type": r[3],
            "tier": r[4],
            "grade_level": r[5],
            "parent_id": r[6],
            "weight": r[7],
            "difficulty": r[8],
        }
        for r in rows
    ]


def _kp_by_code(conn) -> dict[str, dict]:
    return {k["code"]: k for k in load_kp_catalog(conn)}


# ── 提示词（版本化）─────────────────────────────────────────
def build_prompts(mistake: dict, catalog: list[dict]) -> tuple[str, str]:
    """构造 (system, user)。候选清单来自 knowledge_point 表（非硬编码）。"""
    system = (
        "你是小学数学知识点标注专家。根据给定的错题信息，判断这道题考查的"
        "知识点。\n"
        "规则：\n"
        "1. 只依据给出的题面来源、错答、正答判断，不要臆测；\n"
        "2. kp_code 必须逐字使用「知识点清单」里的 code，不得改写或自创；\n"
        "3. 置信度如实评估：题目信息不足或跨多个知识点时给低值"
        "（低置信转人工复核，不会被自动采纳，宁低勿高）；\n"
        "4. 只有当所有现有知识点都明显不适配、且题目明确考查其他内容时，"
        "才提议 new_kp；\n"
        "5. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。\n"
        "输出格式：\n"
        '{"kp_code":"<清单里的code>","confidence":0.0到1.0,'
        '"rationale":"<一句话理由，须引用题面证据>",'
        '"evidence_quote":"<支撑判断的题面片段>","new_kp":null}\n'
        "或（现有清单覆盖不了时）：\n"
        '{"kp_code":null,"confidence":0.0,"rationale":"<现有清单为何覆盖不了>",'
        '"new_kp":{"code":"MATH-G4-XXX-YYY","name":"<中文名>",'
        '"parent_code":"<建议父节点code或null>","kp_type":"CONCEPT|SKILL|FACT",'
        '"rationale":"<为什么需要新增>"}}'
    )

    lines = []
    for k in catalog:
        lines.append(
            f"- {k['code']} | {k['name']} | {k['kp_type']} | "
            f"tier {k['tier']} | 年级 {k['grade_level']}"
        )
    catalog_text = "\n".join(lines) if lines else "（知识点清单为空）"

    parts = [
        f"【知识点清单】（code | 名称 | 类型 | 层级 | 年级）\n{catalog_text}",
        "",
        "【错题信息】",
        f"科目：{mistake.get('subject')}",
        f"题面来源：{mistake.get('source_ref') or '未知'}",
    ]
    if mistake.get("wrong_answer"):
        parts.append(f"学生作答（错）：{str(mistake['wrong_answer'])[:300]}")
    if mistake.get("correct_answer"):
        parts.append(f"正确答案：{str(mistake['correct_answer'])[:300]}")
    if mistake.get("diagnosis_type"):
        parts.append(f"错因标签：{mistake['diagnosis_type']}")
    if mistake.get("reappear_count"):
        parts.append(f"复现次数：{mistake['reappear_count']}")
    parts.append("")
    parts.append("请输出 JSON。")
    return system, "\n".join(parts)


# ── 单次分析（双跑闸门的原子单元）───────────────────────────
def _single_run(system_prompt: str, user_prompt: str, client) -> dict:
    """一次独立 LLM 调用 + 解析。失败不抛，返回可判空 dict。"""
    chain = run_chain(system_prompt, user_prompt, client=client, version_stamp=PROMPT_VERSION)
    out: dict = {
        "source_layer": chain.source,
        "parsed": None,
        "code": None,
        "conf": 0.0,
        "rationale": "",
        "new_kp": None,
    }
    parsed = parse_llm_json(chain.answer)
    if parsed:
        out["parsed"] = parsed
        code = str(parsed.get("kp_code") or "").strip()
        out["code"] = code or None
        out["conf"] = _clamp_conf(parsed.get("confidence"))
        out["rationale"] = str(parsed.get("rationale") or "").strip()[:500]
        nk = parsed.get("new_kp") or None
        out["new_kp"] = nk if isinstance(nk, dict) and nk.get("code") else None
    return out


# ── 主入口 ──────────────────────────────────────────────────
def match_kp_for_mistake(
    conn,
    mistake_id: int,
    *,
    client: MiniMaxClient | None = None,
    confirm_runs: int | None = None,
    allow_new_kp: bool = True,
) -> dict:
    """LLM 匹配知识点 → 双跑闸门 → 挂载 / 落提案。

    返回 dict：{ok, proposal_id, status, kp_code, kp_id, confidence,
                source_layer, gate, confirm, candidate_id, errors}
    绝不抛出（录入/复习主流程不能被匹配打断）。
    """
    result: dict = {
        "ok": False,
        "proposal_id": None,
        "status": None,
        "kp_code": None,
        "kp_id": None,
        "confidence": None,
        "source_layer": None,
        "gate": None,
        "errors": [],
    }
    stamp = fmt_ts(now_utc())
    try:
        mistake = _load_mistake(conn, mistake_id)
        if not mistake:
            result["errors"].append(f"错题 #{mistake_id} 不存在")
            return result

        catalog = load_kp_catalog(conn)
        system_prompt, user_prompt = build_prompts(mistake, catalog)
        runs = AUTO_CONFIRM_RUNS if confirm_runs is None else max(1, int(confirm_runs))

        # —— 第 1 次分析（权威）——
        first = _single_run(system_prompt, user_prompt, client)
        result["source_layer"] = first["source_layer"]

        if first["source_layer"] == "L0" or first["parsed"] is None:
            reason = (
                "LLM 不可达（降级至 L0），待人工归因"
                if first["source_layer"] == "L0"
                else f"LLM 输出不可解析（来源 {first['source_layer']}），待人工归因"
            )
            pid = _insert_proposal(
                conn,
                mistake_id=mistake_id,
                proposed_kp_id=None,
                proposed_kp_code=None,
                confidence=0.0,
                rationale=reason,
                source_layer=first["source_layer"],
                model=None,
                prompt_version=PROMPT_VERSION,
                confirm_json=None,
                status=STATUS_PROPOSED,
            )
            result.update(ok=True, proposal_id=pid, status=STATUS_PROPOSED, gate="manual_queue")
            result["errors"].append(reason)
            return result

        code = first["code"]
        conf = first["conf"]
        rationale = first["rationale"]
        new_kp = first["new_kp"]

        # 新知识点 → 候选通道（King：绝不自动新增）
        if allow_new_kp and new_kp:
            cid = register_kp_candidate(
                conn,
                candidate_code=str(new_kp["code"]).strip(),
                candidate_name=str(new_kp.get("name") or new_kp["code"]),
                parent_code=(
                    str(new_kp.get("parent_code")).strip() if new_kp.get("parent_code") else None
                ),
                kp_type=str(new_kp.get("kp_type") or "CONCEPT"),
                rationale=str(new_kp.get("rationale") or rationale)[:500],
                sample={"mistake_id": mistake_id, "source_ref": mistake.get("source_ref")},
            )
            result["candidate_id"] = cid

        # code 合法性：必须在 knowledge_point 表中
        kp_map = _kp_by_code(conn)
        valid = bool(code) and code in kp_map
        if code and not valid:
            result["errors"].append(f"LLM 返回的 kp_code='{code}' 不在知识点表中 —— 转人工复核")

        # —— 闸门：双跑一致才自动 ——
        status = STATUS_PROPOSED
        gate = "manual_queue"
        decided_by = None
        confirm_audit: dict | None = None

        if valid and conf >= AUTO_ACCEPT_THRESHOLD and runs > 1:
            runs_log = [
                {"run": 1, "code": code, "confidence": conf, "source_layer": first["source_layer"]}
            ]
            all_agree = True
            for i in range(runs - 1):
                nxt = _single_run(system_prompt, user_prompt, client)
                # 复核跑技术性失败（调用成功但输出不可解析）→ 重试；
                # 与"语义不一致"（两次给出不同 code）严格区分（2026-09-12 修复）。
                retries = 0
                while retries < CONFIRM_RETRY_MAX and is_technical_failure(nxt):
                    retries += 1
                    logger.warning(
                        "复核跑 #%s 输出不可解析，重试 %s/%s（mistake=%s）",
                        i + 2,
                        retries,
                        CONFIRM_RETRY_MAX,
                        mistake_id,
                    )
                    nxt = _single_run(system_prompt, user_prompt, client)
                entry = {
                    "run": i + 2,
                    "code": nxt["code"],
                    "confidence": nxt["conf"],
                    "source_layer": nxt["source_layer"],
                }
                if retries:
                    entry["retries"] = retries
                runs_log.append(entry)
                if nxt["code"] != code or nxt["conf"] < AUTO_ACCEPT_THRESHOLD:
                    all_agree = False
                    result["errors"].append(
                        f"复核跑 #{i + 2} 未过一致闸门："
                        f"code={nxt['code'] or '空'} conf={nxt['conf']:.2f}"
                    )
            confirm_audit = {"policy": "two_run_agreement", "runs": runs_log, "agreed": all_agree}
            if all_agree:
                status, gate, decided_by = STATUS_AUTO, "auto", "LLM_ASSISTED"
            else:
                rationale = (rationale + " [复核不一致，转人工]")[:500]
        elif valid and conf >= AUTO_ACCEPT_THRESHOLD and runs == 1:
            status, gate, decided_by = STATUS_AUTO, "auto", "LLM_ASSISTED"

        kp_id = kp_map[code]["id"] if (valid and status == STATUS_AUTO) else None
        pid = _insert_proposal(
            conn,
            mistake_id=mistake_id,
            proposed_kp_id=kp_map[code]["id"] if valid else None,
            proposed_kp_code=code if valid else None,
            confidence=conf,
            rationale=rationale,
            source_layer=first["source_layer"],
            model=getattr(client, "model", None) or "MiniMax-M3",
            prompt_version=PROMPT_VERSION,
            confirm_json=json.dumps(confirm_audit, ensure_ascii=False) if confirm_audit else None,
            status=status,
            decided_by=decided_by,
            decided_at=stamp if decided_by else None,
        )
        result.update(
            ok=True,
            proposal_id=pid,
            status=status,
            gate=gate,
            kp_code=code if valid else None,
            kp_id=kp_id,
            confidence=conf,
            rationale=rationale,
        )
        if confirm_audit:
            result["confirm"] = confirm_audit

        # 闸门放行：写权威字段（mistake_record.kp_id + 回填计划）
        if status == STATUS_AUTO and kp_id:
            written = _write_authoritative_kp(conn, mistake_id=mistake_id, kp_id=kp_id)
            result["written"] = written
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识点匹配失败 mistake_id=%s", mistake_id)
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def _insert_proposal(
    conn,
    *,
    mistake_id,
    proposed_kp_id,
    proposed_kp_code,
    confidence,
    rationale,
    source_layer,
    model,
    prompt_version,
    confirm_json,
    status,
    decided_by=None,
    decided_at=None,
) -> int:
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "INSERT INTO kp_match_proposal"
        " (mistake_id, proposed_kp_id, proposed_kp_code, confidence, rationale,"
        "  source_layer, model, prompt_version, confirm_json, status,"
        "  decided_by, decided_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            mistake_id,
            proposed_kp_id,
            proposed_kp_code,
            float(confidence),
            rationale,
            source_layer,
            model,
            prompt_version,
            confirm_json,
            status,
            decided_by,
            decided_at,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _write_authoritative_kp(conn, *, mistake_id: int, kp_id: int) -> dict:
    """写权威挂载边：mistake_record.kp_id + 回填该错题所有悬空计划的 kp_id。

    为什么回填 review_schedule：错题已挂载但计划还悬空（NULL/0）会造成
    半挂载态——Phase 2 的 kp_state 生产者与报告统计都会受影响。
    """
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE mistake_record SET kp_id=?, updated_at=? WHERE id=?",
        (kp_id, stamp, mistake_id),
    )
    n_sched = conn.execute(
        "UPDATE review_schedule SET kp_id=?, updated_at=?"
        " WHERE mistake_id=? AND (kp_id IS NULL OR kp_id=0)",
        (kp_id, stamp, mistake_id),
    ).rowcount
    conn.commit()
    return {"mistake_updated": bool(cur.rowcount), "schedules_backfilled": n_sched}


# ── 新知识点候选（LLM 提候选 + 人工确认晋级）────────────────
def register_kp_candidate(
    conn,
    *,
    candidate_code: str,
    candidate_name: str,
    parent_code: str | None = None,
    kp_type: str = "CONCEPT",
    rationale: str | None = None,
    sample: dict | None = None,
) -> int:
    """登记新知识点候选；同 code 重复出现累加证据数（同 taxonomy_candidate 模式）。"""
    stamp = fmt_ts(now_utc())
    row = conn.execute(
        "SELECT id, evidence_count, sample_json FROM kp_candidate WHERE candidate_code=?",
        (candidate_code,),
    ).fetchone()
    if row:
        cid, cnt, old = row[0], int(row[1]), row[2]
        try:
            samples = json.loads(old) if old else []
        except Exception:  # noqa: BLE001
            samples = []
        if sample:
            samples.append(sample)
        conn.execute(
            "UPDATE kp_candidate SET evidence_count=?, sample_json=?,"
            " rationale=COALESCE(?, rationale), updated_at=? WHERE id=?",
            (cnt + 1, json.dumps(samples[-20:], ensure_ascii=False), rationale, stamp, cid),
        )
        conn.commit()
        return cid
    cur = conn.execute(
        "INSERT INTO kp_candidate"
        " (candidate_code, candidate_name, parent_code, kp_type, rationale,"
        "  evidence_count, sample_json, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,1,?, 'PENDING', ?, ?)",
        (
            candidate_code,
            candidate_name,
            parent_code,
            kp_type,
            rationale,
            json.dumps([sample] if sample else [], ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_candidates(conn, *, status: str = "PENDING") -> list[dict]:
    """列出候选（默认待处理）。含系统建议、LLM 二审结论与关联错题（2026-09-12 扩展）。

    samples 字段（King 需求：候选 → 关联题号 → 原图 的超链接）：
      候选注册时 sample_json 已存 {mistake_id, source_ref}；此处解析并按 mistake_id
      联表补齐该错题的 source_ref（取最新）与**原图绝对路径**（文件存在时），
      供前端"点击题号跳转到该题原图"。匹配一律用 mistake_id（主键）精确匹配，
      绝不用 rationale 文本里的数字（防误配）。
    """
    rows = conn.execute(
        "SELECT id, candidate_code, candidate_name, parent_code, kp_type,"
        " rationale, evidence_count, status, created_at,"
        " suggestion, suggestion_reason, reject_reason, llm_review_json, sample_json"
        " FROM kp_candidate WHERE status=? ORDER BY evidence_count DESC, created_at",
        (status,),
    ).fetchall()
    keys = [
        "id",
        "candidate_code",
        "candidate_name",
        "parent_code",
        "kp_type",
        "rationale",
        "evidence_count",
        "status",
        "created_at",
        "suggestion",
        "suggestion_reason",
        "reject_reason",
        "llm_review_json",
        "sample_json",
    ]
    out = []
    for r in rows:
        d = dict(zip(keys, r, strict=False))
        # llm_review_json → 结构化（前端直接展示 verdict/rationale）
        raw = d.pop("llm_review_json", None)
        d["llm_review"] = None
        if raw:
            try:
                d["llm_review"] = json.loads(raw)
            except Exception:  # noqa: BLE001
                d["llm_review"] = {"verdict": "UNCERTAIN", "rationale": str(raw)[:200]}
        # 关联错题样本（含原图路径）
        d["samples"] = _samples_enriched(conn, d.pop("sample_json", None))
        out.append(d)
    return out


def _samples_enriched(conn, sample_json) -> list[dict]:
    """解析 sample_json → [{mistake_id, source_ref, img_original, has_photo}]。

    - 按 mistake_id 去重、保序（候选可累积多条证据样本）
    - source_ref 以 mistake_record 现值优先（样本里的是注册时快照，可能已更新）
    - img_original 仅在**文件真实存在**时给出绝对路径（供 /api/image 兜底直开原图），
      否则置 None + has_photo=False（前端据此提示"原图缺失"，不做无效请求）
    """
    try:
        arr = json.loads(sample_json) if sample_json else []
    except Exception:  # noqa: BLE001
        arr = []
    items: list[dict] = []
    seen: set[int] = set()
    for s in arr:
        if not isinstance(s, dict):
            continue
        try:
            mid = int(s.get("mistake_id"))
        except (TypeError, ValueError):
            continue
        if mid in seen:
            continue
        seen.add(mid)
        items.append(
            {
                "mistake_id": mid,
                "source_ref": str(s.get("source_ref") or ""),
                "img_original": None,
                "has_photo": False,
            }
        )
    if not items:
        return []
    q = ",".join("?" * len(items))
    info = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            f"SELECT id, source_ref, img_original FROM mistake_record WHERE id IN ({q})",
            [it["mistake_id"] for it in items],
        ).fetchall()
    }
    try:
        from fdl_core.paths import get_paths

        root = Path(get_paths().root)
    except Exception:  # noqa: BLE001
        root = None
    for it in items:
        ref, img = info.get(it["mistake_id"], (None, None))
        if ref:
            it["source_ref"] = str(ref)
        if img and root is not None:
            p = Path(img)
            src = p if p.is_absolute() else (root / img)
            try:
                if src.is_file():
                    it["img_original"] = str(src)
                    it["has_photo"] = True
            except OSError:
                pass
    return items


def _parse_code_meta(code: str) -> tuple[str | None, int | None]:
    """从 KP code 解析 (subject_code, grade_level)。

    形如 MATH-G4-PATTERN-TABLE → ('MATH', 4)
         CHN-G4-CHAR-DIS      → ('CHINESE', 4)
    解析不出返回 None（调用方给默认）。前缀映射表在此集中，避免多处硬编码。
    """
    parts = str(code or "").upper().split("-")
    prefix = parts[0] if parts else ""
    subject = {"MATH": "MATH", "CHN": "CHINESE", "CHINESE": "CHINESE"}.get(prefix)
    grade = None
    for p in parts:
        if len(p) == 2 and p[0] == "G" and p[1].isdigit():
            grade = int(p[1])
            break
    return subject, grade


def promote_candidate(
    conn,
    candidate_id: int,
    *,
    decided_by: str = "PARENT",
    parent_code: str | None = None,
    grade_level: int | None = None,
    subject_code: str | None = None,
) -> dict:
    """人工确认 → 候选晋级为正式知识点（写入 knowledge_point）。

    subject/grade 默认从 code 解析（MATH-G3-* → MATH/G3）；
    显式传入可覆盖（用于代码格式不规范、需人工校正的候选）。
    """
    row = conn.execute(
        "SELECT candidate_code, candidate_name, parent_code, kp_type, rationale"
        " FROM kp_candidate WHERE id=?",
        (candidate_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"候选不存在：{candidate_id}"}
    code, name, cand_parent, kp_type, rationale = row
    # 父节点：优先显式传入 → 候选建议 → NULL
    parent_final = parent_code or cand_parent
    parent_id = None
    if parent_final:
        prow = conn.execute(
            "SELECT id FROM knowledge_point WHERE code=?", (parent_final,)
        ).fetchone()
        parent_id = prow[0] if prow else None

    # subject / grade：显式参数 > 从 code 解析 > 兜底
    sub_from_code, grade_from_code = _parse_code_meta(code)
    final_subject = subject_code or sub_from_code or "MATH"
    final_grade = grade_level or grade_from_code or 4
    srow = conn.execute("SELECT id FROM subject WHERE code=?", (final_subject,)).fetchone()
    subject_id = srow[0] if srow else 1
    stamp = fmt_ts(now_utc())
    try:
        cur = conn.execute(
            "INSERT INTO knowledge_point"
            " (subject_id, parent_id, code, name, description, grade_level,"
            "  semester, knowledge_domain, bloom_level, abstraction_level,"
            "  importance_weight, exam_frequency, base_difficulty,"
            "  est_learn_minutes, est_review_seconds, kp_type, source, source_ref,"
            "  tier, tier_reason, graph_version, valid_from, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,NULL,NULL,1,1, 0.8,NULL,4.0, 20.0,120,?,"
            "  'CUSTOM',?, 'L1','由 LLM 候选晋级', 'g1', ?, ?, ?)",
            (
                subject_id,
                parent_id,
                code,
                name,
                f"由 LLM 候选 #{candidate_id} 晋级（decided_by={decided_by}）",
                final_grade,
                kp_type,
                rationale,
                stamp[:10],
                stamp,
                stamp,
            ),
        )
        new_id = int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        return {"ok": False, "error": f"写入知识点失败：{type(exc).__name__}: {exc}"}

    conn.execute(
        "UPDATE kp_candidate SET status='PROMOTED', promoted_kp_id=?,"
        " decided_by=?, decided_at=?, updated_at=? WHERE id=?",
        (new_id, decided_by, stamp, stamp, candidate_id),
    )
    conn.commit()
    return {
        "ok": True,
        "promoted_kp_id": new_id,
        "code": code,
        "name": name,
        "subject": final_subject,
        "grade_level": final_grade,
    }


def reject_candidate(conn, candidate_id: int, *, decided_by: str = "PARENT") -> dict:
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE kp_candidate SET status='REJECTED', decided_by=?, decided_at=?,"
        " updated_at=? WHERE id=? AND status='PENDING'",
        (decided_by, stamp, stamp, candidate_id),
    )
    conn.commit()
    return {"ok": bool(cur.rowcount), "candidate_id": candidate_id}


# ── 提案复核 API ────────────────────────────────────────────
def list_proposals(conn, *, status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT id, mistake_id, proposed_kp_id, proposed_kp_code, confidence,"
            " rationale, source_layer, status, created_at FROM kp_match_proposal"
            " WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, mistake_id, proposed_kp_id, proposed_kp_code, confidence,"
            " rationale, source_layer, status, created_at FROM kp_match_proposal"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    keys = [
        "id",
        "mistake_id",
        "proposed_kp_id",
        "proposed_kp_code",
        "confidence",
        "rationale",
        "source_layer",
        "status",
        "created_at",
    ]
    return [dict(zip(keys, r, strict=False)) for r in rows]


def accept_proposal(conn, proposal_id: int, *, decided_by: str = "PARENT") -> dict:
    """人工采纳 → 写 kp_id（走 _write_authoritative_kp，同步回填计划）。

    🔴 2026-09-13 幂等修复（King 报"操作失败：提案已被采纳"）：
    原实现对已 ACCEPTED 的提案直接返回错误 → 前端重复操作（批量同意后
    再点单条 / 双击 / 多标签页）时误报失败，用户以为没成功。
    现语义：**已采纳 = 成功**（幂等），并顺带自愈校验权威挂载边是否在位
    （防"状态已 ACCEPTED 但 kp_id 未写"的中断残留）；仅"提案不存在"
    与"无可采纳项"仍为真错误。
    """
    row = conn.execute(
        "SELECT mistake_id, proposed_kp_id, proposed_kp_code, confidence, status"
        " FROM kp_match_proposal WHERE id=?",
        (proposal_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"提案不存在：{proposal_id}"}
    mistake_id, kp_id, code, conf, status = row
    if status in (STATUS_ACCEPTED, STATUS_AUTO):
        # 幂等成功 + 自愈：若权威挂载边缺失（中断残留），补写
        healed = False
        if kp_id:
            cur = conn.execute(
                "SELECT kp_id FROM mistake_record WHERE id=?", (mistake_id,)
            ).fetchone()
            if cur and (cur[0] or 0) != kp_id:
                _write_authoritative_kp(conn, mistake_id=mistake_id, kp_id=kp_id)
                healed = True
        return {
            "ok": True,
            "already": True,
            "self_healed": healed,
            "kp_id": kp_id,
            "code": code,
            "note": "该提案此前已采纳（本次为重复操作，已幂等通过）",
        }
    if not kp_id:
        return {"ok": False, "error": "提案无有效 proposed_kp_id，无可采纳项"}
    stamp = fmt_ts(now_utc())
    conn.execute(
        "UPDATE kp_match_proposal SET status=?, decided_by=?, decided_at=?,"
        " updated_at=? WHERE id=?",
        (STATUS_ACCEPTED, decided_by, stamp, stamp, proposal_id),
    )
    written = _write_authoritative_kp(conn, mistake_id=mistake_id, kp_id=kp_id)
    return {"ok": True, "written": written, "kp_id": kp_id, "code": code}


def reject_proposal(
    conn, proposal_id: int, *, decided_by: str = "PARENT", reason: str | None = None
) -> dict:
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE kp_match_proposal SET status=?, decided_by=?, decided_at=?,"
        " rationale=COALESCE(rationale || CHAR(10) || ?, rationale), updated_at=?"
        " WHERE id=?",
        (
            STATUS_REJECTED,
            decided_by,
            stamp,
            f"[人工驳回] {reason}" if reason else None,
            stamp,
            proposal_id,
        ),
    )
    conn.commit()
    return {"ok": bool(cur.rowcount), "proposal_id": proposal_id}


# ── 系统建议（LLM 去重分析，2026-09-12 King 需求）────────────────
SUGGESTION_PROMPT_VERSION = "kp-suggest-v1"
REVIEW_PROMPT_VERSION = "kp-review-v1"


def compute_suggestions_llm(conn, *, client: MiniMaxClient | None = None, limit: int = 60) -> dict:
    """LLM 分析全部 PENDING 候选，产出「建议晋级/建议驳回 + 理由」写入表。

    设计：
    - 规则先行：code 已存在于 knowledge_point → 直接标 REJECT「已存在」，不进 LLM。
    - 其余候选**一次打包**送 LLM 做去重+质量判断（比逐条调用省 token 且能横向比较）。
    - LLM 输出 JSON 数组；解析失败/不可达 → 全部标 PENDING_REVIEW（不猜）。
    返回 {ok, total, approve, reject, rule_rejected, source_layer, errors}
    """
    result: dict = {
        "ok": False,
        "total": 0,
        "approve": 0,
        "reject": 0,
        "rule_rejected": 0,
        "source_layer": None,
        "errors": [],
    }
    try:
        rows = conn.execute(
            "SELECT id, candidate_code, candidate_name, kp_type, evidence_count,"
            " rationale FROM kp_candidate WHERE status='PENDING'"
            " ORDER BY evidence_count DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            result["ok"] = True
            return result
        result["total"] = len(rows)

        # 规则层：code 已存在正式知识点 → 直接驳回（无需 LLM）
        existing = {r[0] for r in conn.execute("SELECT code FROM knowledge_point")}
        llm_rows = []
        stamp = fmt_ts(now_utc())
        for cid, code, name, ktype, ev, why in rows:
            if code in existing:
                conn.execute(
                    "UPDATE kp_candidate SET suggestion='REJECT',"
                    " suggestion_reason=?, updated_at=? WHERE id=?",
                    (f"已存在同名知识点 {code}（无需新增）", stamp, cid),
                )
                result["rule_rejected"] += 1
            else:
                llm_rows.append((cid, code, name, ktype, ev, why))
        conn.commit()

        if llm_rows:
            # LLM 层：打包判断（去重 + 质量）
            catalog = conn.execute(
                "SELECT code, name FROM knowledge_point ORDER BY code"
            ).fetchall()
            cat_text = "\n".join(f"- {c} | {n}" for c, n in catalog)
            cand_lines = []
            for cid, _code, _name, _ktype, _ev, _why in llm_rows:
                cand_lines.append(
                    f"[{cid}] {code} | {name} | {ktype} | 证据×{ev} | 理由：{str(why or '')[:120]}"
                )
            system = (
                "你是小学数学知识点库的管理员。给你一份「现有知识点清单」和一份"
                "「待新增候选清单」，请判断每个候选是否应该正式新增。\n"
                "判断标准：\n"
                "1. 若候选与**已有知识点**语义重复（换个名字/同义表述）→ 建议驳回；\n"
                "2. 若候选与**其他候选**互相重复 → 只保留描述最具体、证据最多的一个"
                "（其余建议驳回，理由里注明与哪个编号重复）；\n"
                "3. 若候选是混合体（把两个不同知识点揉在一起）→ 建议驳回，"
                "理由说明应拆分；\n"
                "4. 其余有独立知识价值的候选 → 建议晋级。\n"
                "只输出一个 JSON 对象，不要 markdown 代码块：\n"
                '{"suggestions":[{"id":<候选编号数字>,"suggestion":"APPROVE"或"REJECT",'
                '"reason":"<一句话理由，驳回时须注明原因/与谁重复>"}]}'
            )
            user = (
                f"【现有知识点清单】（code | 名称）\n{cat_text}\n\n"
                f"【待新增候选清单】（[编号] code | 名称 | 类型 | 证据 | 理由）\n"
                + "\n".join(cand_lines)
                + "\n\n请对每个候选给出建议。"
            )
            chain = run_chain(system, user, client=client, version_stamp=SUGGESTION_PROMPT_VERSION)
            result["source_layer"] = chain.source
            parsed = parse_llm_json(chain.answer)
            sug_map: dict[int, dict] = {}
            if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
                for s in parsed["suggestions"]:
                    if not isinstance(s, dict):
                        continue
                    try:
                        sid = int(s.get("id"))
                    except (TypeError, ValueError):
                        continue
                    verdict = str(s.get("suggestion") or "").upper()
                    sug_map[sid] = {
                        "suggestion": "REJECT" if verdict == "REJECT" else "APPROVE",
                        "reason": str(s.get("reason") or "")[:300],
                    }
            for cid, _code, _name, _ktype, _ev, _why in llm_rows:
                got = sug_map.get(cid)
                if got:
                    conn.execute(
                        "UPDATE kp_candidate SET suggestion=?, suggestion_reason=?,"
                        " updated_at=? WHERE id=?",
                        (got["suggestion"], got["reason"], stamp, cid),
                    )
                    result["approve" if got["suggestion"] == "APPROVE" else "reject"] += 1
                else:
                    # LLM 未覆盖该条 → 中性标 PENDING_REVIEW（不猜）
                    conn.execute(
                        "UPDATE kp_candidate SET suggestion='PENDING_REVIEW',"
                        " suggestion_reason=?, updated_at=? WHERE id=?",
                        ("LLM 未给出判断（输出缺失该条），待人工直接决定", stamp, cid),
                    )
            conn.commit()
            if chain.source == "L0":
                result["errors"].append("LLM 不可达（L0），建议未生成")
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("候选建议生成失败")
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def review_rejection_llm(
    conn, candidate_id: int, *, reason: str, client: MiniMaxClient | None = None
) -> dict:
    """用户驳回 → LLM 二审：判断驳回理由是否成立。

    返回 {ok, verdict, rationale, source_layer, errors}
    verdict 取值：
      UPHOLD     维持驳回（用户理由成立）
      OVERTURN   建议复议（用户理由不成立，候选有独立价值）
      UNCERTAIN  存疑（信息不足，建议人工再判）
    结论写入 kp_candidate.llm_review_json + llm_reviewed_at（审计链）。
    """
    out: dict = {
        "ok": False,
        "verdict": None,
        "rationale": None,
        "source_layer": None,
        "errors": [],
    }
    try:
        row = conn.execute(
            "SELECT candidate_code, candidate_name, kp_type, rationale, evidence_count,"
            " sample_json FROM kp_candidate WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if not row:
            out["errors"].append(f"候选不存在：{candidate_id}")
            return out
        code, name, ktype, why, ev, sample = row
        system = (
            "你是知识点库的复核员。用户（家长）驳回了一个「新增知识点候选」，"
            "并给出了驳回原因。你的任务是**客观判断这个驳回理由是否成立**。\n"
            "判断原则：\n"
            "1. 若用户理由指向的事实成立（例如该知识点已在库中、与现有内容重复、"
            "超出学习范围、表述有误）→ 维持驳回；\n"
            "2. 若用户理由与候选事实不符、或候选确有其独立价值（错题中反复出现、"
            "现有知识点确实覆盖不了）→ 建议复议；\n"
            "3. 若信息不足以判断 → 存疑，建议人工再判。\n"
            "保持中立：**不迎合用户**，也不维护系统面子——只依据事实判断。\n"
            "只输出一个 JSON 对象，不要 markdown 代码块：\n"
            '{"verdict":"UPHOLD"或"OVERTURN"或"UNCERTAIN",'
            '"rationale":"<两三句话说明判断依据>"}'
        )
        user = (
            f"【候选知识点】\n"
            f"code: {code}\n名称: {name}\n类型: {ktype}\n"
            f"证据数: {ev}\n系统提出的理由: {str(why or '')[:300]}\n\n"
            f"【用户的驳回原因】\n{str(reason or '')[:500]}\n\n"
            "请判断该驳回理由是否成立。"
        )
        chain = run_chain(system, user, client=client, version_stamp=REVIEW_PROMPT_VERSION)
        out["source_layer"] = chain.source
        parsed = parse_llm_json(chain.answer)
        verdict = "UNCERTAIN"
        rationale = ""
        if isinstance(parsed, dict):
            v = str(parsed.get("verdict") or "").upper()
            if v in ("UPHOLD", "OVERTURN", "UNCERTAIN"):
                verdict = v
            rationale = str(parsed.get("rationale") or "")[:500]
        if chain.source == "L0" or not rationale:
            verdict = "UNCERTAIN"
            rationale = rationale or "LLM 不可达或输出不可解析——建议人工再判"
            out["errors"].append("LLM 二审未有效完成")
        stamp = fmt_ts(now_utc())
        payload = json.dumps(
            {
                "verdict": verdict,
                "rationale": rationale,
                "user_reason": str(reason or "")[:500],
                "source_layer": chain.source,
                "ts": stamp,
            },
            ensure_ascii=False,
        )
        conn.execute(
            "UPDATE kp_candidate SET llm_review_json=?, llm_reviewed_at=?, updated_at=? WHERE id=?",
            (payload, stamp, stamp, candidate_id),
        )
        conn.commit()
        out.update(ok=True, verdict=verdict, rationale=rationale)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.exception("驳回二审失败 candidate_id=%s", candidate_id)
        out["errors"].append(f"{type(exc).__name__}: {exc}")
        return out


def reject_candidate_with_reason(
    conn,
    candidate_id: int,
    *,
    reason: str,
    decided_by: str = "FRANK",
    client: MiniMaxClient | None = None,
) -> dict:
    """驳回（带原因）→ 落库 → 触发 LLM 二审 → 返回二审结论（供前端展示）。

    与 reject_candidate 的区别：要求 reason 非空 + 执行 LLM 二审 + 返回结论。
    """
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "驳回必须填写原因"}
    # 先落驳回（含原因）
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE kp_candidate SET status='REJECTED', reject_reason=?, decided_by=?,"
        " decided_at=?, updated_at=? WHERE id=? AND status='PENDING'",
        (reason[:500], decided_by, stamp, stamp, candidate_id),
    )
    conn.commit()
    if not cur.rowcount:
        row = conn.execute("SELECT status FROM kp_candidate WHERE id=?", (candidate_id,)).fetchone()
        return {
            "ok": False,
            "error": f"候选不存在或非 PENDING（当前：{row[0] if row else '不存在'}）",
        }
    # LLM 二审（失败不影响驳回本身已成立）
    review = review_rejection_llm(conn, candidate_id, reason=reason, client=client)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": "REJECTED",
        "reject_reason": reason,
        "llm_review": review,
    }


# ── 异步触发（confirm 端点挂钩入口）────────────────────────
_ASYNC_DELAY_SEC = 1.0
_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()


def _db_path_of(conn) -> str:
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                return row[2] or ":memory:"
    except Exception:  # noqa: BLE001
        pass
    return ":memory:"


def schedule_async_match(conn, *, mistake_id: int) -> dict:
    """后台线程触发知识点匹配。绝不抛、绝不阻塞调用方。同任务去重。"""
    try:
        db_path = _db_path_of(conn)
        if not db_path or db_path == ":memory:":
            # 内存库：降级为同步执行（子线程新连接看不到内存库）
            return match_kp_for_mistake(conn, mistake_id, client=get_default_client())
        key = (db_path, int(mistake_id))
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                return {"ok": False, "reason": "同任务已在跑", "duplicate": True}
            _INFLIGHT.add(key)

        def _work() -> None:
            try:
                _time.sleep(_ASYNC_DELAY_SEC)
                c = get_connection(db_path)
                try:
                    match_kp_for_mistake(c, mistake_id, client=get_default_client())
                finally:
                    c.close()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "异步知识点匹配失败（不影响主流程）mistake_id=%s",
                    mistake_id,
                    exc_info=True,
                )
            finally:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(key)

        threading.Thread(target=_work, name=f"fdl-kpmat-{mistake_id}", daemon=True).start()
        return {"ok": True, "async": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识点匹配调度失败（不影响主流程）：%s", exc)
        return {"ok": False, "reason": str(exc)}

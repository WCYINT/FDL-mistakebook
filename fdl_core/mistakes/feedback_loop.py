"""反馈层闭环方案 B（LLM 深度）—— 复习反馈 → 断点识别 → 个性化干预 → 落库 → 回标。

定位
----
方案 A（`attribution_engine`）解决的是"**错因是什么**"：每次复习反馈异步推断
`diagnosis_type`，走两次一致闸门晋升为正式归因。
方案 B（本模块）解决的是"**这一题现在卡在哪、下一步做什么**"：把该题的
「历史反馈趋势 + 既有归因提案 + 错因族 + 知识点掌握度」交给 LLM 做**断点识别**，
产出**个性化、可执行**的干预动作，写 `intervention_action`
（trigger='FEEDBACK_LOOP'），在报告驾驶舱展示、执行后回标 DONE。

与 Pareto 模块的关系（结构镜像）
--------------------------------
`fdl_core/mistakes/intervention.py` 是**全局**层（Pareto 根因 → 措施，一条聚合记录）；
本模块是**单题**层（单题断点 → 措施，一条/题/反馈）。二者共用：
- `collect_*evidence` / `build_*prompt` / `analyze_*` / `_persist` / `latest_*` 五段结构；
- `run_chain` + `parse_llm_json` + `ACTION_FAMILY` 锚点 + `version_stamp` 审计。

安全与闸门
----------
- LLM 不可达（L0）→ 返回 None，**不写库、不猜**；
- 解析失败（非 JSON 对象）→ 复用 `CONFIRM_RETRY_MAX` 重试；仍失败 → 记日志返回 None；
- 置信闸门 `GATE`：`confidence < GATE` 仍**写库**，但 `payload["needs_review"]=True`
  ——低置信不静默丢弃，转人工复核（防幻觉）；
- 异步调度（`schedule_async_feedback_loop`）**绝不抛异常、绝不阻塞复习主流程**。

幂等
----
同一 `feedback_id` 只留一条 FEEDBACK_LOOP 行（`payload_json.feedback_id` 去重）；
同题新反馈写入时，旧 PENDING 自动标 SKIPPED（与 PARETO `_persist` 同语义：只留最新）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time as _time

from fdl_core.db.schema import get_connection
from fdl_core.l2.fallback import run_chain
from fdl_core.mistakes.attribution_engine import (
    CONFIRM_RETRY_MAX,
    _db_path_of,
    get_default_client,
    parse_llm_json,
)
from fdl_core.mistakes.intervention import ACTION_FAMILY
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.mistakes.feedback_loop")

FEEDBACK_PROMPT_VERSION = "feedback-loop-v1"
TRIGGER = "FEEDBACK_LOOP"

# 置信闸门：低于阈值仍写库，但标 needs_review=True 转人工（防幻觉，不静默丢弃）。
GATE: float = float(os.environ.get("FDL_FEEDBACK_GATE", "0.6"))

_BREAKPOINTS = {"CONCEPT", "CALC", "MISREAD", "NORM"}
_ACTION_TYPES = {"RETEACH", "SIMPLIFY", "HINT", "PARENT_NUDGE", "DIAGNOSE"}
_ALIGNMENTS = {"consistent", "revised"}
_RATING_LABEL = {1: "陌生", 2: "模糊", 3: "掌握", 4: "熟练"}


# ── 证据采集 ────────────────────────────────────────────────
def _fb_row_to_dict(row) -> dict | None:
    if not row:
        return None
    return {
        "id": row[0],
        "self_rating": row[1],
        "duration_seconds": row[2],
        "note": row[3],
        "schedule_id": row[4],
        "created_at": row[5],
    }


def _resolve_target_feedback(conn, mistake_id: int, feedback_id: int | None) -> dict | None:
    """定位本次要分析的反馈行。

    `review_feedback` 没有 `mistake_id` 列，归属必须经 `review_schedule` JOIN
    （仓库既有口径，见 attribution_engine._resolve_feedback）。
    """
    if feedback_id is not None:
        row = conn.execute(
            "SELECT id, self_rating, duration_seconds, note, schedule_id, created_at"
            " FROM review_feedback WHERE id=?",
            (int(feedback_id),),
        ).fetchone()
        return _fb_row_to_dict(row)
    row = conn.execute(
        "SELECT rf.id, rf.self_rating, rf.duration_seconds, rf.note,"
        " rf.schedule_id, rf.created_at"
        " FROM review_feedback rf"
        " JOIN review_schedule rs ON rs.id = rf.schedule_id"
        " WHERE rs.mistake_id = ?"
        " ORDER BY rf.created_at DESC, rf.id DESC LIMIT 1",
        (int(mistake_id),),
    ).fetchone()
    return _fb_row_to_dict(row)


def collect_feedback_evidence(
    conn, *, mistake_id: int, feedback_id: int | None = None, history_limit: int = 5
) -> dict:
    """第一步：采集单题证据（只读不猜）。任一子表缺失只记 degraded，不抛。"""
    degraded: list[str] = []

    m_row = conn.execute(
        "SELECT id, subject, diagnosis_type, error_type, reappear_count, is_tamed,"
        " kp_id, user_id"
        " FROM mistake_record WHERE id=?",
        (int(mistake_id),),
    ).fetchone()
    mistake = None
    kp_id = 0
    subject_code = None
    user_id = 1
    if m_row is not None:
        mistake = {
            "id": m_row[0],
            "subject": m_row[1],
            "diagnosis_type": m_row[2],
            "error_type": m_row[3],
            "reappear_count": m_row[4],
            "is_tamed": m_row[5],
            "kp_id": m_row[6],
        }
        kp_id = int(m_row[6] or 0)
        subject_code = m_row[1]
        user_id = int(m_row[7] or 1)

    target = _resolve_target_feedback(conn, mistake_id, feedback_id)
    if target is None:
        degraded.append(
            f"feedback #{feedback_id} 未找到" if feedback_id is not None else "该题暂无复习反馈"
        )

    history: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT rf.id, rf.self_rating, rf.note, rf.created_at"
            " FROM review_feedback rf"
            " JOIN review_schedule rs ON rs.id = rf.schedule_id"
            " WHERE rs.mistake_id = ?"
            " ORDER BY rf.created_at DESC, rf.id DESC LIMIT ?",
            (int(mistake_id), int(history_limit)),
        ).fetchall()
        # 取最近 N 条后翻转为时间升序（供 LLM 看趋势）
        history = [
            {"id": r[0], "self_rating": r[1], "note": (r[2] or "")[:120], "created_at": r[3]}
            for r in reversed(rows)
        ]
    except sqlite3.OperationalError as exc:  # noqa: BLE001
        degraded.append(f"历史反馈不可读：{exc}")

    attribution = None
    try:
        a_row = conn.execute(
            "SELECT proposed_code, proposed_new_code, confidence, rationale, status, created_at"
            " FROM attribution_proposal WHERE mistake_id=? ORDER BY id DESC LIMIT 1",
            (int(mistake_id),),
        ).fetchone()
        if a_row is not None:
            attribution = {
                "proposed_code": a_row[0],
                "proposed_new_code": a_row[1],
                "confidence": a_row[2],
                "rationale": (a_row[3] or "")[:240],
                "status": a_row[4],
                "created_at": a_row[5],
            }
    except sqlite3.OperationalError:  # noqa: BLE001
        degraded.append("attribution_proposal 表不可用")

    # kp_state 读取口径（2026-09-13 与 p7-kpstate 对齐）：
    # - 必须带 user_id（UNIQUE 是 (user_id, kp_id)，多用户下 id DESC 会取到别人的行）；
    # - 有行 != 已复习过：status='UNLEARNED' 表示冷启动先验（无作答证据），
    #   由 prompt 层如实呈现，不当作"无知识点状态"；
    # - R(t) 是每日 04:00 重建时刻的值，非请求时刻实时值。
    kp_state = None
    if kp_id > 0:
        try:
            k_row = conn.execute(
                "SELECT status, mastery_adj, stability_days, performance_score,"
                " retrievability, exposure_count, total_answer_count"
                " FROM kp_state WHERE user_id=? AND kp_id=? ORDER BY id DESC LIMIT 1",
                (user_id, kp_id),
            ).fetchone()
            if k_row is not None:
                kp_state = {
                    "status": k_row[0],
                    "m_adj": k_row[1],
                    "stability_days": k_row[2],
                    "performance_score": k_row[3],
                    "retrievability": k_row[4],
                    "exposure_count": k_row[5],
                    "total_answer_count": k_row[6],
                }
            else:
                degraded.append(f"kp_state 无 user_id={user_id} kp_id={kp_id} 记录")
        except sqlite3.OperationalError:  # noqa: BLE001
            degraded.append("kp_state 表不可用")

    try:
        open_iv = conn.execute(
            "SELECT COUNT(*) FROM intervention_action WHERE mistake_id=? AND status='PENDING'",
            (int(mistake_id),),
        ).fetchone()[0]
    except sqlite3.OperationalError:  # noqa: BLE001
        open_iv = 0
        degraded.append("intervention_action 表不可用")

    return {
        "mistake_id": int(mistake_id),
        "mistake": mistake,
        "subject_code": subject_code,
        "kp_id": kp_id,
        "feedback": target,
        "history": history,
        "attribution": attribution,
        "kp_state": kp_state,
        "open_interventions": int(open_iv),
        "degraded": degraded,
    }


# ── Prompt ───────────────────────────────────────────────────
def build_feedback_prompt(ev: dict) -> tuple[str, str]:
    """建 prompt（断点识别 + 个性化干预）。"""
    fam_txt = "\n".join(f"- {k} → {v}" for k, v in ACTION_FAMILY.items())

    system = (
        "你是小学数学错题本的个性化干预专家。请基于**这一道题**的复习反馈历史与既有归因，"
        "做「断点识别」并给出可执行的个性化干预。\n\n"
        "要求：\n"
        "1. 断点识别：判断学生这次卡在哪一环，root_diagnosis 只能是 "
        "CONCEPT（概念未建立）/ CALC（计算不熟练）/ MISREAD（读题漏信息）/ "
        "NORM（书写规范）之一；\n"
        "2. 结合历史评分趋势（1 陌生…4 熟练）与既有归因提案：判断与既有归因"
        "一致写 consistent，有新证据需要修正写 revised；\n"
        "3. 干预动作要**对齐**下列动作族，且必须**可执行**（写清做什么 + 频次/时长）：\n"
        f"{fam_txt}\n"
        "4. 知识点状态里的 R（可提取性）与 P（表现分）是两条正交线索："
        "R 低但 P 高 ≈ 会做但忘了（偏复习节奏）；R 高但 P 低 ≈ 记得但不会做（偏概念/计算）；\n"
        "5. 若 KP 状态标注「尚无作答证据」或为 UNLEARNED，那是冷启动先验值，"
        "不得当作真实表现，也不得据此下高置信结论；\n"
        "6. 证据不足（该题历史反馈少于 2 条）时，在 summary 里如实提示"
        "「证据有限，结论待积累」，并把 confidence 压到 0.6 以下；\n"
        "7. confidence 是你对本次断点判断的把握（0.0-1.0），不要虚高。\n\n"
        "只输出一个 JSON 对象，不要 markdown 代码块：\n"
        '{"breakpoint":"<一句话断点>","root_diagnosis":"CONCEPT|CALC|MISREAD|NORM",'
        '"diagnosis_alignment":"consistent|revised",'
        '"actions":[{"type":"RETEACH|SIMPLIFY|HINT|PARENT_NUDGE|DIAGNOSE",'
        '"instruction":"<做什么+频次/时长>","duration_min":15}],'
        '"confidence":0.0,"summary":"<两句话>"}'
    )

    fb = ev.get("feedback") or {}
    if fb:
        rating = fb.get("self_rating")
        dur = fb.get("duration_seconds")
        dur_txt = f"{dur} 秒" if dur is not None else "（未记录）"
        fb_txt = (
            f"- 时间：{fb.get('created_at')} · 评分：{rating}"
            f"（{_RATING_LABEL.get(rating, '?')}）\n"
            f"- 备注：{(fb.get('note') or '（无）')[:300]}\n"
            f"- 本次用时：{dur_txt}"
        )
    else:
        fb_txt = "（无本次反馈）"

    hist_lines = []
    for h in ev.get("history") or []:
        rating = h.get("self_rating")
        hist_lines.append(
            f"- {h.get('created_at')} · 评分 {rating}"
            f"（{_RATING_LABEL.get(rating, '?')}）"
            + (f" · {(h.get('note') or '')[:80]}" if h.get("note") else "")
        )
    hist_txt = "\n".join(hist_lines) or "（无历史反馈）"

    m = ev.get("mistake") or {}
    meta_txt = (
        (
            f"id={m.get('id')} · 学科={m.get('subject')} · 诊断类型={m.get('diagnosis_type')}"
            f" · error_type={m.get('error_type')} · 复现次数={m.get('reappear_count')}"
            f" · 已驯服={'是' if m.get('is_tamed') else '否'}"
        )
        if m
        else "（错题元数据缺失）"
    )

    attr = ev.get("attribution")
    if attr:
        attr_txt = (
            f"建议 code={attr.get('proposed_code') or attr.get('proposed_new_code')}"
            f" · 置信度={attr.get('confidence')} · 状态={attr.get('status')}"
            + (f"\n理由：{attr.get('rationale')}" if attr.get("rationale") else "")
        )
    else:
        attr_txt = "（暂无归因提案）"

    kps = ev.get("kp_state")
    if kps:
        # 「有 kp_state 行」不等于「已复习过」：UNLEARNED / 0 作答 = 冷启动先验，
        # 必须如实标注，避免 LLM 把先验值当真实表现。
        if kps.get("status") == "UNLEARNED" or not (kps.get("total_answer_count") or 0):
            kp_txt = (
                f"状态={kps.get('status')}（尚无作答证据，以下为先验值）"
                f" · 掌握度调整 m_adj={kps.get('m_adj')}"
                f" · 稳定度={kps.get('stability_days')} 天"
                f" · 作答次数={kps.get('total_answer_count') or 0}"
            )
        else:
            r_val = kps.get("retrievability")
            r_txt = f"{r_val:.2f}" if isinstance(r_val, (int, float)) else "—"
            kp_txt = (
                f"状态={kps.get('status')} · 掌握度调整 m_adj={kps.get('m_adj')}"
                f" · 稳定度={kps.get('stability_days')} 天"
                f" · 表现分 P={kps.get('performance_score')} · 可提取性 R={r_txt}"
                f"（04:00 锚点值，非实时）· 曝光 {kps.get('exposure_count')}"
                f" / 作答 {kps.get('total_answer_count')} 次"
            )
    else:
        kp_txt = "（该题未挂载知识点，无 KP 状态）"

    deg_txt = "；".join(ev.get("degraded") or []) or "无"

    user = (
        f"【本次反馈】\n{fb_txt}\n\n"
        f"【该题历史反馈（时间升序，共 {len(ev.get('history') or [])} 条）】\n{hist_txt}\n\n"
        f"【错题元数据】{meta_txt}\n"
        f"【既有归因提案】{attr_txt}\n"
        f"【知识点掌控状态】{kp_txt}\n"
        f"【该题未完成干预】{ev.get('open_interventions', 0)} 条\n"
        f"【证据缺失提示】{deg_txt}\n\n"
        "请做断点识别，并给出针对这一道题的个性化干预动作。"
    )
    return system, user


def _valid_payload(parsed) -> bool:
    """可用的 LLM 输出：JSON 对象且至少给出断点或动作。"""
    if not isinstance(parsed, dict):
        return False
    return bool(parsed.get("breakpoint") or parsed.get("actions"))


def _normalize_actions(parsed: dict) -> list[dict]:
    actions: list[dict] = []
    for a in parsed.get("actions") or []:
        if not isinstance(a, dict):
            continue
        at = str(a.get("type") or "DIAGNOSE").upper()
        if at not in _ACTION_TYPES:
            at = "DIAGNOSE"
        dur = a.get("duration_min")
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        actions.append(
            {
                "type": at,
                "instruction": str(a.get("instruction") or "")[:300],
                "duration_min": dur,
            }
        )
        if len(actions) >= 3:
            break
    return actions


# ── 主入口 ───────────────────────────────────────────────────
def analyze_feedback_loop(
    conn,
    *,
    mistake_id: int,
    feedback_id: int | None = None,
    client=None,
    write: bool = True,
    force: bool = False,
) -> dict | None:
    """单题反馈闭环分析：证据 → LLM 断点 → 干预动作 → 落库。

    LLM 不可达（L0）/ 重试后仍不可解析 / 无反馈 → None（不写库、不猜）。
    """
    target = _resolve_target_feedback(conn, mistake_id, feedback_id)
    if target is None:
        logger.info("无复习反馈可分析（mistake_id=%s feedback_id=%s）", mistake_id, feedback_id)
        return None
    fid = int(target["id"])

    if write and not force:
        existing = _existing_for_feedback(conn, fid)
        if existing is not None:
            return {
                "ok": True,
                "skipped": "duplicate",
                "intervention_id": existing,
                "feedback_id": fid,
                "mistake_id": int(mistake_id),
            }

    ev = collect_feedback_evidence(conn, mistake_id=mistake_id, feedback_id=fid)
    if ev["mistake"] is None:
        logger.warning("错题 id=%s 不存在，跳过反馈闭环分析", mistake_id)
        return None

    client = client or get_default_client()
    system, user = build_feedback_prompt(ev)

    parsed = None
    source = None
    max_attempts = 1 + CONFIRM_RETRY_MAX
    for attempt in range(1, max_attempts + 1):
        chain = run_chain(system, user, client=client, version_stamp=FEEDBACK_PROMPT_VERSION)
        if chain.source == "L0":
            logger.warning("LLM 不可达（L0），反馈闭环分析未执行 mistake_id=%s", mistake_id)
            return None
        source = chain.source
        parsed = parse_llm_json(chain.answer)
        if _valid_payload(parsed):
            break
        logger.warning("反馈闭环输出不可解析（第 %s/%s 次）", attempt, max_attempts)
        parsed = None

    if not _valid_payload(parsed):
        logger.warning(
            "反馈闭环分析失败：重试 %s 次后仍不可解析 mistake_id=%s",
            max_attempts,
            mistake_id,
        )
        return None

    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    root = str(parsed.get("root_diagnosis") or "").strip().upper()
    if root not in _BREAKPOINTS:
        root = "OTHER"

    alignment = str(parsed.get("diagnosis_alignment") or "").strip().lower()
    if alignment not in _ALIGNMENTS:
        alignment = "consistent"

    result = {
        "mistake_id": int(mistake_id),
        "feedback_id": fid,
        "breakpoint": str(parsed.get("breakpoint") or "")[:200],
        "root_diagnosis": root,
        "diagnosis_alignment": alignment,
        "actions": _normalize_actions(parsed),
        "confidence": confidence,
        "needs_review": confidence < GATE,
        "summary": str(parsed.get("summary") or "")[:400],
        "source_layer": source,
        "model_version": FEEDBACK_PROMPT_VERSION,
        "analyzed_at": fmt_ts(now_utc()),
    }
    if write:
        result["intervention_id"] = _persist(
            conn, result, subject_code=ev["subject_code"], kp_id=ev["kp_id"]
        )
    return result


def _existing_for_feedback(conn, feedback_id: int) -> int | None:
    """该反馈是否已有 FEEDBACK_LOOP 行（去重键 = payload_json.feedback_id）。"""
    try:
        row = conn.execute(
            "SELECT id FROM intervention_action"
            " WHERE trigger='FEEDBACK_LOOP'"
            "   AND CAST(json_extract(payload_json, '$.feedback_id') AS INTEGER) = ?"
            " ORDER BY id DESC LIMIT 1",
            (int(feedback_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        # 老库无 json1 扩展 → 退化为字符串匹配（json.dumps 默认分隔符）
        row = conn.execute(
            "SELECT id FROM intervention_action"
            " WHERE trigger='FEEDBACK_LOOP' AND payload_json LIKE ?"
            " ORDER BY id DESC LIMIT 1",
            (f'%"feedback_id": {int(feedback_id)}%',),
        ).fetchone()
    return int(row[0]) if row else None


def _persist(conn, result: dict, *, subject_code=None, kp_id: int = 0) -> int:
    """写 intervention_action（FEEDBACK_LOOP）。

    幂等：同题更早的 PENDING FEEDBACK_LOOP 先标 SKIPPED，只留最新一条（同 PARETO 语义）。
    """
    stamp = fmt_ts(now_utc())
    mid = int(result["mistake_id"])
    conn.execute(
        "UPDATE intervention_action SET status='SKIPPED', updated_at=?"
        " WHERE trigger='FEEDBACK_LOOP' AND status='PENDING' AND mistake_id=?",
        (stamp, mid),
    )
    subject_id = 1
    if subject_code:
        row = conn.execute(
            "SELECT id FROM subject WHERE code=? LIMIT 1", (subject_code,)
        ).fetchone()
        if row:
            subject_id = int(row[0])

    kp = int(kp_id or 0)
    actions = result.get("actions") or []
    action_type = actions[0]["type"] if actions else "DIAGNOSE"
    payload = {
        "feedback_id": int(result["feedback_id"]),
        "breakpoint": result["breakpoint"],
        "root_diagnosis": result["root_diagnosis"],
        "diagnosis_alignment": result["diagnosis_alignment"],
        "actions": actions,
        "confidence": result["confidence"],
        "needs_review": result["needs_review"],
        "summary": result["summary"],
        "source_layer": result["source_layer"],
        "model_version": result["model_version"],
        "analyzed_at": result["analyzed_at"],
    }
    cur = conn.execute(
        "INSERT INTO intervention_action"
        " (user_id, mistake_id, kp_id, subject_id, trigger, action_type,"
        "  payload_json, status, created_at, updated_at)"
        " VALUES (1, ?, ?, ?, 'FEEDBACK_LOOP', ?, ?, 'PENDING', ?, ?)",
        (
            mid,
            kp if kp > 0 else None,
            subject_id,
            action_type,
            json.dumps(payload, ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_for_mistake(conn, mistake_id: int) -> dict | None:
    """读该题最新一条 FEEDBACK_LOOP（payload 解析 + 行元数据）。"""
    row = conn.execute(
        "SELECT id, action_type, status, payload_json, created_at, updated_at"
        " FROM intervention_action"
        " WHERE trigger='FEEDBACK_LOOP' AND mistake_id=? ORDER BY id DESC LIMIT 1",
        (int(mistake_id),),
    ).fetchone()
    return _row_to_action(row, int(mistake_id))


def _row_to_action(row, mistake_id: int | None = None) -> dict | None:
    if not row:
        return None
    try:
        d = json.loads(row[3] or "{}")
    except (ValueError, TypeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d["intervention_id"] = int(row[0])
    d["action_type"] = row[1]
    d["status"] = row[2]
    d["created_at"] = row[4]
    d["updated_at"] = row[5]
    if mistake_id is not None:
        d["mistake_id"] = int(mistake_id)
    return d


def recent_actions(conn, *, limit: int = 10) -> list[dict]:
    """最近 N 条 FEEDBACK_LOOP（CLI --list / 排查用）。"""
    rows = conn.execute(
        "SELECT id, action_type, status, payload_json, created_at, updated_at, mistake_id"
        " FROM intervention_action"
        " WHERE trigger='FEEDBACK_LOOP' ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    out = []
    for r in rows:
        out.append(_row_to_action(r[:6], r[6]))
    return out


def mark_done(conn, intervention_id: int) -> dict:
    """幂等把一条干预动作标 DONE（CLI --done 与 HTTP 端点共用语义）。"""
    row = conn.execute(
        "SELECT status FROM intervention_action WHERE id=?", (int(intervention_id),)
    ).fetchone()
    if row is None:
        return {"ok": False, "not_found": True, "id": int(intervention_id)}
    if row[0] == "DONE":
        return {"ok": True, "already": True, "id": int(intervention_id)}
    conn.execute(
        "UPDATE intervention_action SET status='DONE', updated_at=? WHERE id=?",
        (fmt_ts(now_utc()), int(intervention_id)),
    )
    conn.commit()
    return {"ok": True, "id": int(intervention_id)}


# ── 回填 ─────────────────────────────────────────────────────
def backfill_feedback_loop(conn, *, limit: int = 50, client=None) -> dict:
    """回填历史反馈的闭环分析（无 FEEDBACK_LOOP 行的 review_feedback，id desc）。

    返回 {"processed", "written", "skipped", "failed"}。真跑 LLM，供 CLI 手动执行。
    """
    stats = {"processed": 0, "written": 0, "skipped": 0, "failed": 0}
    try:
        rows = conn.execute(
            "SELECT rf.id, rs.mistake_id FROM review_feedback rf"
            " LEFT JOIN review_schedule rs ON rs.id = rf.schedule_id"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM intervention_action ia"
            "   WHERE ia.trigger='FEEDBACK_LOOP'"
            "     AND CAST(json_extract(ia.payload_json, '$.feedback_id') AS INTEGER) = rf.id"
            " )"
            " ORDER BY rf.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT rf.id, rs.mistake_id FROM review_feedback rf"
            " LEFT JOIN review_schedule rs ON rs.id = rf.schedule_id"
            " ORDER BY rf.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

    for fid, mid in rows:
        stats["processed"] += 1
        if not mid:
            # 反馈无 schedule 归属（历史脏数据）→ 无法定位错题
            stats["failed"] += 1
            continue
        try:
            r = analyze_feedback_loop(
                conn,
                mistake_id=int(mid),
                feedback_id=int(fid),
                client=client,
                write=True,
            )
        except Exception:  # noqa: BLE001
            logger.warning("回填反馈闭环失败 feedback_id=%s", fid, exc_info=True)
            stats["failed"] += 1
            continue
        if r is None:
            stats["failed"] += 1
        elif r.get("skipped") == "duplicate":
            stats["skipped"] += 1
        else:
            stats["written"] += 1
    return stats


# ── 异步触发（复习主流程的挂钩入口）────────────────────────
# 与 attribution_engine.schedule_async_analysis 同款：独立线程 + 自开连接 +
# (db_path, mistake_id, feedback_id) 去重 + 延迟 1 秒避开主线程事务窗口。
_ASYNC_DELAY_SEC = 1.0
_INFLIGHT: set[tuple[str, int, int]] = set()
_INFLIGHT_LOCK = threading.Lock()


def schedule_async_feedback_loop(conn, *, mistake_id: int, feedback_id: int) -> dict:
    """后台线程触发反馈闭环分析。**绝不抛异常、绝不阻塞调用方。**"""
    try:
        db_path = _db_path_of(conn)
        if not db_path or db_path == ":memory:":
            return {
                "ok": False,
                "reason": "内存库不支持异步反馈闭环（请用同步 analyze_feedback_loop）",
            }
        key = (db_path, int(mistake_id), int(feedback_id))
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                return {"ok": False, "reason": "同任务已在跑", "duplicate": True}
            _INFLIGHT.add(key)

        def _work() -> None:
            try:
                _time.sleep(_ASYNC_DELAY_SEC)
                c = get_connection(db_path)
                try:
                    analyze_feedback_loop(
                        c,
                        mistake_id=int(mistake_id),
                        feedback_id=int(feedback_id),
                        client=get_default_client(),
                        write=True,
                    )
                finally:
                    c.close()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "异步反馈闭环失败（不影响复习主流程）mistake_id=%s",
                    mistake_id,
                    exc_info=True,
                )
            finally:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(key)

        threading.Thread(target=_work, name=f"fdl-fbloop-{mistake_id}", daemon=True).start()
        return {"ok": True, "async": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("反馈闭环调度失败（不影响复习主流程）：%s", exc)
        return {"ok": False, "reason": str(exc)}

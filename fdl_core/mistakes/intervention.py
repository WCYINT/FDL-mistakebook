"""干预动作 · LLM Pareto 根因分析与措施生成（2026-09-12 King 需求）。

背景与定位
----------
驾驶舱「错因诊断」模块此前展示的是**静态映射**（`_DIAGNOSIS_ACTIONS`：
4 类错因 → 固定一句话动作）。King 要求升级为：

    「由 LLM 基于已有分析与反馈，采用 Pareto（帕累托）原则分析主要根因，
      并针对根因主动识别干预措施。」

本模块就是把这句话落地：**数据 → Pareto 根因 → 干预措施 → 落库 → 报告展示**。

计算逻辑（四步）
----------------
1. **采集证据**（读库，不猜）
   - 错因分布：`mistake_record.diagnosis_type` × 计数 × 已驯服数
   - 复习反馈：`review_feedback` 近 30 条 `self_rating` 分布（1 陌生…4 熟练）
   - 挂载信息：各错因组的 `kp_id` 挂载数（判断"是否知识点盲区"）
2. **Pareto 排序**（LLM 判读，非机械计算）
   错因按"问题贡献度"降序 → 累计占比达到 **约 80%** 的前若干项 = **主要根因
   （Vital Few）**；其余为 Trivial Many。LLM 需结合反馈解释"为什么这是根因"
   （如：CALC 占比高 + self_rating 长期 ≤2 → 计算自动化未形成）。
3. **措施生成**（针对根因，非泛泛而谈）
   每个主要根因给 2-3 条**可执行**干预（含时长/频次建议），并对齐错因→干预
   动作族（CONCEPT→回定义复述 / CALC→限时竖式 / MISREAD→圈画关键信息 /
   NORM→模板化书写）。
4. **落库与展示**
   - 写 `intervention_action`（trigger='PARETO_ROOT'，action_type=DIAGNOSE，
     payload_json 存完整分析）——进入"干预动作待办"链路；
   - 报告读最新一条 → 驾驶舱「错因诊断」下方展示根因排序 + 措施。

闸门与安全
----------
- LLM 不可达（L0）→ 返回 None，**不写库、不猜**（报告回退静态映射展示）；
- 由 `scripts/fdl_intervention.py` 手动触发或挂批处理；**不在请求路径里跑**。
"""

from __future__ import annotations

import json
import logging

from fdl_core.l2.fallback import run_chain
from fdl_core.mistakes.attribution_engine import get_default_client, parse_llm_json
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.mistakes.intervention")

PROMPT_VERSION = "pareto-intervention-v1"

# 错因 → 干预动作族（与静态映射一致，作为 LLM 的锚点，避免自由发挥）
ACTION_FAMILY = {
    "CONCEPT": "回到定义，用自己的话复述；不抢限时训练",
    "CALC": "限时竖式专项（25 min 段）；先放慢到不出错再提速",
    "MISREAD": "圈画关键信息（数字/单位/方向）；再读题再下笔",
    "NORM": "模板化书写要求；订正写几遍直到正确成型",
    "OTHER": "按证据细化（暂用通用复盘）",
}

PARETO_THRESHOLD = 0.80  # 累计占比阈值（Vital Few 界定）


def collect_evidence(conn, *, feedback_limit: int = 30) -> dict:
    """第一步：采集证据（只读）。"""
    dist = []
    for diag, total, tamed, linked in conn.execute(
        "SELECT diagnosis_type, COUNT(*), COALESCE(SUM(is_tamed=1),0),"
        " COALESCE(SUM(CASE WHEN kp_id!=0 THEN 1 ELSE 0 END),0)"
        " FROM mistake_record GROUP BY diagnosis_type"
        " ORDER BY COUNT(*) DESC"
    ):
        dist.append(
            {"diagnosis": diag or "OTHER", "count": total, "tamed": tamed, "linked": linked}
        )
    total_mis = sum(d["count"] for d in dist) or 1
    for d in dist:
        d["share"] = round(d["count"] / total_mis, 3)

    fb_rows = conn.execute(
        "SELECT self_rating, COUNT(*) FROM review_feedback"
        " GROUP BY self_rating ORDER BY self_rating"
    ).fetchall()
    feedback = {int(r[0]): r[1] for r in fb_rows if r[0] is not None}

    recent = conn.execute(
        "SELECT self_rating FROM review_feedback ORDER BY id DESC LIMIT ?", (feedback_limit,)
    ).fetchall()
    recent_ratings = [int(r[0]) for r in recent if r[0] is not None]

    iv_open = conn.execute(
        "SELECT COUNT(*) FROM intervention_action WHERE status='PENDING'"
    ).fetchone()[0]

    return {
        "diagnosis_dist": dist,
        "mistake_total": total_mis,
        "feedback_counts": feedback,
        "recent_ratings": recent_ratings,
        "open_interventions": iv_open,
    }


# ── Prompt ───────────────────────────────────────────────────
def build_prompts(ev: dict) -> tuple[str, str]:
    """建 prompt（Pareto 原则 + 措施生成指令）。"""
    lines = []
    for d in ev["diagnosis_dist"]:
        lines.append(
            f"- {d['diagnosis']}：{d['count']} 题（占比 {d['share'] * 100:.1f}%，"
            f"已驯服 {d['tamed']}，已挂知识点 {d['linked']}）"
        )
    dist_txt = "\n".join(lines) or "（无错题数据）"
    fb_txt = "、".join(f"{k}分×{v}" for k, v in sorted(ev["feedback_counts"].items())) or "（暂无）"
    fam_txt = "\n".join(f"- {k} → {v}" for k, v in ACTION_FAMILY.items())

    system = (
        "你是小学数学学习系统的诊断专家。请按 **Pareto（帕累托）原则**分析"
        "学生的错因结构，识别**主要根因（Vital Few：贡献约 80% 问题的那几项）**，"
        "并针对每个主要根因主动给出可执行的干预措施。\n\n"
        "分析方法要求：\n"
        "1. 按问题贡献度（占比）降序，累计占比达到约 80% 的前若干项 = 主要根因；\n"
        "2. 每个根因要引用**证据**（占比、驯服数、挂载数、反馈评分），不能空说；\n"
        "3. 干预措施要**可执行**（含做什么、做多久/多频繁），并倾向对齐下列动作族：\n"
        f"{fam_txt}\n"
        "4. 若总样本量很小（<10），要在 summary 里如实提示「证据有限，结论待积累」。\n\n"
        "只输出一个 JSON 对象，不要 markdown 代码块：\n"
        '{"roots":[{"diagnosis":"<错因码>","share":0.42,"cumulative":0.42,'
        '"evidence":"<一句话证据>","actions":["<措施1>","<措施2>"],"priority":1}],'
        '"vital_few_count":2,"summary":"<两句话总括>"}'
    )
    user = (
        f"【错因分布（含占比/驯服/挂载）】\n{dist_txt}\n\n"
        f"【复习反馈评分分布（1陌生…4熟练）】{fb_txt}\n"
        f"【近 {len(ev['recent_ratings'])} 次复习评分序列】{ev['recent_ratings']}\n"
        f"【未完成干预动作】{ev['open_interventions']} 条\n\n"
        "请做 Pareto 根因分析并给出干预措施。"
    )
    return system, user


def pareto_intervention_analysis(
    conn,
    *,
    client=None,
    write: bool = True,
) -> dict | None:
    """主入口：Pareto 分析 → 措施 → 落库。LLM 不可达返回 None（不猜）。"""
    ev = collect_evidence(conn)
    if not ev["diagnosis_dist"]:
        logger.info("无错题数据，跳过 Pareto 分析")
        return None

    client = client or get_default_client()
    system, user = build_prompts(ev)
    chain = run_chain(system, user, client=client, version_stamp=PROMPT_VERSION)
    if chain.source == "L0":
        logger.warning("LLM 不可达（L0），Pareto 分析未执行")
        return None
    parsed = parse_llm_json(chain.answer)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("roots"), list):
        logger.warning("Pareto 分析输出不可解析：%s", str(chain.answer)[:160])
        return None

    roots = []
    for r in parsed["roots"]:
        if not isinstance(r, dict):
            continue
        roots.append(
            {
                "diagnosis": str(r.get("diagnosis") or "OTHER"),
                "share": float(r.get("share") or 0.0),
                "cumulative": float(r.get("cumulative") or 0.0),
                "evidence": str(r.get("evidence") or "")[:300],
                "actions": [str(a)[:200] for a in (r.get("actions") or [])][:3],
                "priority": int(r.get("priority") or len(roots) + 1),
            }
        )
    if not roots:
        return None

    result = {
        "roots": sorted(roots, key=lambda x: x["priority"]),
        "vital_few_count": int(parsed.get("vital_few_count") or len(roots)),
        "summary": str(parsed.get("summary") or "")[:400],
        "evidence_snapshot": {
            "mistake_total": ev["mistake_total"],
            "feedback_total": sum(ev["feedback_counts"].values()),
        },
        "source_layer": chain.source,
        "model_version": PROMPT_VERSION,
        "analyzed_at": fmt_ts(now_utc()),
    }

    if write:
        _persist(conn, result)
    return result


def _persist(conn, result: dict) -> int:
    """写 intervention_action（PARETO_ROOT）：一条聚合记录（mistake_id NULL）。

    幂等策略：同一分析不重复堆叠——把旧的 PARETO_ROOT 标 SKIPPED，
    只保留最新一条 PENDING（报告只读最新）。
    """
    stamp = fmt_ts(now_utc())
    conn.execute(
        "UPDATE intervention_action SET status='SKIPPED', updated_at=?"
        " WHERE trigger='PARETO_ROOT' AND status='PENDING'",
        (stamp,),
    )
    # subject_id：用第一个学科（PARETO_ROOT 是全局分析，不绑定单科）
    row = conn.execute("SELECT id FROM subject ORDER BY sort_order LIMIT 1").fetchone()
    subject_id = row[0] if row else 1
    cur = conn.execute(
        "INSERT INTO intervention_action"
        " (user_id, mistake_id, kp_id, subject_id, trigger, action_type,"
        "  payload_json, status, created_at, updated_at)"
        " VALUES (1, NULL, NULL, ?, 'PARETO_ROOT', 'DIAGNOSE', ?, 'PENDING', ?, ?)",
        (subject_id, json.dumps(result, ensure_ascii=False), stamp, stamp),
    )
    conn.commit()
    result["intervention_id"] = int(cur.lastrowid)
    return int(cur.lastrowid)


def latest_analysis(conn) -> dict | None:
    """读最新一条 PARETO_ROOT 分析（报告用；无则 None）。"""
    row = conn.execute(
        "SELECT payload_json, created_at FROM intervention_action"
        " WHERE trigger='PARETO_ROOT' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        d = json.loads(row[0] or "{}")
    except (ValueError, TypeError):
        return None
    d["created_at"] = row[1]
    return d

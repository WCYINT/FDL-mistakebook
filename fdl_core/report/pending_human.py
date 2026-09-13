"""「待人工处理」统一注册表与聚合（2026-09-12 King 规则）。

规则（King 2026-09-12）
----------------------
**所有「待人工处理」的问题，必须分别呈现在驾驶舱页面或今日复习页面中。**
不允许任何待人工项只存在于数据库/队列文件里而界面看不到。

落地方式（本模块 = 规则的唯一事实源）
--------------------------------------
`PENDING_SOURCES` 是全部待人工来源的**登记表**：每个来源声明
① 数据表/文件 ② 判定条件 ③ 呈现页面（dashboard 驾驶舱 / review 今日复习）
④ 前端块 id ⑤ 操作方式。
`collect_pending_human()` 按表聚合 → 注入报告 payload → 前端按 page 渲染。
新增任何待人工来源时，必须在此登记（否则回归测试会失败，见
`tests/test_pending_human.py::test_all_sources_have_render_slot`）。

四类来源
--------
    kp_mount_proposal     KP 挂载提案待复核   → 今日复习（可逐条 同意/驳回）
    kp_new_candidate      新知识点候选待确认   → 今日复习（同意/驳回 + LLM 二审）
    attribution_proposal  错因归因提案待复核   → 今日复习（展示；复核走 CLI）
    image_needs_review    图片无法自动识别     → 驾驶舱（缩略图 + 原因）
    intervention_pending  干预动作待办        → 驾驶舱（按 trigger 分组）
"""

from __future__ import annotations

import json

from fdl_core.paths import get_paths

# ── 登记表（规则的唯一事实源；新增来源必须在此登记）─────────────
PENDING_SOURCES: list[dict] = [
    {
        "key": "kp_mount_proposal",
        "label": "知识点挂载待复核",
        "kind": "db",
        "table": "kp_match_proposal",
        "where": "status='PROPOSED'",
        "page": "review",  # 今日复习
        "block_id": "kp-proposals-block",
        "action": "逐条同意（写权威挂载）/ 驳回",
        "hint": "同意后该错题挂载到建议的知识点，挂载率随即上升（A2 升级条件之一）",
    },
    {
        "key": "kp_new_candidate",
        "label": "新知识点候选待确认",
        "kind": "db",
        "table": "kp_candidate",
        "where": "status='PENDING'",
        "page": "review",
        "block_id": "kp-candidates-block",
        "action": "同意晋级 / 驳回（需填原因，LLM 二审）",
        "hint": "系统从错题中自动提炼；同意后进入知识点库，可被后续匹配",
    },
    {
        "key": "attribution_proposal",
        "label": "错因归因待复核",
        "kind": "db",
        "table": "attribution_proposal",
        "where": "status='PROPOSED'",
        "page": "review",
        "block_id": "attr-proposals-block",
        "action": "展示提案（复核走 attribution 工具）",
        "hint": "LLM 对复习反馈的错因再判断结果",
    },
    {
        "key": "image_needs_review",
        "label": "图片待人工确认",
        "kind": "queue",
        "path": "data/review_queue.json",
        "where": "status='pending'",
        "page": "dashboard",
        "block_id": "needs-review-block",
        "action": "看原图人工判断",
        "hint": "OCR/VLM 无法自动识别或置信不足的错题照片",
    },
    {
        "key": "intervention_pending",
        "label": "干预动作待办",
        "kind": "db",
        "table": "intervention_action",
        "where": "status='PENDING'",
        "page": "dashboard",
        "block_id": "feedback-kpi-block",
        "action": "按 trigger 分组执行（重教/简化/提示/家长提醒）",
        "hint": "由首次答错/逾期/低信心等触发",
    },
]


def _count_db(conn, src: dict) -> tuple[int, list[dict]]:
    """DB 类来源：计数 + 前若干条样本。"""
    table, where = src["table"], src["where"]
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    except Exception:  # noqa: BLE001 — 表缺失不算错（阶段未到）
        return 0, []
    samples: list[dict] = []
    if n:
        # 各表字段不同——用宽松列选择，取通用摘要字段
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT 5"
            ).fetchall()
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 1").description]
            for r in rows:
                d = dict(zip(cols, r, strict=False))
                samples.append(
                    {
                        "id": d.get("id"),
                        "summary": (
                            str(
                                d.get("proposed_kp_code")
                                or d.get("candidate_name")
                                or d.get("rationale")
                                or d.get("action_type")
                                or ""
                            )
                        )[:60],
                        "confidence": d.get("confidence"),
                        "mistake_id": d.get("mistake_id"),
                    }
                )
        except Exception:  # noqa: BLE001
            pass
    return n, samples


def _count_queue(src: dict) -> tuple[int, list[dict]]:
    """队列文件类来源（review_queue.json）：计数 + 样本。"""
    p = get_paths().fdl_root / src["path"]
    if not p.exists():
        return 0, []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, []
    pend = [x for x in items if x.get("status") == "pending"]
    samples = [
        {
            "id": i,
            "summary": str(x.get("src") or "")[:60],
            "reason": str(x.get("reason") or "")[:50],
        }
        for i, x in enumerate(pend[:5])
    ]
    return len(pend), samples


def collect_pending_human(conn) -> dict:
    """聚合全部待人工来源。

    返回：
      {
        "total": int,
        "dashboard": [ {key,label,count,samples,block_id,action,hint}, ... ],  # 驾驶舱呈现
        "review":    [ ... ],                                                  # 今日复习呈现
        "all":       [ ... ],                                                  # 全量（审计用）
      }
    规则保证：PENDING_SOURCES 中每一项必然出现在 dashboard 或 review 之一。
    """
    out: dict = {"total": 0, "dashboard": [], "review": [], "all": []}
    for src in PENDING_SOURCES:
        if src["kind"] == "db":
            n, samples = _count_db(conn, src)
        else:
            n, samples = _count_queue(src)
        entry = {
            "key": src["key"],
            "label": src["label"],
            "count": n,
            "samples": samples,
            "page": src["page"],
            "block_id": src["block_id"],
            "action": src["action"],
            "hint": src["hint"],
        }
        out["all"].append(entry)
        out["total"] += n
        (out["dashboard"] if src["page"] == "dashboard" else out["review"]).append(entry)
    return out

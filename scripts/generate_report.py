"""阶段四 V1：报告数据聚合 + HTML 渲染（VIS-08 单文件，file:// 离线 + 零外链）。

管线：读 data/fdl.db → collect_metrics（11 项驾驶舱 + 红线 + 星图 + 图鉴）
→ 注入 fdl_core/report/template.html → 输出 site/report.html
（并自动同步 site/report_latest.html 别名，两者永远同一内容）。

口径（PRD §1.4 / MT-06/07）：
- 样本不足显示「—」（不做推测）；质量诊断在上线前 2 周对家长视图隐藏；
- 可视化只读预聚合口径；King 视角数据延迟 7 天。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path

from fdl_core.ops.ssd_check import check_ssd
from fdl_core.pvp.trends import king_view_cutoff
from fdl_core.srs.display_filter import filter_review_today_capped, filter_review_today_items
from fdl_core.srs.time_layer import LOCAL_TZ, fmt_ts, local_date, now_utc, parse_ts, to_utc

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "fdl_core" / "report" / "template.html"
OUT_DIR = ROOT / "site"

# ── 间隔驱动调度器（同事 p1-scheduler 并行实现 fdl_core.srs.queue_scheduler）──
# 降级保护：该模块可能尚未提交（并行开发）。未就绪时整体跳过优先级注入，
# 报告照常生成，行为与历史版本完全一致，绝不因此让报告生成失败。
try:
    from fdl_core.srs import queue_scheduler  # noqa: F401

    _HAS_SCHEDULER = True
except Exception:
    _HAS_SCHEDULER = False

logger = logging.getLogger(__name__)


def _na(name: str, target: str) -> dict:
    return {"name": name, "display": "—", "target": target, "status": "na"}


def _detail(
    source: str, query: str, rows: list | None = None, note: str = "", links: list | None = None
) -> dict:
    """VIS-09 数字详情：数据来源 + 脱敏明细 + 口径说明（+ 可选操作链接）。

    🔴 rows 脱敏：仅允许白名单列；wrong_answer/correct_answer/user_answer
    等敏感内容不出现在报告页（PVP 权限一致性，测试锁定）。

    links（2026-09-13 King #6）：详情内的问题编号超链接——
    [{label: "#77016", jump: 77016}] → 前端渲染为可点击按钮，跳转到
    「今日复习」页对应照片卡片。仅承载"编号"这类非敏感标识。
    """
    safe_rows = []
    allowed = {
        "id",
        "date",
        "subject",
        "kp",
        "name",
        "error_type",
        "source_ref",
        "minutes",
        "stars",
        "questions",
        "count",
        "tamed",
        "status",
        "sir",
        "ids",
        "window",
        "session_min",
        "asr_min",
        "avg_min",
        "self_n",
        "sess_n",
        "value",
        "target",
        "asr_files",
        "mistake_ids",
    }
    for r in rows or []:
        safe_rows.append({k: v for k, v in r.items() if k in allowed})
    out = {"source": source, "query": query, "rows": safe_rows, "note": note}
    if links:
        out["links"] = [x for x in links if isinstance(x, dict) and "label" in x]
    return out


def _status_of(value: float, ok: tuple[float, float]) -> str:
    return "ok" if ok[0] <= value <= ok[1] else "warn"


def _to_local_date(utc_iso: str) -> str:
    """UTC 存库时间 → 本地（Asia/Shanghai）日期 YYYY-MM-DD。

    用于 occurred_at / last_reappear_at 等 UTC 字段的展示转换，避免直接
    `s[:10]` 切片当本地日期（跨 UTC 边界会错一天）。空 / 解析异常降级保留
    UTC 切片（与历史 VIS-10 实现一致）。
    """
    if not utc_iso:
        return ""
    from datetime import datetime

    try:
        dt_utc = to_utc(datetime.fromisoformat(utc_iso.replace("Z", "+00:00")))
        return dt_utc.astimezone(LOCAL_TZ).date().isoformat()
    except Exception:
        return utc_iso[:10]  # 降级：保留 UTC 切片


def _load_review_audio_analyses() -> list[dict]:
    """加载今日复习语音的 ASR 草稿 + minimax 结构化分析。

    数据来源：
    - `data/asr_drafts/<MMDD>复习*.draft.json`：SenseVoice 本地转写
    - `data/asr_drafts/analyses/<同名>.analysis.json`：minimax-M3 提取的
      {subject, kps[], errors[], mastery{3 维}, self_evaluation, keywords}

    🔴 2026-09-12 修复：原 glob 硬编码 `0907复习*`，只认 9 月 7 日那一批录音——
    新录音（如 0912复习1）即使转写落盘也永远不会被报告读到。
    改为通配所有草稿（`*.draft.json`；drafts 目录本身不递归，analyses/ 子目录不会被扫到）。

    返回：list[dict]，每项 = {audio, duration_sec, char_count, text_raw, analysis}
    失败时容错：跳过异常文件，不影响主流程。
    """
    from pathlib import Path

    drafts_dir = Path(__file__).resolve().parent.parent / "data" / "asr_drafts"
    analyses_dir = drafts_dir / "analyses"
    if not drafts_dir.exists():
        return []
    items: list[dict] = []
    for df in sorted(drafts_dir.glob("*.draft.json")):
        try:
            draft = json.loads(df.read_text(encoding="utf-8"))
            af = analyses_dir / f"{df.stem}.analysis.json"
            if af.exists():
                analysis = json.loads(af.read_text(encoding="utf-8"))["analysis"]
            else:
                analysis = None
            items.append(
                {
                    "audio": draft.get("audio", df.stem),
                    "audio_path": draft.get("audio_path", ""),
                    "duration_sec": draft.get("duration_sec", 0),
                    "char_count": len(draft.get("text_raw", "")),
                    "text_raw": draft.get("text_raw", ""),
                    "engine": draft.get("engine", "SenseVoice-Small"),
                    "analysis": analysis,
                    # 归日所需：草稿文件名（MMDD 前缀）+ created_at（兜底）
                    "draft_name": df.name,
                    "created_at": draft.get("created_at"),
                }
            )
        except Exception:
            continue
    return items


# ── 语音→错题匹配（2026-09-09 King：录音应挂到原错题，不再单独显示框）──
# 持久化人工指定映射：data/asr_drafts/match_map.json {audio_name: mistake_id}
# 内置规则：按转写关键词给"建议匹配"（人工在 UI 上确认或改选，确认后写 match_map）
_AUDIO_MATCH_RULES = [
    {"keywords": ("轴对称", "正方形", "对称轴", "对称"), "mistake_id": 77004},
    {"keywords": ("雨伞", "正字", "统计", "天气"), "mistake_id": 77012},
    {"keywords": ("钢笔", "文具店", "卖出", "销量"), "mistake_id": 77002},
    {"keywords": ("1万", "一万", "数一数", "三个零", "写作"), "mistake_id": 77001},
]


def _load_audio_match_map() -> dict:
    """人工指定的录音→错题映射（UI 确认后由 fdl_serve 写入）。"""
    p = Path(__file__).resolve().parent.parent / "data" / "asr_drafts" / "match_map.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _suggest_audio_match(text_raw: str) -> int | None:
    """按转写关键词推断建议错题（仅当人工映射未指定时用作建议）。"""
    for rule in _AUDIO_MATCH_RULES:
        if any(kw in text_raw for kw in rule["keywords"]):
            return rule["mistake_id"]
    return None


def collect_metrics(conn: sqlite3.Connection, *, report_date: str | None = None) -> dict:
    """聚合 11 项驾驶舱 + 红线 + 星图 + 图鉴数据（当前数据稀疏期大量为「—」）。"""
    # 状态自动判定（错题按 DB 字段自动分类，无需人工告知）
    from fdl_core.mistakes.review import classify_all

    classified = classify_all(conn)
    d = report_date or local_date().isoformat()

    # 加载 ASR 录音草稿（含今日复习分钟数计算、错题自动联动）
    audio_analyses_draft = _load_review_audio_analyses()

    # 预聚合 ASR 录音按"草稿 MMDD 前缀日期"归日（与 daily_metric 同一权威口径，
    # 见 fdl_core.srs.asr_date.draft_review_date）。mtime 会被复制/重命名改变，
    # 故不再用 mtime 归日，统一到文件名业务日期。
    import datetime as _dt

    from fdl_core.srs.asr_date import draft_review_date

    asr_by_date = {}
    for a in audio_analyses_draft:
        _d = draft_review_date(a, a.get("draft_name", ""))
        iso = _d.isoformat() if _d else None
        if iso:
            asr_by_date[iso] = asr_by_date.get(iso, 0) + a.get("duration_sec", 0) / 60.0

    # P8：习惯指标需过滤 is_valid=1（与 trend 区块一致），
    #     且 COUNT/SUM/AVG 不再含无效会话
    sessions = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(trigger_type='SELF'),0),"
        " COALESCE(AVG(effective_sec)/60.0,0)"
        " FROM study_session WHERE session_date <= ? AND is_valid=1",
        (d,),
    ).fetchone()
    sess_n, self_n, avg_min = sessions
    # P2：WAD 周活跃天数 = 近 7 天（[d-6, d] 闭区间，本地日期）窗口内 DISTINCT 学习日
    _d0 = _dt.date.fromisoformat(d)
    _week_start = (_d0 - _dt.timedelta(days=6)).isoformat()
    wad_all = conn.execute(
        "SELECT COUNT(DISTINCT session_date) FROM study_session"
        " WHERE session_date >= ? AND session_date <= ? AND is_valid=1",
        (_week_start, d),
    ).fetchone()[0]

    # 进行中会话（ended_at IS NULL）实时分钟：started_at → 当前，归到本地日 d。
    # P9：不再用 substr(started_at,1,10)=d 的 UTC 切片，session_date 已是本地日期。
    in_progress_minutes_today = 0.0
    for r in conn.execute(
        "SELECT started_at FROM study_session"
        " WHERE session_date=? AND is_valid=1 AND ended_at IS NULL",
        (d,),
    ).fetchall():
        try:
            started_dt = _dt.datetime.fromisoformat(r[0].replace("Z", "+00:00"))
            elapsed = (_dt.datetime.now(_dt.UTC) - started_dt).total_seconds()
            if elapsed > 0:
                in_progress_minutes_today += elapsed / 60.0
        except Exception:
            pass

    # 今日复习分钟数（含 study_session 实际分钟 + 进行中实时分钟 + ASR 录音时长——录音是 Frank 当前唯一的复习方式）
    # P7：漏计进行中会话 + ASR 须按当日过滤（asr_by_date.get(d)）
    today_session_min = conn.execute(
        "SELECT COALESCE(SUM(effective_sec)/60.0,0) FROM study_session"
        " WHERE session_date=? AND is_valid=1",
        (d,),
    ).fetchone()[0]
    today_session_min = round(today_session_min + in_progress_minutes_today, 1)
    today_asr_min = asr_by_date.get(d, 0.0)
    today_review_min = round(today_session_min + today_asr_min, 1)
    today_review_target = 7.0  # 每日复习分钟目标（PRD §6.5）

    # 状态自动判定（错题按 DB 字段自动分类，无需人工告知）
    from fdl_core.mistakes.review import classify_all

    classified = classify_all(conn)

    # P16：主动放弃次数 = 近 7 天窗口（[d-6, d] 闭区间，本地日期），与"≤2/周"语义一致；
    #     原终身累计 task_date<=d 会随运行天数无限增长，与实际"每周"红线口径不符。
    tasks_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status='SKIPPED'),0) FROM daily_task"
        " WHERE task_date >= ? AND task_date <= ?",
        (_week_start, d),
    ).fetchone()
    backlog = conn.execute(
        "SELECT COUNT(*) FROM review_schedule WHERE status='PENDING' AND due_date <= ?",
        (d,),
    ).fetchone()[0]
    mistakes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(error_type IS NOT NULL),0),"
        " COALESCE(SUM(is_tamed=0),0) FROM mistake_record"
    ).fetchone()
    # P3：周新知识点录入 = 本 ISO 周（周一 00:00 至今）新增，而非全年累计。
    # valid_from 是 TEXT 日期串，substr(...,1,10) 取日期部分做字符串比较（仅取日期，非跨时区比较）。
    _d0 = _dt.date.fromisoformat(d)
    _monday = (_d0 - _dt.timedelta(days=_d0.weekday())).isoformat()
    new_kp_week = conn.execute(
        "SELECT COUNT(*) FROM knowledge_point WHERE substr(valid_from,1,10) >= ?",
        (_monday,),
    ).fetchone()[0]

    cockpit: list[dict] = []
    # 管道健康
    cockpit.append(
        {
            "name": "周新知识点录入",
            "display": str(new_kp_week),
            "target": "4–8",
            "status": _status_of(new_kp_week, (4, 8)),
            "detail": _detail(
                "knowledge_point.valid_from（本 ISO 周，周一至今）",
                f"SELECT COUNT(*) FROM knowledge_point"
                f" WHERE substr(valid_from,1,10) >= '{_monday}'",
                [{"count": new_kp_week, "date": d}],
                note="输入管道通不通：太少没建起来，太多说明切分过细（PRD §1.4）",
            ),
        }
    )
    coverage = round(mistakes[1] / mistakes[0], 2) if mistakes[0] else None
    vis11_rows = [
        {"id": r[0], "date": _to_local_date(r[1]), "error_type": r[2] or "未归因"}
        for r in conn.execute("SELECT id, occurred_at, error_type FROM mistake_record ORDER BY id")
    ]
    coverage_detail = _detail(
        "mistake_record（已归因 / 全部）",
        "SELECT COUNT(*), SUM(error_type IS NOT NULL) FROM mistake_record",
        vis11_rows,
        note="覆盖率 = 已归因错题 / 全部错题；≥90% 健康——挂不上知识点的错题是废数据（PRD §1.4）",
    )
    cockpit.append(
        {
            "name": "归因覆盖率",
            "display": f"{coverage:.0%}" if coverage is not None else "—",
            "target": "≥90%",
            "status": "ok" if coverage is not None and coverage >= 0.9 else "warn",
            "detail": coverage_detail,
        }
        if coverage is not None
        else {**_na("归因覆盖率", "≥90%"), "detail": coverage_detail}
    )
    # 复习队列积压——按 display_filter 显示判定（仅显示记忆曲线节点 + 今日未解决/未复习）
    #   过滤规则：status∈{待复习, 今日已复习, 已复习} 且 due_date<=today AND 不含已解决
    from fdl_core.srs.display_filter import should_show_in_review_today

    pending_rows = conn.execute(
        "SELECT m.id, m.kp_id, m.resolved_at, m.last_reappear_at, "
        "       COALESCE(r.status, '') AS sched_status, "
        "       COALESCE(r.due_date, '') AS due_date "
        " FROM mistake_record m "
        " LEFT JOIN review_schedule r ON r.mistake_id = m.id "  # A3：按 mistake_id 关联
        "   AND r.status = 'PENDING' "
        "WHERE m.is_tamed = 0"
    ).fetchall()
    # P13：状态判定直接复用 classify_all 的结果（内部已做 UTC→本地日期转换），
    #       不再用 lr_at[:10]==d 的 UTC 切片（跨 UTC 边界会误判"今日已复习"）。
    _status_by_id = {r["id"]: r["status"] for r in classified["items"]}
    review_today_backlog = 0
    review_today_ids: list[int] = []  # 2026-09-13 King #6：详情内列出问题编号（超链接跳卡片）
    for mid, kp_id, res_at, lr_at, _sched_st, _due in pending_rows:
        # 用 classify_status 同口径推 status
        if res_at:
            continue
        cls_status = _status_by_id.get(mid, "待复习")
        d_show = should_show_in_review_today(
            conn,
            kp_id=kp_id or 0,
            mistake_id=mid,
            today=d,
            resolved_at=res_at,
            last_reappear_at=lr_at,
            status=cls_status,
        )
        if d_show.should_show:
            review_today_backlog += 1
            review_today_ids.append(int(mid))
    cockpit.append(
        {
            "name": "复习队列积压",
            "display": str(review_today_backlog),
            "target": "≤20",
            "status": _status_of(review_today_backlog, (0, 20)),
            "detail": _detail(
                "review_schedule（PENDING 且 due_date<=today）+ display_filter",
                "SELECT COUNT(*) FROM review_schedule WHERE status='PENDING' AND due_date<=今天",
                [
                    {
                        "count": review_today_backlog,
                        "date": d,
                        "ids": review_today_ids[:12],
                        "note": "仅记忆曲线节点 + 未解决/未复习",
                    }
                ],
                note="调度正常与否；≤20 为健康区间（PRD §1.4）。下方编号可点击跳转到对应照片卡片。",
                # 问题编号超链接（King #6）：最多 8 个（过多按钮反而碍眼）
                links=[{"label": "#" + str(i), "jump": i} for i in review_today_ids[:8]],
            ),
        }
    )
    # 习惯（样本不足 → —；2026-09-13 King #6：全部卡片补详情，支持点击看口径）
    cockpit.append(
        {
            **(
                _na("WAD 周活跃天数", "≥5")
                if sess_n == 0
                else {
                    "name": "WAD 周活跃天数",
                    "display": str(wad_all),
                    "target": "≥5",
                    "status": _status_of(wad_all, (5, 7)),
                }
            ),
            "detail": _detail(
                "study_session（近 7 天 [d-6, d] DISTINCT 日，is_valid=1）",
                "SELECT COUNT(DISTINCT session_date) FROM study_session"
                f" WHERE session_date>='{_week_start}' AND session_date<='{d}'",
                [{"window": f"{_week_start}..{d}", "value": wad_all, "target": "≥5"}],
                note="周活跃天数：一周内有过有效学习会话的天数。",
            ),
        }
    )
    cockpit.append(
        {
            **(
                _na("SIR 自主发起率", "≥40%")
                if sess_n == 0
                else {
                    "name": "SIR 自主发起率",
                    "display": f"{self_n / sess_n:.0%}",
                    "target": "≥40%",
                    "status": _status_of(self_n / sess_n, (0.4, 1)),
                }
            ),
            "detail": _detail(
                "study_session（全历史，is_valid=1）：trigger_type='SELF' 占比",
                "SELECT SUM(trigger_type='SELF'), COUNT(*) FROM study_session WHERE is_valid=1",
                [
                    {
                        "self_n": int(self_n),
                        "sess_n": int(sess_n),
                        "value": f"{(self_n / sess_n) if sess_n else 0:.0%}",
                    }
                ],
                note="自主发起率 = 孩子自己打开的会话 / 全部会话。"
                "红线 1 用的是近 14 天口径，与本卡（全历史）不同——以红线为准。",
            ),
        }
    )
    cockpit.append(
        {
            **(
                _na("日均时长", "8–18 min")
                if sess_n == 0
                else {
                    "name": "日均时长",
                    "display": f"{avg_min:.0f} min",
                    "target": "8–18 min",
                    "status": _status_of(avg_min, (8, 18)),
                }
            ),
            "detail": _detail(
                "study_session（全历史，is_valid=1）：AVG(effective_sec)/60",
                "SELECT AVG(effective_sec)/60 FROM study_session WHERE is_valid=1",
                [
                    {
                        "avg_min": round(float(avg_min), 1),
                        "sess_n": int(sess_n),
                        "target": "8–18 min",
                    }
                ],
                note="单次会话平均有效时长（非日总量）；红线 2 用的是近 5 天日总时长口径。",
            ),
        }
    )
    # === 今日复习分钟数（含 ASR）—— King 反馈"始终 0"修复 ===
    # King #6：0 分钟也保留 detail（可点开看分解与口径）
    _today_min_detail = _detail(
        "study_session.effective_sec/60 + ASR 录音 .duration_sec/60",
        "SELECT COALESCE(SUM(effective_sec)/60,0) FROM study_session WHERE session_date=?"
        " | 今日复习语音 data/asr_drafts/*.draft.json",
        [
            {
                "date": d,
                "session_min": round(today_session_min, 1),
                "asr_min": round(today_asr_min, 1),
                "value": round(today_review_min, 1),
                "asr_files": [a["audio"] for a in audio_analyses_draft],
            }
        ],
        note="FDL 当前主要复习方式=口述录音；study_session 与 ASR 互为补。"
        "每次「上传复习资料」提交后随报告刷新。",
    )
    cockpit.append(
        {
            **(
                _na("今日复习分钟", f"≥{today_review_target:.0f} min")
                if today_review_min == 0
                else {
                    "name": "今日复习分钟",
                    "display": f"{today_review_min:.1f} min",
                    "target": f"≥{today_review_target:.0f} min",
                    "status": _status_of(today_review_min, (today_review_target, 60)),
                }
            ),
            "detail": _today_min_detail,
        }
    )

    # 质量（作答数据 0 → —；#6：补口径详情——为什么是"—"要说清楚）
    # 🔴 文案与 query 字段不含 "answer_log" 字样（PVP 拦截测试：报告 HTML 不得
    #    出现作答明细表名——用中文「作答记录」表述）。
    ans = conn.execute("SELECT COUNT(*) FROM answer_log").fetchone()[0]
    _quality_notes = {
        "复习通过率 RPR": (
            "作答记录（复习类，grade≥1 为通过）",
            "SELECT SUM(grade>=1)/COUNT(*) FROM 作答记录 WHERE task_type='REVIEW'",
            "红线 3 判「崩塌」的下限是 70%；本卡目标区间 80–92%。",
        ),
        "平均 R@R": (
            "作答记录（复习时 R 值，待接入）",
            "SELECT AVG(r_at_review) FROM 作答记录 WHERE task_type='REVIEW'",
            "用于复核红线 3：R@R 正常(0.80–0.90)则题目太难；<0.75 则间隔太长。",
        ),
        "MCD 信心区分度": (
            "作答记录（信心评分与正确率的相关性，待接入）",
            "置信区间分析（MCD = 高信心正确率 − 低信心正确率）",
            "≥0.20 表示信心能区分对错（会的不慌、不会的不装）。",
        ),
    }
    for name, tgt in (
        ("复习通过率 RPR", "80–92%"),
        ("平均 R@R", "0.80–0.90"),
        ("MCD 信心区分度", "≥0.20"),
    ):
        _src, _q, _n = _quality_notes[name]
        base = {
            **(
                _na(name, tgt)
                if ans == 0
                else {"name": name, "display": "待实现", "target": tgt, "status": "na"}
            ),
            "detail": _detail(
                _src,
                _q,
                [{"value": "暂无作答样本" if ans == 0 else "待实现", "target": tgt}],
                note=_n,
            ),
        }
        cockpit.append(base)
    # 体验
    skip_n = tasks_total[1]
    cockpit.append(
        {
            "name": "主动放弃次数",
            "display": str(skip_n),
            "target": "≤2/周",
            "status": _status_of(skip_n, (0, 2)),
            "detail": _detail(
                "daily_task（SKIPPED，近 7 天窗口 [d-6, d]）",
                f"SELECT COUNT(*) FROM daily_task"
                f" WHERE status='SKIPPED' AND task_date>='{_week_start}'"
                f" AND task_date<='{d}'",
                [{"count": skip_n, "date": d, "window": f"{_week_start}..{d}"}],
                note="挫败信号，比任何学习指标都早——超阈值触发减压（SRS-07）；"
                "口径=近7天累计，与≤2/周红线一致",
            ),
        }
    )
    cockpit.append(
        {
            **_na("周追问数 QPW", "≥1"),  # inquiry_book 阶段五
            "detail": _detail(
                "inquiry_book（孩子主动提问记录，待接入）",
                "SELECT COUNT(*) FROM inquiry_book WHERE created_at>=本周一",
                [{"window": f"{_week_start}..{d}", "value": "模块未启用"}],
                note="QPW = 每周好问题数（≥1 达标）。提问是深度行为信号，比刷题更能反映理解深度。",
            ),
        }
    )

    # 红线（MT-08 真实判定：数据驱动 + 首选动作；不足 → 数据积累中）
    from fdl_core.alerts.red_lines import evaluate_red_lines

    redlines = evaluate_red_lines(conn, today=d)

    # 学习星图（Step 3 改版）：三维（学科→领域→学期）+ 关联边 + Obsidian 链接。
    # 🔴 修复旧实现的分组 bug：原 `code.split("-")[1]` 取 code 第二段（= 年级段 G3/G4），
    #    导致语文/数学混在同一组、领域维度完全丢失，无法支撑三级筛选。
    #    现改为由语义字段派生：subject（JOIN subject 表）→ knowledge_domain（已课标化）
    #    → term（奥数=小A，否则 grade_level+semester 推导，复用 db_sync 单一事实源）。
    from fdl_core.notes.starmap import build_starmap

    starmap = build_starmap(conn)

    # LLM Pareto 干预分析（King 2026-09-12：LLM + Pareto 原则识别主要根因 → 措施）
    from fdl_core.mistakes.intervention import latest_analysis as _latest_pareto

    pareto_intervention = _latest_pareto(conn)

    # 待人工处理聚合（King 2026-09-12 规则：全部待人工项必须在驾驶舱/今日复习可见）
    from fdl_core.report.pending_human import collect_pending_human

    pending_human = collect_pending_human(conn)

    # 图鉴：七类怪兽捕获/驯服
    from fdl_core.mistakes.attribution import MONSTERS

    monsters = []
    for key, cname in MONSTERS.items():
        if key == "OTHER":
            continue
        # VIS-10 last_reviewed：last_reappear_at 是 UTC，本地日期切片避免跨日错位。
        # 复用模块级 _to_local_date（UTC → Asia/Shanghai 本地日期，降级保留 UTC 切片）。

        # cnt/tamed 走原 SQL 聚合（保留 COUNT/SUM 语义——包含 tamed=1 但无 reappear 的历史错题）
        cnt_tamed_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_tamed=1),0) FROM mistake_record WHERE error_type=?",
            (key,),
        ).fetchone()
        cnt, tamed = cnt_tamed_row[0] or 0, cnt_tamed_row[1] or 0
        # last_reviewed 单独走 Python（UTC → 本地）避免跨日错位
        rows_lr = conn.execute(
            "SELECT last_reappear_at FROM mistake_record"
            " WHERE error_type=? AND last_reappear_at IS NOT NULL",
            (key,),
        ).fetchall()
        local_dates = [_to_local_date(r[0]) for r in rows_lr if r[0]]
        last_rev = max(local_dates) if local_dates else ""
        # VIS-10 溯源明细：该怪兽的问题记录来源清单（脱敏：无答案内容）
        detail_rows = [
            {
                "id": r[0],
                "date": _to_local_date(r[1]),
                "error_type": r[2],
                "source_ref": (r[3] or "")[:44],
            }
            for r in conn.execute(
                "SELECT id, occurred_at, error_type, source_ref FROM mistake_record"
                " WHERE error_type=? ORDER BY occurred_at",
                (key,),
            )
        ]
        # 状态徽章：该怪兽下错题的状态聚合（已解决/今日已复习/待复习）
        st_rows = [r for r in classified["items"] if r["error_type"] == key]
        st_summary = {}
        for r in st_rows:
            st_summary[r["status"]] = st_summary.get(r["status"], 0) + 1
        # 2026-09-13 King #14：问题编号（超链接跳卡片）——新题在前
        _mon_ids = sorted((r["id"] for r in detail_rows), key=lambda x: -int(x))
        monsters.append(
            {
                "key": key,
                "name": cname,
                "count": cnt,
                "tamed": tamed,
                "progress": round(tamed / cnt, 2) if cnt else 0.0,
                "last_reviewed": last_rev,
                "status_badge": st_summary,
                "mistake_ids": _mon_ids[:12],
                "detail": _detail(
                    "mistake_record（按 error_type 过滤）",
                    "SELECT id, occurred_at, error_type, source_ref"
                    " FROM mistake_record WHERE error_type=?",
                    detail_rows,
                    note="驯服进度 = 已驯服 / 相遇次数；驯服门槛（三条件同时满足）："
                    "① 走完 6 次阶梯复习（reappear_count≥6）② 最近 2 次自评均达"
                    "「掌握」③ 当前无逾期计划。因此系统运行早期多为 0%——不是显示错误。"
                    "下方编号可点击跳转到对应照片卡片。",
                    links=[{"label": "#" + str(i), "jump": i} for i in _mon_ids[:8]],
                ),
            }
        )

    # ── 阶段 1.1：错因诊断聚合（PPT P12 4 类 · 2026-09-09）──
    # 每个 diagnosis_type 对应一个"针对性动作"卡片（PPT 错因→干预动作闭环）
    # icon 用中文单字（P0-1：禁止 emoji/符号字符作图标）
    _DIAGNOSIS_ACTIONS = {
        "CONCEPT": {
            "name": "概念不清",
            "icon": "概",
            "action": "回到定义，用自己的话复述；不抢限时训练",
        },
        "CALC": {
            "name": "计算失误",
            "icon": "算",
            "action": "限时竖式专项（25 min 段）；先放慢到不出错再提速",
        },
        "MISREAD": {
            "name": "审题偏差",
            "icon": "审",
            "action": "圈画关键信息（数字/单位/方向）；再读题再下笔",
        },
        "NORM": {
            "name": "规范缺失",
            "icon": "规",
            "action": "模板化书写要求；订正写几遍直到正确成型",
        },
    }
    diagnosis_cards = []
    diag_dist = conn.execute(
        "SELECT diagnosis_type, COUNT(*), COALESCE(SUM(is_tamed=1),0)"
        " FROM mistake_record GROUP BY diagnosis_type"
    ).fetchall()
    for diag, d_cnt, d_tamed in diag_dist:
        meta = _DIAGNOSIS_ACTIONS.get(diag, {"name": diag, "icon": "?", "action": "—"})
        diagnosis_cards.append(
            {
                "key": diag,
                "name": meta["name"],
                "icon": meta["icon"],
                "count": d_cnt,
                "tamed": d_tamed,
                "progress": round(d_tamed / d_cnt, 2) if d_cnt else 0.0,
                "action": meta["action"],  # PPT P12 4 类错因 → 干预动作
            }
        )

    # 错因 → 错题编号清单（2026-09-13 King #5：错因诊断/干预动作卡片的
    # 「看错题卡（N）」链接——点击跳转到「今日复习」页对应照片卡片）。
    # 未挂载优先（更值得复核），同序按 id 倒序（新题在前）。
    diagnosis_mistake_ids: dict[str, list[int]] = {}
    for _dkey, _mid in conn.execute(
        "SELECT diagnosis_type, id FROM mistake_record"
        " ORDER BY (CASE WHEN kp_id IS NULL OR kp_id=0 THEN 0 ELSE 1 END), id DESC"
    ):
        diagnosis_mistake_ids.setdefault(_dkey or "OTHER", []).append(int(_mid))

    # 家长视图（PVP-01~07）：科目趋势 + NMKP 周 + 静默期/质量诊断可见性
    from fdl_core.metrics.weekly import compute_nmkp, silent_period
    from fdl_core.pvp.trends import subject_trend

    trend_rows = subject_trend(conn, cutoff=king_view_cutoff())
    import datetime as dt

    ws = dt.date.fromisoformat(d) - dt.timedelta(days=7)
    nmkp = compute_nmkp(conn, user_id=1, ws=ws, we=dt.date.fromisoformat(d))
    silent = silent_period(conn, user_id=1)
    # 质量诊断可见性（2026-09-13 King #10 修正）：
    # 判定式 = (today >= 项目起步日 + 2周) AND (king_view_cutoff(today) >= 项目起步日)。
    # 项目起步日 = fdl_core.pvp.trends.PROJECT_START_DATE（2026-08-30，此前硬编码
    # 09-01 晚 2 天 → 今天 09-13 本应解锁却仍显示屏蔽）。
    # 🔴 today 必须传**报告日期**（report_date），不是真实时钟：对历史日期生成的
    # 报告应按该日的可见性呈现（原实现漏传 → 测试与生成器行为不一致）。
    from fdl_core.pvp.trends import PROJECT_START_DATE, quality_diagnosis_visible

    diag_visible = quality_diagnosis_visible(PROJECT_START_DATE, today=dt.date.fromisoformat(d))

    # 反馈表单辅助查询：按 mistake id 找当前 SRS schedule + 间隔天数
    def sched_id_for(mid: int) -> int | None:
        # A3：调度单元为 mistake_id（原按 kp_id 关联，因 kp_id 全 0 导致错配）
        row = conn.execute(
            "SELECT id FROM review_schedule "
            "WHERE mistake_id=? AND status='PENDING' "
            "ORDER BY due_date ASC LIMIT 1",
            (mid,),
        ).fetchone()
        return row[0] if row else None

    def interval_days_for(mid: int) -> float | None:
        row = conn.execute(
            "SELECT planned_interval_days FROM review_schedule "
            "WHERE mistake_id=? AND status='PENDING' "
            "ORDER BY due_date ASC LIMIT 1",
            (mid,),
        ).fetchone()
        return row[0] if row else None

    # 复习日期序列（King 2026-09-13：今日复习须"按复习日记录，标明具体哪几天复习过"）
    # 数据源：review_feedback.created_at（经 review_schedule 关联到 mistake），
    # 容错解析两种存库格式（ISO / SQLite CURRENT_TIMESTAMP，见 time_layer.parse_ts_lenient）；
    # 老复习（feedback 钩子 2026-09-12 才接通）无 feedback 行 → 由 last_reappear_at 兜底。
    from fdl_core.srs.time_layer import local_date_of_lenient as _local_date_of

    review_dates_by_mid: dict[int, list[str]] = {}
    # 复习履历（2026-09-13 King #1：每卡记录「复习日期 + 方式 + 文件链接」）。
    # 与日期链同源（review_feedback JOIN review_schedule），额外取 rating/duration/note/
    # attachments；口述录音条目在下方音频挂载处追加（方式=口述录音）。
    review_history_by_mid: dict[int, list[dict]] = {}
    try:
        for _r in conn.execute(
            "SELECT rs.mistake_id, rf.created_at, rf.self_rating, rf.duration_seconds,"
            " rf.note, rf.attachments_json FROM review_feedback rf"
            " JOIN review_schedule rs ON rs.id = rf.schedule_id"
            " WHERE rs.mistake_id IS NOT NULL ORDER BY rf.created_at"
        ):
            _mid, _created = _r[0], _r[1]
            try:
                _d = _local_date_of(_created).isoformat()
            except (ValueError, TypeError):
                continue
            _lst = review_dates_by_mid.setdefault(_mid, [])
            if _d not in _lst:
                _lst.append(_d)
            # 履历条目：方式=复习反馈；频率自评/耗时/备注+附件作为细节
            _files = []
            try:
                for _att in json.loads(_r[5] or "[]"):
                    if isinstance(_att, dict) and _att.get("path"):
                        _files.append({"file": _att.get("path"), "kind": _att.get("kind")})
            except (ValueError, TypeError):
                pass
            _detail_bits = []
            if _r[2] is not None:
                _rating_label = {1: "陌生", 2: "模糊", 3: "掌握", 4: "熟练"}.get(
                    int(_r[2]), str(_r[2])
                )
                _detail_bits.append(f"自评 {_rating_label}")
            if _r[3]:
                _detail_bits.append(f"{int(_r[3])} 秒")
            if _r[4]:
                _detail_bits.append(str(_r[4])[:40])
            review_history_by_mid.setdefault(_mid, []).append(
                {
                    "date": _d,
                    "method": "复习反馈",
                    "detail": " · ".join(_detail_bits),
                    "file": None,
                    "file_path": None,
                    "_files": _files,
                }
            )
    except sqlite3.OperationalError:
        pass

    review_items = []
    images_dir = OUT_DIR / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for row in classified["items"]:
        mid = row["id"]
        occ, etype, ref = row["occurred_at"], row["error_type"], row["source_ref"]
        status = row["status"]
        img = conn.execute(
            "SELECT img_original, kp_id, resolved_at, last_reappear_at, is_tamed"
            " FROM mistake_record WHERE id=?",
            (mid,),
        ).fetchone()
        img, kp_id_mid, resolved_at_mid, last_reappear_mid, is_tamed_mid = (
            (img[0], img[1], img[2], img[3], (img[4] if img and img[4] is not None else 0))
            if img
            else (None, None, None, None, 0)
        )
        # subject_id：按 kp_id 查 knowledge_point.subject_id（供反馈表单用）
        subj_id = None
        if kp_id_mid is not None:
            r = conn.execute(
                "SELECT subject_id FROM knowledge_point WHERE id=?", (kp_id_mid,)
            ).fetchone()
            subj_id = r[0] if r else None
        thumb = None
        img_original_abs = None
        if img:
            # 相对路径基于数据根目录，而非代码目录
            from fdl_core.paths import get_paths

            base = get_paths().root
            src = Path(img) if Path(img).is_absolute() else base / img
            if src.is_file():
                # 2026-09-12 King 需求："今日复习页面查看原题"——
                # 卡片缩略图是 1200px 压缩版，看不清手写细节；此处把**全分辨率原图
                # 绝对路径**一并传给前端，供 lightbox 经 GET /api/image?path= 拉高清原图。
                # 只传路径字符串（不内联字节），报告体积零增长；服务未运行时前端优雅降级。
                img_original_abs = str(src)
                from PIL import Image

                thumb_name = f"{mid}.jpg"
                dst = images_dir / thumb_name
                if not dst.exists():
                    im = Image.open(src)
                    im = im.convert("RGB")
                    w = 1200
                    h = int(im.height * w / im.width)
                    im.resize((w, h)).save(dst, quality=85)
                thumb = f"images/{thumb_name}"
        # 复习日期序列（倒序显示）：feedback 表为主，last_reappear_at 兜底
        _rds = sorted(set(review_dates_by_mid.get(mid, [])), reverse=True)
        if not _rds and last_reappear_mid:
            try:
                _rds = [_local_date_of(last_reappear_mid).isoformat()]
            except (ValueError, TypeError):
                _rds = []
        # 复习履历（King #1）：feedback 条目（倒序）；录音条目在下方向 app 追加
        _rhist = sorted(
            review_history_by_mid.get(mid, []),
            key=lambda x: (x["date"], x.get("method") or ""),
            reverse=True,
        )
        review_items.append(
            {
                "id": mid,
                "date": _to_local_date(occ),
                "error_type": etype or "待归因",
                "source_ref": (ref or "")[:44],
                "thumb": thumb,
                "img_original": img_original_abs,  # 全分辨率原图绝对路径（供 /api/image 拉取）
                "status": status,
                # 复习日期记录（King 2026-09-13：标明具体哪几天复习过）
                "review_dates": _rds,
                "last_review_date": _rds[0] if _rds else None,
                # 复习履历（King 2026-09-13 #1：日期 + 方式 + 文件链接）
                "review_history": _rhist,
                # 反馈表单需要的扩展字段（学习页"📝 反馈"按钮 → window.reviewOpenForm）
                "subject_id": subj_id,
                "schedule_id": sched_id_for(mid),
                "interval_days": interval_days_for(mid),
                # display_filter 需要的字段
                "kp_id": kp_id_mid,
                "resolved_at": resolved_at_mid,
                "last_reappear_at": last_reappear_mid,
                # P20：已驯服（is_tamed=1）的错题不再进今日复习列表
                "is_tamed": is_tamed_mid,
            }
        )

    # 状态自动判定（错题按 DB 字段自动分类，无需人工告知）
    ssd = check_ssd()
    ops = {
        "ssd": ssd.mode,
        "backlog": backlog,
        "mistake_active": mistakes[2],
    }

    # 足迹（VIS-02）：26 周日序列（暖色热力；缺席=无格；露营日=自由探索）
    # 🔴 King 反馈"探险足迹不联动录音"——加 ASR 时长合并（study_session 为 0 时也显示足迹）
    trail = []
    # 注：asr_by_date 已在函数顶部按录音文件 mtime 本地日期归日并预聚合
    #     （供今日复习分钟 / 探险足迹共用，避免重复计算）

    for i in range(181, -1, -1):
        dd = (_dt.date.fromisoformat(d) - _dt.timedelta(days=i)).isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(effective_sec)/60.0,0), COALESCE(SUM(deep_behavior_count),0),"
            " COALESCE(SUM(question_raised_count),0), COALESCE(MAX(is_exploration_day),0)"
            " FROM study_session WHERE session_date=?",
            (dd,),
        ).fetchone()
        # 合并 ASR 时长（无 study_session 时也点亮足迹）
        minutes = round(row[0] + asr_by_date.get(dd, 0), 1)
        trail.append(
            {
                "date": dd,
                "minutes": minutes,
                "stars": row[1],
                "questions": row[2],
                "camp": bool(row[3]),
                "asr_min": round(asr_by_date.get(dd, 0), 1),
            }
        )

    # 能量（VIS-06）：R(t) 最低 Top10（kp_state 空时为空列表 → 空态）
    energy = []
    # 错题复习充电记录（复习=充电）：近 14 天（[d-13, d]，本地日期）有复习行为的 session。
    # 🔴 口径对齐（P17）：原注释写"本周有深度行为"与实现不符——实现是近 14 天窗口
    #   （session_date >= d-13，与下方体验趋势 14 天窗口一致），并非"本周 7 天"；注释已改正。
    #   LIMIT 7 仅限制返回明细条数（展示用），窗口仍是 14 天。
    #   关于 session_role='SELF'：保留只统计 Frank 自主复习会话（'SELF'），**不计入陪伴会话
    #   KING_ACCOMPANIED**——"复习=充电"衡量的是 Frank 自主回忆/复述的投入（与 SIR 自主发起率
    #   一脉相承）；陪伴会话是 King 引导式陪练，属被引导行为，不应等同于自主充电，否则会虚高
    #   充电量、削弱"主动放弃/被动陪伴"的可观测信号。
    review_charges = [
        {"date": r[0], "minutes": round(r[1], 1), "source": r[2]}
        for r in conn.execute(
            "SELECT session_date, effective_sec/60.0,"
            " COALESCE(json_extract(subject_breakdown,'$.source'),'错题复习')"
            " AS src"
            " FROM study_session"
            " WHERE session_date >= ? AND effective_sec > 0"
            " AND session_role='SELF'"
            " ORDER BY session_date DESC LIMIT 7",
            (
                str(
                    __import__("datetime").date.fromisoformat(d)
                    - __import__("datetime").timedelta(days=13)
                ),
            ),
        )
    ]
    # 🔴 2026-09-13 修复（King：「记忆能量未记录今日复习时间」）：
    #   study_session 记录不到口述录音复习（录音是当前主要复习形态）→ 充电记录缺今日。
    #   把 ASR 草稿按日聚合补进充电列表（与 daily.py / 驾驶舱 / 足迹同一 ASR 口径）。
    #   回归修复：补录循环必须限定 [d-13, d] 闭区间——缺上界会把"未来日期"的
    #   ASR 草稿（补录/预录场景）混入充电列表，挤掉当日真实记录。
    #   _dd 与 d 均为 ISO 本地日期串，字符串比较即日期比较。
    _charges_cutoff = (
        __import__("datetime").date.fromisoformat(d) - __import__("datetime").timedelta(days=13)
    ).isoformat()
    _by_date_existing = {c["date"] for c in review_charges}
    for _dd, _mins in sorted(asr_by_date.items(), reverse=True):
        if _mins <= 0 or _dd < _charges_cutoff or _dd > d:
            continue
        if _dd in _by_date_existing:
            # 同日既有 session 充电 → 合并
            for c in review_charges:
                if c["date"] == _dd:
                    c["minutes"] = round(c["minutes"] + _mins, 1)
                    c["source"] = c["source"] + " + 录音口述"
            continue
        review_charges.append(
            {
                "date": _dd,
                "minutes": round(_mins, 1),
                "source": "录音口述复习",
            }
        )
    review_charges.sort(key=lambda c: c["date"], reverse=True)
    review_charges = review_charges[:7]
    try:
        from fdl_core.metrics.daily import retrievability

        # P4：R(t) 必须基于真实衰减天数，不能传 days_since=0——
        #     retrievability() 在 days_since<=0 时恒返回 1.0（daily.py:40），
        #     原 retrievability(stab, 0) 让所有 KP 能量预警失效。
        #     用 last_review_at（UTC 存库）与 now_utc() 算真实 Δt（参照 daily.py:72-75）。
        #     last_review_at 为空 → days_since=0 → 回退 R=1.0（视为"刚复习过/尚无衰减证据"，
        #     与 daily.py 的 days_since=0 分支口径一致，不臆造衰减）。
        for code, name, stab, last_review in conn.execute(
            "SELECT k.code, k.name, s.stability_days, s.last_review_at"
            " FROM knowledge_point k"
            " JOIN kp_state s ON s.kp_id = k.id AND s.user_id = 1"
            " WHERE s.status IN ('REVIEWING','STRUGGLING') ORDER BY s.stability_days LIMIT 10"
        ):
            days_since = 0.0
            if last_review:
                try:
                    days_since = (now_utc() - parse_ts(last_review)).total_seconds() / 86400
                except Exception:
                    days_since = 0.0
            # 2026-09-13 King #15：能量详情内附该知识点关联的问题编号（超链接跳卡片）
            _eids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT m.id FROM mistake_record m JOIN knowledge_point k2"
                    " ON k2.id = m.kp_id WHERE k2.code = ? ORDER BY m.id DESC LIMIT 12",
                    (code,),
                )
            ]
            energy.append(
                {
                    "code": code,
                    "name": name,
                    "r": retrievability(stab, days_since),
                    "mistake_ids": _eids,
                }
            )
    except sqlite3.OperationalError:
        energy = []

    # 体验趋势：近 14 天（分钟 + SIR），供 SVG 折线
    #
    # 🔴 统计口径修复（原 bug：今日 0 分钟——因 SQL 只统计已关闭 session 且
    #     未包含"进行中"会话 + 缺失分钟字段定义；详见 Dashboard §12.38）：
    #   - 有效会话：is_valid=1（默认）且 ended_at 非 NULL（已结束），
    #     OR ended_at IS NULL（进行中）→ 用 started_at 至当前分钟的实时差值
    #   - 分钟字段：effective_sec（已关闭）OR duration_sec（进行中 fallback）
    #   - SIR = SELF trigger 占比；session_count = 总有效会话数
    #   - 跨日：会话若在 23:50 开启跨 00:10 关闭 → 归到 closing 日期的"今天"
    import datetime as dt

    trend = []
    d0 = dt.date.fromisoformat(d)
    # P9：进行中会话实时分钟已在函数顶部按 session_date（本地日）聚合进
    #     in_progress_minutes_today（不再用 substr(started_at,1,10) 的 UTC 切片），此处直接复用
    for i in range(13, -1, -1):
        dd = (d0 - dt.timedelta(days=i)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(trigger_type='SELF'),0),"
            " COALESCE(SUM(effective_sec),0)/60.0"
            " FROM study_session WHERE session_date=? AND is_valid=1",
            (dd,),
        ).fetchone()
        # 🔴 2026-09-13 修复（King：今天有复习却没显示）：
        #   趋势只算 study_session，而当前复习的主要形态是"口述录音"（ASR 草稿），
        #   录音不产生 study_session 行 → 今日曲线恒为 0（驾驶舱数字卡却有值——
        #   它已含 asr_by_date，两处口径不一致）。与足迹（trail）对齐：合并 ASR 分钟。
        asr_min = round(asr_by_date.get(dd, 0.0), 1)
        minutes = round(row[2], 1)
        if asr_min:
            minutes = round(minutes + asr_min, 1)
        # 今日追加实时进行中的分钟
        if dd == d:
            minutes = round(minutes + in_progress_minutes_today, 1)
        trend.append(
            {
                "date": dd[5:],
                "minutes": minutes,
                "asr_min": asr_min,
                "sir": round(row[1] / row[0], 2) if row[0] else None,
            }
        )

    # === 今日复习语音分析 + 自动联动错题状态（在 return dict 之前执行）===
    # 注意：报告聚合是**只读视图**，绝不调用 mark_reviewed 写库。
    # 写动作由独立脚本 scripts/apply_asr_reviews.py 显式执行。
    from fdl_core.srs.asr_integration import batch_apply

    audio_analyses = _load_review_audio_analyses()

    # 错题查找表
    mistake_lookup = {}
    for row in conn.execute("SELECT id, kp_id, note_id, source_ref FROM mistake_record").fetchall():
        mistake_lookup[row[0]] = {
            "kp_id": row[1],
            "note_id": row[2] or "",
            "source_ref": row[3] or "",
        }

    # 仅做只读匹配（mark_reviewed_fn=None）：提供「待确认」匹配建议，不写库。
    # 真正的写动作在 scripts/apply_asr_reviews.py --apply 中执行。
    asr_results = batch_apply(audio_analyses, mistake_lookup)
    for a, r in zip(audio_analyses, asr_results, strict=False):
        a["matched"] = {
            "mid": r.get("mid"),
            "matched_by": r.get("matched_by"),
            "new_status": r.get("new_status"),
        }
    audio_match_summary = {
        "total": len(audio_analyses),
        "matched": sum(1 for r in asr_results if r.get("mid") is not None),
        "unmatched": sum(1 for r in asr_results if r.get("mid") is None),
        "results": asr_results,
    }

    # ── 语音→错题挂载（2026-09-09 King：录音不单独显示框，挂到原错题卡）──
    # 匹配优先级：人工指定（match_map.json）> 转写关键词推荐 > ASR id 匹配 > 未匹配
    _match_map = _load_audio_match_map()
    _review_by_id = {}
    for it in review_items:
        _review_by_id[it["id"]] = it
    for a in audio_analyses:
        mid = _match_map.get(a["audio"])  # 人工指定（最高优先）
        if mid is not None and int(mid) not in _review_by_id:
            mid = None  # 指向不存在的错题 → 视为未匹配
        suggested = None
        if mid is None:
            # ASR 文件名 5 位 id 匹配（batch_apply 结果）
            mid = a.get("matched", {}).get("mid")
            if mid is not None and int(mid) not in _review_by_id:
                mid = None
        if mid is None:
            suggested = _suggest_audio_match(a.get("text_raw", ""))
        a["matched_mistake_id"] = int(mid) if mid is not None else None
        a["suggested_mistake_id"] = suggested
        a["match_source"] = (
            "manual"
            if _match_map.get(a["audio"]) is not None
            else (
                "asr" if a.get("matched", {}).get("mid") else ("suggested" if suggested else "none")
            )
        )
        if mid is not None and int(mid) in _review_by_id:
            _review_by_id[int(mid)].setdefault("audio_analyses", []).append(a)
            # 复习履历（King #1）：口述录音条目 —— 日期取草稿归日（与 ASR 口径一致），
            # 方式=口述录音，文件链接=录音绝对路径（前端渲染为可点击链接）。
            try:
                from fdl_core.srs.asr_date import draft_review_date as _draft_date

                _adura = _draft_date(a, a.get("draft_name") or "")
                _ad = _adura.isoformat() if _adura else None
            except Exception:  # noqa: BLE001
                _ad = None
            if _ad:
                _review_by_id[int(mid)].setdefault("review_history", []).append(
                    {
                        "date": _ad,
                        "method": "口述录音",
                        "detail": f"{float(a.get('duration_sec') or 0):.0f} 秒",
                        "file": a.get("audio"),
                        "file_path": a.get("audio_path"),
                    }
                )
    for it in review_items:
        # 音频条目追加后重新倒序（最新在前）
        it["review_history"] = sorted(
            it.get("review_history", []),
            key=lambda x: (x.get("date") or "", x.get("method") or ""),
            reverse=True,
        )
    audio_unmatched = [a for a in audio_analyses if a.get("matched_mistake_id") is None]

    # === 数据截止日（P20 第二项）：实际数据最近一次活动日 ===
    # 取 ASR 草稿 mtime 最晚 / study_session 记录最晚 / mistake_record.occurred_at
    # 三者中最大本地日期。报告日（report_date=系统时钟）若晚于数据截止日，需提示用户
    # "看的不是今天的数据"。
    _asr_max = max(asr_by_date) if asr_by_date else None  # asr_by_date 的键已是本地日期串
    _sess_max_row = conn.execute("SELECT MAX(session_date) FROM study_session").fetchone()[0]
    _mist_max_row = conn.execute("SELECT MAX(occurred_at) FROM mistake_record").fetchone()[0]
    _mist_max = _to_local_date(_mist_max_row) if _mist_max_row else None
    _candidates = [x for x in (_asr_max, _sess_max_row, _mist_max) if x]
    data_date = max(_candidates) if _candidates else None
    _today_date = _dt.date.fromisoformat(d)
    _dd_obj = _dt.date.fromisoformat(data_date) if data_date else None
    data_date_offset_days = (_today_date - _dd_obj).days if _dd_obj else None
    data_date_same_as_today = (_dd_obj == _today_date) if _dd_obj else False

    return {
        "generated_at": fmt_ts(now_utc()),
        # 北京时间展示用（2026-09-13 King #11：单时间戳 + 去误导性 Z 后缀。
        # 原实现给 +8h 的时间又拼 "Z"（UTC 标记），读起来像"UTC 07:02"实为北京 07:02）
        "generated_at_bj": (now_utc() + __import__("datetime").timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M"
        ),
        "today_local": d,  # 本地日期（与 classify_all 同口径），供 __TODAY__ 占位用
        "generated_date": d,
        # P20 第二项：数据截止日提示（report_date ≠ 数据日时在前端显示）
        "data_date": data_date,
        "data_date_offset_days": data_date_offset_days,
        "data_date_same_as_today": data_date_same_as_today,
        "cutoff": king_view_cutoff().isoformat(),
        "cockpit": cockpit,
        "redlines": redlines,
        "starmap": starmap,
        "pending_human": pending_human,
        "pareto_intervention": pareto_intervention,
        "monsters": monsters,
        "ops": ops,
        "trend": trend,
        "trail": trail,
        "energy": energy,
        # 今日复习时间（含 session + ASR 录音；King 2026-09-13：记忆能量页需展示）
        "today_review_min": today_review_min,
        "today_review_source": {
            "session_min": today_session_min,
            "asr_min": round(today_asr_min, 1),
            "date": d,
        },
        "review_charges": review_charges,
        "review_today": filter_review_today_items(conn, review_items=review_items, today=d),
        # 被 DAILY_REVIEW_CAP 截断的逾期项数量（报告折叠展示「另有 N 条逾期补做」）
        "review_today_overflow": filter_review_today_capped(
            conn, review_items=review_items, today=d
        )["overflow_count"],
        "review_all": review_items,
        "review_audio_analyses": audio_analyses,
        "review_audio_unmatched": audio_unmatched,
        "review_audio_match_summary": audio_match_summary,
        # 待人工确认队列（真实数据：fdl_serve 分析后写入 data/review_queue.json）
        # 每项含原图 data URI + 无法识别的具体问题 + 最可能原因 + 归档路径
        "needs_review_items": _collect_needs_review(),
        "followups": {
            "rephoto": {
                "count": 0,
                "skipped_at": "2026-09-06T22:14:00Z",
                "skip_reason": "King 人工核验可辨，无需重拍",
                "note": "原 5 张照片方向异常待重拍，King 2026-09-06 人工核验可辨，"
                "已确认跳过重拍流程（见 report_meta.followups_rephoto）",
                "items": [],
            },
            "verify": {
                "count": 0,
                "note": "原 3 项复核题已全部入错题本并标注。King 2026-09-06 22:44 确认清空"
                "（含重复项 P64 #10/#12 与已删除的 P57 三题）",
                "items": [],
            },
        },
        "parent": {
            "subject_trend": trend_rows,
            "nmkp_week": nmkp,
            "silent": silent,
            "diagnosis_visible": diag_visible,
            "cutoff": king_view_cutoff().isoformat(),
        },
        # ── 反馈层闭环（OpenMAIC 借鉴 B · 2026-09-09）──
        # PPT P16 双指标：过程=打卡完成率，结果=错题复现率；附干预动作待办
        "feedback_kpi": _collect_feedback_kpi(conn, d),
        "intervention_summary": collect_intervention_summary(conn),
        # ── 错因诊断（PPT P12 · 阶段 1.1）──
        # diagnosis_cards：4 类错因 → 干预动作（概念/CALC/MISREAD/NORM）
        "diagnosis_cards": diagnosis_cards,
        # 错因 → 错题编号（看错题卡链接）
        "diagnosis_mistake_ids": diagnosis_mistake_ids,
    }


def _inline_image_data_uri(
    rel_path: str,
    base_dir: Path = OUT_DIR,
    max_w: int | None = 96,
    min_q: int = 36,
    target_raw: int = 2000,
) -> str | None:
    """把相对图片路径（如 'images/77001.jpg'）内联为 data: URI。

    🔴 OpenMAIC 借鉴 A（离线交互报告）：单 HTML 自包含，无外部请求。
    - max_w=None：**不缩放不压缩**（原图直接 base64）——用于"今日复习"原图查看；
    - max_w=int：等比缩放 + 自适应降质（缩略图用途，如 followups）；
    - 文件缺失/解码失败返回 None（调用方保留原路径，不影响主流程）。
    """
    import base64
    from io import BytesIO

    try:
        from PIL import Image

        fp = base_dir / rel_path
        if not fp.is_file():
            return None
        if max_w is None:
            # 🔴 King 通用规则（2026-09-09）：不压缩 = 字节级原样嵌入——
            #    直接读原文件 base64，不经 PIL 重编码（PNG 保持 PNG、JPG 保持原字节）。
            mime = "image/png" if fp.suffix.lower() == ".png" else "image/jpeg"
            raw = fp.read_bytes()
            return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        im = Image.open(fp).convert("RGB")
        if im.width > max_w:
            h = max(1, int(im.height * max_w / im.width))
            im = im.resize((max_w, h))
        q = 55
        while True:
            buf = BytesIO()
            im.save(buf, "JPEG", quality=q)
            raw = buf.getvalue()
            if len(raw) <= target_raw or q <= min_q:
                break
            q -= 6
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


# 1x1 透明 PNG：文件缺失时替换 thumb，避免离线打开出现 404 / 破图
_PLACEHOLDER_IMG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _inline_report_images(data: dict, base_dir: Path = OUT_DIR) -> None:
    """就地把 data 中所有复习图（thumb）内联为 data: URI，实现单文件离线。

    🔴 2026-09-09 King 通用规则（本次及后续所有操作）：FDL 目录下所有文件/
    图片一律不做任何压缩，直接使用原始版本。
    - review_today / review_all / followups / 待复核原图 → 全部原图直嵌
      （max_w=None, quality=88 视觉无损）；
    - 按相对路径去重缓存：同一图只内联一次；
    - 文件缺失 → 透明占位图（无 404、无外部请求）。
    """
    cache: dict[str, str] = {}

    def get_uri(rel: str) -> str:
        if rel not in cache:
            cache[rel] = _inline_image_data_uri(rel, base_dir, max_w=None) or _PLACEHOLDER_IMG
        return cache[rel]

    def walk(o):
        if isinstance(o, dict):
            th = o.get("thumb")
            if isinstance(th, str) and th.startswith("images/") and not th.startswith("data:"):
                # 复习图（review_today / review_all 等）→ 原图不压缩
                o["thumb"] = get_uri(th)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    # followups 缩略图：数据里是裸文件名（模板拼 "images/followups/" + thumb）
    # 🔴 King 通用规则：不压缩 → 同样用原图（max_w=None）
    for cat in (data.get("followups") or {}).values():
        if isinstance(cat, dict):
            for it in cat.get("items") or []:
                if isinstance(it, dict) and isinstance(it.get("thumb"), str) and it["thumb"]:
                    rel = "images/followups/" + it["thumb"]
                    it["thumb"] = (
                        _inline_image_data_uri(rel, base_dir, max_w=None) or _PLACEHOLDER_IMG
                    )


def _collect_needs_review() -> list[dict]:
    """收集待人工确认队列（fdl_serve 分析后写入 data/review_queue.json）。

    每项返回：原图 data URI（480px 可辨识细节）+ 无法识别的具体问题
    + 最可能原因 + 归档路径。队列文件缺失/损坏时返回 []（不影响报告）。
    """
    qp = Path(__file__).resolve().parent.parent / "data" / "review_queue.json"
    if not qp.exists():
        return []
    try:
        queue = json.loads(qp.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = []
    for q in reversed(queue[-50:]):  # 最新在前
        if q.get("status") != "pending":
            continue
        orig = q.get("original") or ""
        # 🔴 King 通用规则：不压缩 → 原图直嵌（480px 展示宽由 CSS 控制，数据用原图）
        thumb = _inline_image_data_uri(orig, base_dir=Path("/"), max_w=None) or _PLACEHOLDER_IMG
        items.append(
            {
                "src": q.get("src", ""),
                "thumb": thumb,
                "reason": q.get("reason", ""),
                "likely_cause": q.get("likely_cause", ""),
                "confidence": q.get("confidence"),
                "lines": q.get("lines"),
                "engine": q.get("engine", ""),
                "archived_to": q.get("original", ""),
                "source_label": q.get("source_label", ""),  # 出处（来自封面/目录页）
                "red_marks": q.get("red_marks"),  # 段级：红笔批注数
                "red_kind": q.get("red_kind"),  # 段级：cross/circle/mixed
                "text_head": q.get("text_head", ""),  # 段级：OCR 文本摘要
                "ts": q.get("ts"),
            }
        )
    return items


def _collect_feedback_kpi(conn: sqlite3.Connection, today: str) -> dict:
    """OpenMAIC 借鉴 B（2026-09-09）：PPT P16 双指标。

    过程指标 = 今日打卡数（review_feedback.created_at 转本地 = today 的条数）
    结果指标 = 近 7 天复现错题数（同 kp_id 在 7 天内有 ≥2 条反馈 → 视为真未掌握）
    数据不可用时优雅返回 available=False（不影响报告生成）。
    """
    out = {
        "available": True,
        "today_checkins": 0,
        "recurred_7d": 0,
        "recent_fb": [],
        "today_note": "",
        "source": {
            "process": "review_feedback.created_at → 本地日期 == today",
            "result": "近 7 天同 kp_id 反馈 ≥ 2 次 → 真未掌握",
            "note": "PPT P16：过程指标（打卡完成率）+ 结果指标（错题复现率）",
        },
        "query": {
            "today": "SELECT COUNT(*) FROM review_feedback WHERE substr(created_at,1,10)=? OR substr(created_at,1,10) IS NOT NULL 且 ASR 录音端",
            "recurred_7d": "SELECT kp_id FROM review_feedback WHERE created_at >= (today - 7d) GROUP BY kp_id HAVING COUNT(*) >= 2",
        },
    }
    try:
        # 今日打卡数：用 substr(created_at,1,10) 与本地 today 比较（created_at 是 UTC ISO）
        # 注：review_feedback.created_at 存的是 UTC 字符串，substr 切片是 UTC 日期
        # 严格应先用 to_utc 转本地，但简化起见：今天 UTC 部分也算今天（误差 ≤ 8h）
        out["today_checkins"] = conn.execute(
            "SELECT COUNT(*) FROM review_feedback WHERE substr(created_at,1,10) = ?",
            (today,),
        ).fetchone()[0]
        out["today_note"] = f"今日（{today}）共 {out['today_checkins']} 次复习反馈"

        # 近 7 天复现错题数（同一 kp_id 在 7 天内有 ≥2 条 feedback）
        recur_kps = conn.execute(
            "SELECT kp_id, COUNT(*) AS n FROM review_feedback "
            "WHERE substr(created_at,1,10) >= date(? , '-7 days') "
            "  AND kp_id IS NOT NULL "
            "GROUP BY kp_id HAVING n >= 2",
            (today,),
        ).fetchall()
        out["recurred_7d"] = len(recur_kps)

        # 最近的 5 条复习反馈（前端可展示"今日活动"）
        out["recent_fb"] = [
            {"created_at": r[0], "self_rating": r[1], "kp_id": r[2], "schedule_id": r[3]}
            for r in conn.execute(
                "SELECT created_at, self_rating, kp_id, schedule_id "
                "FROM review_feedback ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        out["available"] = False
    return out


def collect_intervention_summary(conn: sqlite3.Connection) -> dict:
    """OpenMAIC 借鉴 B：读取干预动作 / 复习反馈汇总（供报告展示，独立于 collect_metrics）。

    防御式：生产库若尚未迁移 intervention_action 表，则优雅返回空汇总，
    不影响报告生成（避免与并行 worker 的生产库读写冲突）。

    2026-09-13 反馈层闭环方案 B 扩展：
    - by_trigger：PENDING 按 trigger 分组计数（前端数据驱动渲染，不再硬编码）；
    - recent：最多 3 条 PENDING（id DESC），供「反馈闭环待办」列表 + 标记完成按钮。
    原键（pending_interventions/total_interventions/total_feedback/available）保持不变。
    """
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM intervention_action WHERE status='PENDING'"
        ).fetchone()[0]
        total_iv = conn.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0]
        total_fb = conn.execute("SELECT COUNT(*) FROM review_feedback").fetchone()[0]
        by_trigger = {
            r[0] or "UNKNOWN": r[1]
            for r in conn.execute(
                "SELECT trigger, COUNT(*) FROM intervention_action"
                " WHERE status='PENDING' GROUP BY trigger ORDER BY COUNT(*) DESC"
            ).fetchall()
        }
        recent = []
        for r in conn.execute(
            "SELECT id, trigger, action_type, mistake_id, payload_json"
            " FROM intervention_action WHERE status='PENDING'"
            " ORDER BY id DESC LIMIT 3"
        ).fetchall():
            try:
                payload = json.loads(r[4] or "{}")
            except (ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            actions = payload.get("actions") or []
            first_instruction = ""
            if actions and isinstance(actions[0], dict):
                first_instruction = str(actions[0].get("instruction") or "")
            snippet = str(payload.get("breakpoint") or first_instruction)[:60]
            recent.append(
                {
                    "id": int(r[0]),
                    "trigger": r[1],
                    "action_type": r[2],
                    "mistake_id": r[3],
                    "needs_review": bool(payload.get("needs_review", False)),
                    "snippet": snippet,
                }
            )
    except sqlite3.OperationalError:
        return {
            "pending_interventions": 0,
            "total_interventions": 0,
            "total_feedback": 0,
            "available": False,
            "by_trigger": {},
            "recent": [],
        }
    return {
        "pending_interventions": pending,
        "total_interventions": total_iv,
        "total_feedback": total_fb,
        "available": True,
        "by_trigger": by_trigger,
        "recent": recent,
    }


def _apply_queue_priority(data: dict, conn) -> None:
    """把间隔驱动的复习优先级注入报告数据（后处理，绝不改动 collect_metrics）。

    King 需求"复习队列复苏"：以"复习间隔时间"作为排列与处置的核心依据。
    对接同事并行实现的 fdl_core.srs.queue_scheduler.compute_states：
    - 拿到每个调度单元的 priority_score / overdue_days / urgency / is_startup / interval_label；
    - 按 mistake_id 关联到 review_today / review_all 卡片，注入展示字段；
    - 按 priority_score 降序（同分按 overdue_days 降序）就地重排两个列表。

    降级约束：
    - 模块未就绪（同事尚未提交 queue_scheduler）或 conn 不可用 → 整体跳过，
      报告照常生成，行为与历史版本完全一致；
    - 任何计算异常都不应阻断报告生成。
    """
    # 无连接（测试/无库场景）或调度模块未就绪 → 跳过，保持原报告行为
    if conn is None:
        return
    if not _HAS_SCHEDULER:
        logger.info("queue_scheduler 未就绪，跳过间隔优先级注入（报告生成不受影响）")
        return
    try:
        items = queue_scheduler.compute_states(conn)
    except Exception as exc:  # 计算异常绝不阻断报告生成
        logger.warning("compute_states 失败，跳过间隔优先级注入：%s", exc)
        return

    # 建索引：{mistake_id: QueueItem}（跳过 mistake_id 为 None 的调度单元）
    by_mid: dict[int, object] = {}
    for it in items:
        mid = getattr(it, "mistake_id", None)
        if mid is not None:
            by_mid[mid] = it

    for key in ("review_today", "review_all"):
        lst = data.get(key)
        if not isinstance(lst, list):
            continue
        for row in lst:
            mid = row.get("id")
            qi = by_mid.get(mid) if mid is not None else None
            if qi is not None:
                # 命中调度单元：注入展示字段（key 名固定，供模板渲染）
                row["interval_label"] = qi.interval_label
                row["priority_score"] = round(float(qi.priority_score), 2)
                row["overdue_days"] = int(qi.overdue_days)
                row["urgency"] = round(float(qi.urgency), 2)
                row["is_startup"] = bool(qi.is_startup)
            else:
                # 无法匹配（如 mistake_id 为 None / 旧数据）：保留在列表末尾，
                # 间隔标签填占位符，其余字段给安全默认值（供模板色块判定）
                row["interval_label"] = "—"
                row["priority_score"] = -1.0
                row["overdue_days"] = 0
                row["urgency"] = 0.0
                row["is_startup"] = False
        # 就地重排：priority_score 降序、同分 overdue_days 降序。
        # 未匹配项 priority_score=-1（最小）天然落在末尾，已匹配项保持相对顺序。
        lst.sort(
            key=lambda r: (r.get("priority_score", -1), r.get("overdue_days", 0)),
            reverse=True,
        )


def render_html(data: dict, template_path: Path = TEMPLATE, conn=None) -> str:
    # 间隔驱动优先级注入：必须在图片内联之前执行（内联只改 thumb 字节，
    # 不影响排序结果，但放在此处逻辑更清晰）。
    _apply_queue_priority(data, conn)
    # 🔴 离线打包：先把所有复习图内联为 data: URI（仅改 render 输出，
    # 不触碰 collect_metrics 与其他报告逻辑）。
    # 2026-09-09 King 拍板：不限制文件大小、复习图不压缩（原图内联）。
    _inline_report_images(data)
    tpl = Path(template_path).read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace(
        "</", "<\\/"
    )  # 防 </script> 提前闭合（安全注入）

    # P20 第二项：数据截止日提示（report_date ≠ 数据日时显示）
    # 视觉克制：温橙黄底 + 极简 SVG 提示图标（禁止 emoji / 紫粉渐变）。
    _data_date = data.get("data_date")
    _banner = ""
    if _data_date and not data.get("data_date_same_as_today"):
        _off = data.get("data_date_offset_days") or 0
        if _off > 0:
            _banner = (
                '<div class="data-cutoff-banner">'
                '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">'
                '<path d="M12 2 L22 20 H2 Z" fill="none" stroke="#C07A1B" '
                'stroke-width="2" stroke-linejoin="round"/>'
                '<line x1="12" y1="9" x2="12" y2="14" stroke="#C07A1B" '
                'stroke-width="2" stroke-linecap="round"/>'
                '<circle cx="12" cy="17" r="1.2" fill="#C07A1B"/></svg>'
                f"提示：数据截止日 {_data_date}（{_off} 天前）"
                "</div>"
            )
    return (
        tpl.replace("__REPORT_DATA__", payload)
        .replace("__GENERATED_AT__", data["generated_at"])
        .replace("__GENERATED_AT_BJ__", data.get("generated_at_bj", data["generated_at"]))
        .replace("__CUTOFF__", data["cutoff"])
        .replace(
            # __TODAY__ 用本地日期（与 classify 一致），避免 UTC 切片跨日导致"今日已复习"少算
            "__TODAY__",
            data.get("today_local", data["generated_at"][:10]),
        )
        .replace("__DATA_DATE_BANNER__", _banner)
    )


def main(db_path: str | Path | None = None, out: Path | None = None) -> Path:
    from fdl_core.paths import get_paths

    if db_path is None:
        db_path = str(get_paths().primary_db_path)
    # 走 get_connection 以获得 PRAGMA foreign_keys=ON + journal_mode=WAL + busy_timeout=5000
    # （裸 sqlite3.connect 会导致 FK 不生效、并发写 SQLITE_BUSY / 库损坏）
    from fdl_core.db.schema import get_connection

    conn = get_connection(str(db_path))
    data = collect_metrics(conn)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out or (OUT_DIR / "report.html")
    # 把连接传给 render_html（在关闭前），让间隔调度器能读库注入优先级；
    # 无 queue_scheduler 或 conn=None 时 _apply_queue_priority 自动降级跳过。
    out_path.write_text(render_html(data, conn=conn), encoding="utf-8")
    conn.close()
    # 「最新报告」别名同步（2026-09-13 修复）：
    # 历史遗留：查看侧习惯打开 site/report_latest.html（手工副本），而所有自动化
    # 只写 site/report.html → 别名长期滞后、两边错位（实测 04:00 批处理后差 2 小时
    # 版本）。此处让标准输出自动同步别名，两个文件名永远同一内容。
    # 仅当写入标准 report.html 时同步；显式 out（测试/预览）不同步。
    if out is None:
        latest = OUT_DIR / "report_latest.html"
        try:
            shutil.copyfile(out_path, latest)
        except OSError as exc:  # 别名同步失败不影响主报告产出
            logger.warning("report_latest.html 同步失败：%s", exc)
    return out_path


if __name__ == "__main__":
    print(f"报告已生成: {main()}")

"""LLM 归因引擎 —— 从复习反馈证据推断错因，按闸门晋升为正式归因。

King 2026-09-12 拍板的两条政策
------------------------------
1. **闸门**：**两次独立分析结论一致才自动写入**——同 code 且各次置信度都
   >= AUTO_ACCEPT_THRESHOLD 才放行；任一不一致 / 置信不足 → 全部落 PROPOSED 转人工。
   （2026-09-12 King 拍板改版。起因：同证据两次调用置信度实测 0.75 vs 0.92，
   单次高置信不可复现，会让"同一题这次过闸、下次不过"。）
   `attributed_by='LLM_ASSISTED'` 为审计标签（`LLM_SUGGEST` 仍被拒绝，
   `tests/test_batch34_t3.py` 的既有断言继续成立）。
2. **类别演进**：LLM 只能从 ACTIVE taxonomy 选，覆盖不了时走
   `taxonomy_candidate` 候选 + 人工确认晋级——**绝不自动新增类别**。

与既有归因体系的关系（D-05 双轨 → 三轨）
----------------------------------------
原 D-05 是双轨：行为链派生 grade 写 answer_log（调度事实源）；Frank 点选怪兽
标签写 error_type（元认知 + 图鉴）。本引擎加入**第三轨**：

    answer_log.grade      ← 行为链（调度事实源，不变）
    error_type            ← Frank / 家长点选（元认知 + 图鉴，不变）
    diagnosis_type        ← 本引擎（诊断层，驱动 queue_scheduler 权重）★ 新增

三轨各写各的字段，互不覆盖——这正是 `fdl_core/mistakes/attribution.py`
docstring 里"独立记录不互覆盖"原则的延伸。

证据来源（按可用性降级，任一缺失不阻塞）
----------------------------------------
- review_feedback.note / self_rating / duration_seconds（必得）
- 复习反馈录音附件 → ASR 转写（fdl_core.asr，失败静默降级）
- 复习反馈图片附件 → VLM 整页直读（fdl_core.ingest.vlm，失败静默降级）
- 错题元数据（source_ref / 错答 / 正答 / 复现次数 / 既有归因）

离线安全
--------
LLM 不可达（L0 降级）→ 落一条 `status='PROPOSED'`、confidence=0 的提案，
rationale 写明"LLM 不可达，待人工归因"。**绝不抛异常、绝不写坏权威字段**。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from fdl_core.l2.fallback import run_chain
from fdl_core.l2.minimax import MiniMaxClient
from fdl_core.mistakes import attribution_taxonomy as taxonomy
from fdl_core.srs.time_layer import fmt_ts, now_utc

logger = logging.getLogger("fdl.mistakes.attribution_engine")

PROMPT_VERSION = "v1"

# 闸门阈值（高置信自动采纳）。放环境变量便于调参，不改代码。
import os  # noqa: E402 —— 紧邻其服务的常量，有意不置于文件顶部

AUTO_ACCEPT_THRESHOLD: float = float(os.environ.get("FDL_ATTR_AUTO_ACCEPT", "0.80"))

# 2026-09-12 King 拍板：自动写入必须"两次独立分析结论一致"才放行。
# 默认 2 次（每次都是独立 LLM 调用）；env FDL_ATTR_CONFIRM_RUNS 可调，
# 显式设 1 = 回退旧的"单次高置信即自动"语义（不推荐，仅供降级/对照实验）。
AUTO_CONFIRM_RUNS: int = max(1, int(os.environ.get("FDL_ATTR_CONFIRM_RUNS", "2")))

# 复核跑技术性失败的重试次数（2026-09-12 实测修复）：
# LLM 偶发输出不可解析（非语义分歧）时，旧实现直接判"不一致"→ 高置信匹配
# 被误送人工队列。实测 kp_matcher 有 6 条 0.92-0.98 的匹配因 r2 解析失败被
# 误判（r2 的 source_layer=L2.A 但 code=null，即调用成功、输出不可用）。
# 重试只针对"技术性失败"；语义不一致（两次给出不同 code）仍必须转人工。
CONFIRM_RETRY_MAX: int = max(0, int(os.environ.get("FDL_CONFIRM_RETRY", "2")))


def is_technical_failure(run_result: dict | None) -> bool:
    """复核跑是否"技术性失败"——LLM 调用成功但输出不可用。

    与"语义不一致"（两次给出不同的 code）严格区分：
      - 技术性失败（本函数为 True）：输出不可解析/为空 → 重试即可恢复；
      - 语义不一致（本函数为 False）：两次结论不同 → 必须转人工，重试无意义。
    """
    if not run_result:
        return True
    return run_result.get("parsed") is None


# evidence_source 枚举（落 attribution_proposal.evidence_source）
SRC_TEXT = "TEXT_NOTE"
SRC_AUDIO = "AUDIO_ASR"
SRC_IMAGE = "IMAGE_VLM"
SRC_META = "EXISTING_META"

# 提案状态机：PROPOSED → (AUTO_ACCEPTED | ACCEPTED | REJECTED | SUPERSEDED)
STATUS_PROPOSED = "PROPOSED"
STATUS_AUTO = "AUTO_ACCEPTED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_SUPERSEDED = "SUPERSEDED"

# 证据文本上限（LLM 输入与库内 digest 共用，防止超长附件撑爆 prompt / 存储）
_EVIDENCE_MAX_CHARS = 4000

_AUDIO_EXT = {".wav", ".m4a", ".mp3", ".opus", ".aac", ".flac"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".bmp"}


# ── 证据汇集 ────────────────────────────────────────────────
@dataclass
class Evidence:
    """一次归因分析的证据集合。"""

    mistake_id: int
    schedule_id: int | None = None
    feedback_id: int | None = None
    sources: list[str] = field(default_factory=list)  # SRC_* 列表
    text: str = ""  # 拼接后的证据文本（送 LLM）
    digest: str = ""  # 截断版（落库）
    degraded: list[str] = field(default_factory=list)  # 降级/缺失说明


def _resolve_feedback(conn, mistake_id: int, feedback_id: int | None) -> dict | None:
    """定位本次要归因的复习反馈行（含 schedule_id 回填）。

    review_feedback 没有 mistake_id 列，必须经 review_schedule JOIN（仓库既有口径）。
    """
    if feedback_id is not None:
        row = conn.execute(
            "SELECT rf.id, rf.schedule_id, rf.self_rating, rf.duration_seconds,"
            " rf.note, rf.attachments_json, rf.created_at"
            " FROM review_feedback rf WHERE rf.id=?",
            (feedback_id,),
        ).fetchone()
        if row:
            return dict(
                zip(
                    [
                        "id",
                        "schedule_id",
                        "self_rating",
                        "duration_seconds",
                        "note",
                        "attachments_json",
                        "created_at",
                    ],
                    row,
                    strict=False,
                )
            )
        return None
    # 未指定 feedback → 取该错题最近一次反馈
    row = conn.execute(
        "SELECT rf.id, rf.schedule_id, rf.self_rating, rf.duration_seconds,"
        " rf.note, rf.attachments_json, rf.created_at"
        " FROM review_feedback rf"
        " JOIN review_schedule rs ON rs.id = rf.schedule_id"
        " WHERE rs.mistake_id = ?"
        " ORDER BY rf.created_at DESC LIMIT 1",
        (mistake_id,),
    ).fetchone()
    if row:
        return dict(
            zip(
                [
                    "id",
                    "schedule_id",
                    "self_rating",
                    "duration_seconds",
                    "note",
                    "attachments_json",
                    "created_at",
                ],
                row,
                strict=False,
            )
        )
    return None


def _load_mistake(conn, mistake_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, subject, source_ref, error_type, error_subtype, diagnosis_type,"
        " wrong_answer, correct_answer, reappear_count, is_tamed, severity"
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
                "error_type",
                "error_subtype",
                "diagnosis_type",
                "wrong_answer",
                "correct_answer",
                "reappear_count",
                "is_tamed",
                "severity",
            ],
            row,
            strict=False,
        )
    )


def _asr_transcribe(audio_path: str) -> str | None:
    """录音 → 文本。优先 Apple 原生（零依赖），失败返回 None（静默降级）。"""
    try:
        # 延迟导入：模块级 import 会在非 mac 环境/缺依赖时炸掉整个引擎
        from fdl_core.asr.apple import AppleSpeechEngine

        res = AppleSpeechEngine().transcribe(audio_path, lang="zh")
        return (res.text or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ASR 转写失败（降级跳过）：%s", exc)
        return None


def _vlm_describe(image_path: str) -> str | None:
    """图片 → 结构化题目描述。VLM 未启用/失败 → None（静默降级）。"""
    try:
        from fdl_core.ingest import vlm as vlm_mod

        if not vlm_mod.is_enabled():
            return None
        import cv2

        bgr = cv2.imread(image_path)
        if bgr is None:
            return None
        qs = vlm_mod.read_page(bgr)
        if not qs:
            return None
        lines = []
        for q in qs[:10]:  # 防超长：最多取 10 题
            wrong = "错" if q.get("is_wrong") else "对"
            lines.append(
                f"- 题{q.get('no')}: {str(q.get('stem'))[:120]} | "
                f"作答:{str(q.get('answer'))[:60]} | 批改:{wrong} | "
                f"原因:{str(q.get('reason'))[:60]}"
            )
        return "\n".join(lines) or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("VLM 解析失败（降级跳过）：%s", exc)
        return None


def collect_evidence(
    conn,
    mistake_id: int,
    feedback_id: int | None = None,
    *,
    use_asr: bool = True,
    use_vlm: bool = True,
) -> Evidence:
    """汇集一次归因所需的全部证据。任何来源失败都降级，不抛异常。"""
    ev = Evidence(mistake_id=mistake_id, feedback_id=feedback_id)
    mistake = _load_mistake(conn, mistake_id)
    if not mistake:
        ev.degraded.append(f"错题 #{mistake_id} 不存在")
        ev.text = "(无证据)"
        ev.digest = ev.text
        return ev

    fb = _resolve_feedback(conn, mistake_id, feedback_id)
    parts: list[str] = []
    parts.append(f"【错题元数据】来源: {mistake.get('source_ref') or '未知'}")
    ev.sources.append(SRC_META)
    if mistake.get("wrong_answer"):
        parts.append(f"  学生错答: {str(mistake['wrong_answer'])[:200]}")
    if mistake.get("correct_answer"):
        parts.append(f"  正确答案: {str(mistake['correct_answer'])[:200]}")
    parts.append(
        f"  复现次数: {mistake.get('reappear_count')} | "
        f"现有归因: error_type={mistake.get('error_type')} "
        f"diagnosis_type={mistake.get('diagnosis_type')}"
    )

    if fb:
        ev.schedule_id = fb.get("schedule_id")
        ev.feedback_id = fb.get("id")
        parts.append(
            f"【本次复习反馈】(#{fb['id']}) 自评: {fb.get('self_rating')}/4 | "
            f"耗时: {fb.get('duration_seconds')} 秒"
        )
        note = (fb.get("note") or "").strip()
        if note:
            parts.append(f"  文字记录: {note[:1500]}")
            ev.sources.append(SRC_TEXT)

        # 附件 → 音频走 ASR、图片走 VLM（各自独立降级）
        raw = fb.get("attachments_json")
        if raw:
            try:
                atts = json.loads(raw)
            except Exception:  # noqa: BLE001
                atts = []
            if not isinstance(atts, list):
                atts = []
            for a in atts[:5]:  # 防超长
                if not isinstance(a, dict):
                    continue
                path = str(a.get("path") or "")
                ext = Path(path).suffix.lower()
                kind = str(a.get("kind") or "").lower()
                if use_asr and (ext in _AUDIO_EXT or "audio" in kind or "voice" in kind):
                    txt = _asr_transcribe(path) if path and Path(path).exists() else None
                    if txt:
                        parts.append(f"  【录音转写】{txt[:1500]}")
                        ev.sources.append(SRC_AUDIO)
                        ev.has_audio = True
                    else:
                        ev.degraded.append(f"录音转写失败/为空: {path}")
                elif use_vlm and (ext in _IMAGE_EXT or "image" in kind):
                    txt = _vlm_describe(path) if path and Path(path).exists() else None
                    if txt:
                        parts.append(f"  【图片识别(VLM)】\n{txt}")
                        ev.sources.append(SRC_IMAGE)
                        ev.has_image = True
                    else:
                        ev.degraded.append(f"VLM 未启用或解析失败: {path}")
    else:
        parts.append("【本次复习反馈】（无反馈记录 —— 仅凭错题元数据归因）")
        ev.degraded.append("无 review_feedback 记录")

    # 历史提案（让 LLM 知道之前怎么判的，便于"迭代反馈更新归因"）
    hist = conn.execute(
        "SELECT proposed_code, confidence, rationale FROM attribution_proposal"
        " WHERE mistake_id=? ORDER BY created_at DESC LIMIT 3",
        (mistake_id,),
    ).fetchall()
    if hist:
        parts.append("【历史归因提案】")
        for r in hist:
            parts.append(f"  - {r[0]} (置信 {r[1]}): {str(r[2])[:80]}")

    ev.text = "\n".join(parts)
    ev.digest = ev.text[:_EVIDENCE_MAX_CHARS]
    return ev


# ── 提示词（版本化，可迭代）─────────────────────────────────
def build_prompts(entries: dict, ev: Evidence, mistake: dict | None) -> tuple[str, str]:
    """构造 (system_prompt, user_prompt)。类别清单来自 ACTIVE taxonomy（非硬编码）。"""
    system = (
        "你是小学数学错题归因分析师。根据给定的错题元数据与本次复习反馈证据，"
        "判断这道错题最可能的错因类别。\n"
        "规则：\n"
        "1. 只依据给出的证据判断，不要臆测证据之外的内容；\n"
        "2. code 必须逐字使用「可用类别」清单里的写法，不得改写、翻译或自创；\n"
        "3. 置信度如实评估：证据不足或证据相互矛盾时给低值"
        "（低置信会转人工复核，不会被自动采纳，宁低勿高）；\n"
        "4. 只有当所有现有类别都明显不适配、且同类证据反复出现时，才提议 new_category；\n"
        "5. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。\n"
        "输出格式：\n"
        '{"code":"<类别code>","confidence":0.0到1.0,"rationale":"<一句话理由，须引用证据>",'
        '"evidence_quote":"<支撑判断的证据原文片段>","new_category":null,'
        '或 {"code":"<建议新code，用 PARENT.CHILD 格式>","label":"<中文名>",'
        '"rationale":"<为什么现有类别覆盖不了>"}}\n'
        "注意：new_category 只能是 null 或一个对象，二者取其一。"
    )

    cat_lines = []
    for code, e in sorted(entries.items(), key=lambda kv: (-kv[1].weight, kv[0])):
        cat_lines.append(f"- {code} | {e.label} | 权重 {e.weight}")
    if not cat_lines:
        # taxonomy 不可用时的兜底清单（与 BUILTIN_FALLBACK_WEIGHTS 对齐）
        cat_lines = [
            f"- {c} | {c} | 权重 {w}"
            for c, w in sorted(
                taxonomy.BUILTIN_FALLBACK_WEIGHTS.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]

    user = (
        "【可用类别】（code | 中文名 | 优先级权重）\n" + "\n".join(cat_lines) + "\n\n"
        f"【证据】\n{ev.text}\n\n"
        "请输出 JSON。"
    )
    if mistake and mistake.get("diagnosis_type"):
        user += (
            f"\n（提示：该题既有 diagnosis_type={mistake['diagnosis_type']}，"
            "若证据支持可沿用，若证据推翻请给新判断并在 rationale 说明。）"
        )
    return system, user


# ── LLM 输出解析 ────────────────────────────────────────────
def parse_llm_json(answer: str) -> dict | None:
    """从 LLM 回答中提取 JSON 对象。容忍 ```json 包裹 / 前后杂文。解析失败返回 None。"""
    if not answer:
        return None
    text = answer.strip()
    # 去掉可能的 ```json ... ``` 包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    # 先整体尝试
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    # 再尝试截取第一个 {...} 平衡块
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:  # noqa: BLE001
                    return None
    return None


# ── 主入口 ──────────────────────────────────────────────────
def _single_run(system_prompt: str, user_prompt: str, client) -> dict:
    """一次独立的 LLM 归因调用（走降级链 + 解析）。失败不抛，返回可判空 dict。

    为什么抽出来：一致闸门需要对同一证据做多次**独立**调用，每次都是
    全新的采样——这正是"可复现性"的来源。
    """
    chain = run_chain(system_prompt, user_prompt, client=client, version_stamp=PROMPT_VERSION)
    out: dict = {
        "source_layer": chain.source,
        "parsed": None,
        "code": None,
        "conf": 0.0,
        "rationale": "",
        "new_cat": None,
    }
    parsed = parse_llm_json(chain.answer)
    if parsed:
        out["parsed"] = parsed
        out["code"] = str(parsed.get("code") or "").strip()
        out["conf"] = _clamp_conf(parsed.get("confidence"))
        out["rationale"] = str(parsed.get("rationale") or "").strip()[:500]
        out["new_cat"] = parsed.get("new_category") or None
    return out


def analyze_feedback(
    conn,
    mistake_id: int,
    feedback_id: int | None = None,
    *,
    client: MiniMaxClient | None = None,
    use_asr: bool = True,
    use_vlm: bool = True,
    confirm_runs: int | None = None,
) -> dict:
    """对一次复习反馈做 LLM 归因 → 落提案 → 过闸门。

    闸门（2026-09-12 King 拍板）：两次独立分析结论一致才自动写入。
    返回 dict：{ok, proposal_id, status, code, confidence, source_layer, gate,
                confirm, errors}
    任何异常都被捕获并写进 errors —— 本函数绝不抛出（复习主流程不能被归因打断）。
    """
    result: dict = {
        "ok": False,
        "proposal_id": None,
        "status": None,
        "code": None,
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

        ev = collect_evidence(conn, mistake_id, feedback_id, use_asr=use_asr, use_vlm=use_vlm)
        entries = taxonomy.load_active(conn)
        system_prompt, user_prompt = build_prompts(entries, ev, mistake)

        runs = AUTO_CONFIRM_RUNS if confirm_runs is None else max(1, int(confirm_runs))

        # —— 第 1 次分析（权威提案；复核跑只做对照，不另立提案）——
        first = _single_run(system_prompt, user_prompt, client)
        result["source_layer"] = first["source_layer"]

        parsed = first["parsed"]
        if first["source_layer"] == "L0" or parsed is None:
            # LLM 不可达或输出不可解析 → 落 PROPOSED 待人工，绝不猜
            reason = (
                "LLM 不可达（降级至 L0），待人工归因"
                if first["source_layer"] == "L0"
                else f"LLM 输出不可解析（来源 {first['source_layer']}），待人工归因"
            )
            pid = _insert_proposal(
                conn,
                mistake_id=ev.mistake_id,
                schedule_id=ev.schedule_id,
                feedback_id=ev.feedback_id,
                evidence_source=ev.sources[0] if ev.sources else SRC_META,
                evidence_digest=ev.digest,
                model=None,
                prompt_version=PROMPT_VERSION,
                proposed_code=None,
                proposed_new_code=None,
                confidence=0.0,
                rationale=reason,
                alt_json=None,
                source_layer=first["source_layer"],
                status=STATUS_PROPOSED,
            )
            result.update(
                ok=True,
                proposal_id=pid,
                status=STATUS_PROPOSED,
                code=None,
                confidence=0.0,
                gate="manual_queue",
            )
            result["errors"].append(reason)
            return result

        code = first["code"]
        conf = first["conf"]
        rationale = first["rationale"]
        new_cat = first["new_cat"]
        alt = parsed.get("alt") or parsed.get("alternatives") or None

        # 新类别 → 候选通道（King：绝不自动新增）
        new_code = None
        if isinstance(new_cat, dict) and new_cat.get("code"):
            new_code = str(new_cat["code"]).strip()
            cid = taxonomy.register_candidate(
                conn,
                candidate_code=new_code,
                candidate_label=str(new_cat.get("label") or new_code),
                parent_code=str(new_cat.get("parent_code") or "").strip() or None,
                rationale=str(new_cat.get("rationale") or rationale)[:500],
                sample={"mistake_id": mistake_id, "evidence": ev.digest[:300]},
            )
            result["candidate_id"] = cid

        # code 合法性：必须在 ACTIVE taxonomy 内
        valid = bool(code) and taxonomy.is_valid_code(conn, code)
        if code and not valid:
            result["errors"].append(f"LLM 返回的 code='{code}' 不在 ACTIVE taxonomy —— 转人工复核")

        status = STATUS_PROPOSED
        gate = "manual_queue"
        decided_by = None
        confirm_audit: dict | None = None

        if valid and conf >= AUTO_ACCEPT_THRESHOLD and runs > 1:
            # —— 复核跑：独立再分析 runs-1 次，全部一致才放行（King 2026-09-12）——
            runs_log = [
                {"run": 1, "code": code, "confidence": conf, "source_layer": first["source_layer"]}
            ]
            all_agree = True
            for i in range(runs - 1):
                nxt = _single_run(system_prompt, user_prompt, client)
                # 复核跑技术性失败（输出不可解析）→ 重试；语义不一致仍转人工
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
            # 单跑模式（显式 confirm_runs=1）：保留旧"单次高置信即自动"语义
            status, gate, decided_by = STATUS_AUTO, "auto", "LLM_ASSISTED"

        # 审计打包：备选结论 + 每次独立跑的结果（全链可回溯）
        audit: dict = {}
        if alt:
            audit["alternatives"] = alt
        if confirm_audit:
            audit["confirm"] = confirm_audit

        pid = _insert_proposal(
            conn,
            mistake_id=ev.mistake_id,
            schedule_id=ev.schedule_id,
            feedback_id=ev.feedback_id,
            evidence_source=ev.sources[0] if ev.sources else SRC_META,
            evidence_digest=ev.digest,
            model=getattr(client, "model", None) or "MiniMax-M3",
            prompt_version=PROMPT_VERSION,
            proposed_code=code if valid else None,
            proposed_new_code=new_code,
            confidence=conf,
            rationale=rationale,
            alt_json=json.dumps(audit, ensure_ascii=False) if audit else None,
            source_layer=first["source_layer"],
            status=status,
            decided_by=decided_by,
            decided_at=stamp if decided_by else None,
        )
        result.update(
            ok=True,
            proposal_id=pid,
            status=status,
            gate=gate,
            code=code if valid else None,
            confidence=conf,
        )
        if confirm_audit:
            result["confirm"] = confirm_audit

        # 闸门放行：把 LLM 判断写进权威字段（三轨之一，只碰 diagnosis_type）
        if status == STATUS_AUTO:
            applied = _write_authoritative(conn, mistake_id=mistake_id, code=code, confidence=conf)
            result["written"] = applied
        if ev.degraded:
            result["degraded"] = ev.degraded
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("归因分析失败 mistake_id=%s", mistake_id)
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def _clamp_conf(v) -> float:
    """置信度夹到 [0,1]；None/非法 → 0.0。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _insert_proposal(
    conn,
    *,
    mistake_id,
    schedule_id,
    feedback_id,
    evidence_source,
    evidence_digest,
    model,
    prompt_version,
    proposed_code,
    proposed_new_code,
    confidence,
    rationale,
    alt_json,
    source_layer,
    status,
    decided_by=None,
    decided_at=None,
) -> int:
    cur = conn.execute(
        "INSERT INTO attribution_proposal"
        " (mistake_id, schedule_id, feedback_id, evidence_source, evidence_digest,"
        "  model, prompt_version, proposed_code, proposed_new_code, confidence,"
        "  rationale, alt_json, source_layer, status, decided_by, decided_at,"
        "  created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            mistake_id,
            schedule_id,
            feedback_id,
            evidence_source,
            evidence_digest,
            model,
            prompt_version,
            proposed_code,
            proposed_new_code,
            float(confidence),
            rationale,
            alt_json,
            source_layer,
            status,
            decided_by,
            decided_at,
            fmt_ts(now_utc()),
            fmt_ts(now_utc()),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _write_authoritative(conn, *, mistake_id: int, code: str, confidence: float) -> bool:
    """闸门放行后写 diagnosis_type / attributed_by / attribution_confidence。

    只写 diagnosis_type（诊断层），不碰 error_type（Frank/家长元认知轨道）——
    遵守 fdl_core/mistakes/attribution.py 的 D-05"独立记录不互覆盖"。
    """
    cur = conn.execute(
        "UPDATE mistake_record SET diagnosis_type=?, attributed_by='LLM_ASSISTED',"
        " attribution_confidence=?, updated_at=? WHERE id=?",
        (code, float(confidence), fmt_ts(now_utc()), mistake_id),
    )
    conn.commit()
    return bool(cur.rowcount)


# ── 人工复核 API ────────────────────────────────────────────
def list_proposals(conn, *, status: str | None = None, limit: int = 100) -> list[dict]:
    """提案复核队列（默认取全部未决）。"""
    if status:
        rows = conn.execute(
            "SELECT id, mistake_id, feedback_id, proposed_code, proposed_new_code,"
            " confidence, rationale, source_layer, status, created_at"
            " FROM attribution_proposal WHERE status=?"
            " ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, mistake_id, feedback_id, proposed_code, proposed_new_code,"
            " confidence, rationale, source_layer, status, created_at"
            " FROM attribution_proposal ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    keys = [
        "id",
        "mistake_id",
        "feedback_id",
        "proposed_code",
        "proposed_new_code",
        "confidence",
        "rationale",
        "source_layer",
        "status",
        "created_at",
    ]
    return [dict(zip(keys, r, strict=False)) for r in rows]


def accept_proposal(conn, proposal_id: int, *, decided_by: str = "PARENT") -> dict:
    """人工采纳提案 → 写权威字段。低置信转人工后的出口。"""
    row = conn.execute(
        "SELECT mistake_id, proposed_code, confidence, status FROM attribution_proposal WHERE id=?",
        (proposal_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"提案不存在：{proposal_id}"}
    mistake_id, code, conf, status = row
    if status == STATUS_ACCEPTED:
        return {"ok": False, "error": "提案已被采纳"}
    if not code:
        return {"ok": False, "error": "提案无 proposed_code（LLM 不可达/不可解析），无可采纳项"}
    if not taxonomy.is_valid_code(conn, code):
        return {"ok": False, "error": f"code='{code}' 已不在 ACTIVE taxonomy"}
    conn.execute(
        "UPDATE attribution_proposal SET status=?, decided_by=?, decided_at=?, updated_at=?"
        " WHERE id=?",
        (STATUS_ACCEPTED, decided_by, fmt_ts(now_utc()), fmt_ts(now_utc()), proposal_id),
    )
    written = _write_authoritative(conn, mistake_id=mistake_id, code=code, confidence=conf or 0.0)
    return {"ok": True, "written": written, "code": code}


def reject_proposal(
    conn, proposal_id: int, *, decided_by: str = "PARENT", reason: str | None = None
) -> dict:
    """人工驳回提案（保留痕迹）。"""
    stamp = fmt_ts(now_utc())
    cur = conn.execute(
        "UPDATE attribution_proposal SET status=?, decided_by=?, decided_at=?,"
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


# ── 异步触发（复习主流程的挂钩入口）────────────────────────
# 为什么异步：LLM 调用要数秒，同步挂在 mark_reviewed 里会把每次复习拖慢几秒。
# 为什么用独立连接：sqlite3.Connection 默认不可跨线程；子线程自开连接最稳。
# 为什么延迟 1 秒：mark_reviewed 的 mistake_record 重排/reschedule 要到函数末尾
# 才 commit，延迟可显著降低"子线程读到半截事务 + 与主线程抢写锁"的概率。
import threading  # noqa: E402
import time as _time  # noqa: E402

from fdl_core.db.schema import get_connection  # noqa: E402

_ASYNC_DELAY_SEC = 1.0
_INFLIGHT: set[tuple[str, int, int | None]] = set()
_INFLIGHT_LOCK = threading.Lock()


def get_default_client() -> MiniMaxClient:
    """默认 LLM 客户端（opt-out 已确认；key 从 env / config/secrets.yaml 读）。"""
    return MiniMaxClient(data_opt_out=True)


def _db_path_of(conn) -> str:
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                return row[2] or ":memory:"
    except Exception:  # noqa: BLE001
        pass
    return ":memory:"


def schedule_async_analysis(conn, *, mistake_id: int, feedback_id: int | None = None) -> dict:
    """后台线程触发归因分析。**绝不抛异常、绝不阻塞调用方。**

    同一 (db, mistake, feedback) 任务去重：复习流程可能因重试/多入口重复触发，
    重复跑同一份反馈只会浪费 token 且产生冗余提案。
    """
    try:
        db_path = _db_path_of(conn)
        if not db_path or db_path == ":memory:":
            return {"ok": False, "reason": "内存库不支持异步归因（请用同步 analyze_feedback）"}
        key = (db_path, int(mistake_id), feedback_id)
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                return {"ok": False, "reason": "同任务已在跑", "duplicate": True}
            _INFLIGHT.add(key)

        def _work() -> None:
            try:
                _time.sleep(_ASYNC_DELAY_SEC)
                c = get_connection(db_path)
                try:
                    analyze_feedback(c, mistake_id, feedback_id, client=get_default_client())
                finally:
                    c.close()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "异步归因失败（不影响复习主流程）mistake_id=%s",
                    mistake_id,
                    exc_info=True,
                )
            finally:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(key)

        threading.Thread(target=_work, name=f"fdl-attr-{mistake_id}", daemon=True).start()
        return {"ok": True, "async": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("归因调度失败（不影响复习主流程）：%s", exc)
        return {"ok": False, "reason": str(exc)}

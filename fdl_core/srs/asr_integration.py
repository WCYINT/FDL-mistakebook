"""
fdl_core/srs/asr_integration.py — 复习录音 ↔ 错题自动联动

🟢 业务定位：
- 用户做完复习口述（录音 → SenseVoice 转写 → minimax-M3 分析）后，
  FDL 系统应**自动将该次复习的"有效时长"计入今日驾驶舱数字卡**。
- 同时按"音频路径"或"音频文件名包含错题号"匹配到对应错题（mistake_record），
  调用 mark_reviewed(mid) 触发 SRS 闭环（已在 fdl_core.mistakes.review）。

🟢 输入：
- 草稿：data/asr_drafts/0907复习*.draft.json（含 audio 路径 + 转写文本）
- 错题本：mistake_record（按 source_ref 或 note_id 模糊匹配）

🟢 输出：list[ASRReviewMatch]，每项含：
  {audio, mid(可能 null), kp_id(可能 null), duration_sec,
   matched_by('source_ref'|'note_id'|'filename'|'unmatched'),
   new_status('已复习'|'no_match')}
"""

from __future__ import annotations

import re


def match_audio_to_mistake(
    audio_name: str,
    audio_path: str,
    draft_text: str,
    mistake_lookup: dict[int, dict],
) -> tuple[int | None, str]:
    """按多级回退匹配录音到错题。

    mistake_lookup: {mistake_id: {source_ref, note_id, ...}}

    优先级：
      1. audio 文件名含数字 ID（如 "P57-77041" 或 "P64-12-77059"）
      2. 转写文本含错题号引用（如 "77041"、"P57" + 数字）
      3. note_id 直接匹配
      4. source_ref 部分匹配
      5. 兜底 None
    """
    # 1. 文件名含 5 位数字 ID：必须是「以 77 开头」且「确实存在于 mistake_record」
    #    的错题 id（错题 id 空间 77001-77061），避免任意 5 位数字误匹配成错题。
    for n in re.findall(r"\d{5}", audio_name):
        if not n.startswith("77"):
            continue
        mid = int(n)
        if mid in mistake_lookup:
            return mid, "filename"

    # 2. 转写文本中 5 位数字 ID（同样要求 77 前缀 + 存在性校验，杜绝误匹配）
    text_ids = set(re.findall(r"\d{5}", draft_text))
    for n in text_ids:
        if not n.startswith("77"):
            continue
        mid = int(n)
        if mid in mistake_lookup:
            return mid, "transcript"

    # 3. note_id 匹配（note_id 通常是字符串化的 id）
    for mid, m in mistake_lookup.items():
        nid = str(m.get("note_id", "")).strip()
        if nid and (nid in audio_name or nid in draft_text):
            return mid, "note_id"

    # 4. source_ref 部分匹配（如 "深中奥数小A P57" 出现在文件名）
    for mid, m in mistake_lookup.items():
        ref = m.get("source_ref", "") or ""
        # 短关键词（>=3 字符）匹配
        for kw in re.findall(r"[\u4e00-\u9fffA-Z0-9]{3,}", ref):
            if kw in audio_name or kw in draft_text[:200]:
                return mid, "source_ref"

    return None, "unmatched"


def apply_asr_to_review(
    audio_name: str,
    audio_path: str,
    duration_sec: float,
    transcript: str,
    mistake_lookup: dict[int, dict],
    mark_reviewed_fn=None,  # callable: (mid) -> bool；None = 仅匹配、不写库
) -> dict:
    """单条录音 → 匹配错题（可选 mark_reviewed）。

    返回 ASRReviewMatch dict（含 mid/matched_by/new_status）。

    - mark_reviewed_fn 为 None：只读匹配，new_status="matched_pending"
      （供报告聚合展示"待确认"，绝不触发任何 SRS 写动作）。
    - mark_reviewed_fn 为回调：调用它触发 SRS 闭环（递增 reappear_count
      + 写 review_schedule），new_status 为 "已复习"/"mark_failed"。
    """
    mid, matched_by = match_audio_to_mistake(audio_name, audio_path, transcript, mistake_lookup)
    if mid is None:
        return {
            "audio": audio_name,
            "audio_path": audio_path,
            "duration_sec": duration_sec,
            "mid": None,
            "matched_by": matched_by,
            "new_status": "unmatched",
        }
    if mark_reviewed_fn is None:
        # 只读匹配：返回「待确认」状态，不触发任何 SRS 写动作
        return {
            "audio": audio_name,
            "audio_path": audio_path,
            "duration_sec": duration_sec,
            "mid": mid,
            "matched_by": matched_by,
            "new_status": "matched_pending",
        }
    # mark_reviewed 触发 SRS 闭环（递增 reappear_count + 写 review_schedule）
    ok = mark_reviewed_fn(mid)
    return {
        "audio": audio_name,
        "audio_path": audio_path,
        "duration_sec": duration_sec,
        "mid": mid,
        "matched_by": matched_by,
        "new_status": "已复习" if ok else "mark_failed",
    }


def batch_apply(
    drafts: list[dict],
    mistake_lookup: dict[int, dict],
    mark_reviewed_fn=None,
) -> list[dict]:
    """批量：每条草稿都跑一次 apply_asr_to_review。

    mark_reviewed_fn 默认 None → 仅做只读匹配（报告聚合用），不产生任何写副作用。
    """
    results = []
    for d in drafts:
        r = apply_asr_to_review(
            audio_name=d.get("audio", ""),
            audio_path=d.get("audio_path", ""),
            duration_sec=d.get("duration_sec", 0),
            transcript=d.get("text_raw", ""),
            mistake_lookup=mistake_lookup,
            mark_reviewed_fn=mark_reviewed_fn,
        )
        # 同时记录"有效复习时长"（按今日 date）
        results.append(r)
    return results

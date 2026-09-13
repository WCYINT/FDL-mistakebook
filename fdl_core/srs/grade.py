"""Grade 判定（P2-10 / SRS-02）。

范围：**仅键盘/手写/选择**（时长信号可信）；语音模式 Grade 判定依赖 ING-03，
延至阶段三（届时 `time_ratio` 不参与、改用 `self_confidence`）。

规则：
1. `hint_used` → 强制 0（PRD §6.2.5 answer_log 注记）
2. 不正确 → 0
3. Frank 三档自评（UX-02 动作化按钮）：再学一遍→0 / 有点卡→1 / 搞定→2
4. 第 4 档 Easy **由系统派生**，不给按钮（UX-02）：自评"搞定" 且
   time_ratio ≤ EASY_RATIO 且零重试 → 3
5. 防滥用：自评"搞定" 但 time_ratio > SLOW_RATIO → 降 1（Hard）
6. 选择题（CHOICE）无自评：纯系统判定（正确性 + 时长）
"""

from __future__ import annotations

EASY_TIME_RATIO = 0.7  # ≤0.7×预期时长且零重试 → 派生 Easy
SLOW_TIME_RATIO = 2.0  # 自评"搞定"但超 2×预期时长 → 降 Hard

AGAIN, HARD, GOOD, EASY = 0, 1, 2, 3
_TIMED_MODES = ("KEYBOARD", "HANDWRITE", "CHOICE")


def judge_grade(
    *,
    is_correct: bool,
    time_ratio: float | None,
    self_rating: int | None,
    hint_used: bool = False,
    retry_count: int = 0,
    input_mode: str = "KEYBOARD",
) -> int:
    """判定单次作答的 Grade（0/1/2/3）。

    `self_rating`：Frank 三档按钮（0=再学一遍 1=有点卡 2=搞定），CHOICE 模式传 None。
    `time_ratio`：response/expected；None（如语音模式）时退化为纯自评/正误判定。
    """
    if hint_used:
        return AGAIN
    if not is_correct:
        return AGAIN
    if input_mode not in _TIMED_MODES:
        # 语音等未实现模式：有自评用自评，无自评按正确给 Good
        return AGAIN if self_rating is None else min(self_rating, GOOD)

    tr = time_ratio if time_ratio is not None else 1.0
    if self_rating is None:  # 选择题：系统全权判定
        if tr <= EASY_TIME_RATIO and retry_count == 0:
            return EASY
        if tr <= SLOW_TIME_RATIO:
            return GOOD
        return HARD

    if self_rating == GOOD:
        if retry_count == 0 and tr <= EASY_TIME_RATIO:
            return EASY  # 系统派生第 4 档
        if tr > SLOW_TIME_RATIO:
            return HARD  # 超慢"搞定"降级防滥用
        return GOOD
    return min(max(self_rating, AGAIN), HARD)  # 0/1 直通

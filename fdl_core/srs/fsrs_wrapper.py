"""fsrs 调度内核封装（P2-06 / SRS-01）。

对 `fsrs` 6.3.2 的最小封装：
- `review_card(card, rating, at, duration)` → `(new_card, ReviewLog)`
- `to_dict / from_dict` 持久化（库原生支持，往返无损）
- 提取 kp_state 所需字段（S / D / due / last_review）

Card 的 stability（S，天）与 difficulty（D，1–10）直接映射到
`kp_state.stability_days / difficulty`（P2-01 schema）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fsrs import Card, Rating, ReviewLog, Scheduler

from fdl_core.srs.time_layer import now_utc, to_utc

# FSRS rating → PRD grade（0=Again 1=Hard 2=Good 3=Easy）
GRADE_TO_RATING = {
    0: Rating.Again,
    1: Rating.Hard,
    2: Rating.Good,
    3: Rating.Easy,
}


@dataclass
class CardState:
    """从 fsrs Card 提取的持久化字段（对齐 kp_state 列）。"""

    card_id: int
    stability_days: float | None  # S
    difficulty: float | None  # D（1–10）
    due: datetime
    last_review: datetime | None
    raw: dict  # to_dict() 全量（可 from_dict 还原）


def new_card(at: datetime | None = None) -> Card:
    """新建 fsrs Card（due = 当前时刻）。"""
    return Card() if at is None else Card(due=to_utc(at))


def review(
    card: Card,
    grade: int,
    at: datetime | None = None,
    duration_ms: int | None = None,
    scheduler: Scheduler | None = None,
) -> tuple[Card, ReviewLog]:
    """执行一次复习调度，返回 `(new_card, review_log)`。

    `grade`：0=Again 1=Hard 2=Good 3=Easy（PRD 口径，内部映射 fsrs Rating）。
    """
    rating = GRADE_TO_RATING[grade]
    at_utc = to_utc(at) if at is not None else now_utc()
    sched = scheduler or Scheduler()
    return sched.review_card(card, rating, at_utc, duration_ms)


def card_state(card: Card) -> CardState:
    """提取持久化字段；`raw` 可经 `card_from_dict` 完整还原。"""
    return CardState(
        card_id=card.card_id,
        stability_days=card.stability,
        difficulty=card.difficulty,
        due=card.due,
        last_review=card.last_review,
        raw=card.to_dict(),
    )


def card_from_dict(raw: dict) -> Card:
    """从 `CardState.raw` 还原 Card（往返无损）。"""
    return Card.from_dict(raw)

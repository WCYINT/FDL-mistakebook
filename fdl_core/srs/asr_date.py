"""ASR 复习录音归日工具（共享口径）。

🔴 业务定位：口述复习录音是 Frank 当前唯一的复习方式，其"复习日"必须
有**单一权威口径**，否则报告（generate_report）与每日批处理（daily_metric）
会对同一份草稿算出不同日期，导致数字对不上。

口径（统一到此处，daily.py 与 generate_report.py 均调用本函数）：
- 文件名 `MMDD` 前缀优先（业务含义，最稳——不随文件系统操作改变）；
- 解析不到 MMDD 时，回退到草稿 `created_at`（UTC → 本地日期）；
- 都没有则回退到本地今天。

🚫 禁止对 UTC 时间戳做 `[:10]` 切片——一律走 `local_date(parse_ts(...))`。
"""

from __future__ import annotations

import re
from datetime import date

from fdl_core.srs.time_layer import local_date, parse_ts

_MMDD_RE = re.compile(r"(\d{2})(\d{2})")


def draft_review_date(draft: dict, filename: str) -> date | None:
    """返回录音草稿的「复习归日」（本地 date）。

    参数：
      draft:   草稿 dict（至少可能含 `created_at` 字段）
      filename: 草稿文件名（用于提取 MMDD 前缀，如 "0907复习1.draft.json"）

    返回本地 date；若连 created_at / 今天都无法确定则返回 None（极罕见）。
    """
    created_local = None
    if draft.get("created_at"):
        try:
            created_local = local_date(parse_ts(draft["created_at"]))
        except (ValueError, TypeError):
            created_local = None
    m = _MMDD_RE.match(filename)
    if m:
        try:
            return date((created_local or local_date()).year, int(m[1]), int(m[2]))
        except ValueError:
            pass
    return created_local

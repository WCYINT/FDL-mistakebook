"""FDL 数据层权限守门（C7.2，NFR-8）。

权限校验在数据访问层（本模块），不依赖 UI 层（DFD-5）。

黑名单（6 类越权，Agent 绝不可执行）：
- write_sqlite        直接写 SQLite
- read_answer_log     访问 answer_log 明细
- modify_kp_state     修改 kp_state 8 态
- metrics_raw         调用 fdl metrics --raw
- set_evidence_flag   置位 is_valid_evidence
- modify_shared_auth  修改 shared_with_parent

白名单（Agent 允许）：
- write_markdown      Markdown CRUD（knowledge_point/mistake/inquiry）
- call_fdl_cli        受限 fdl CLI 子集
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# 黑名单：6 类越权
FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {
        "write_sqlite",
        "read_answer_log",
        "modify_kp_state",
        "metrics_raw",
        "set_evidence_flag",
        "modify_shared_auth",
    }
)

# 白名单
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "write_markdown",
        "call_fdl_cli",
    }
)


def _audit(audit_dir: Path, action: str, actor: str, allowed: bool) -> None:
    """审计留痕：所有写操作（含被阻断的）写入 logs/audit/auth_access/。"""
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    month = datetime.now().strftime("%Y-%m")
    out = audit_dir / f"agent_actions-{month}.log"
    record = {
        "ts": datetime.now().isoformat(),
        "action": action,
        "actor": actor,
        "allowed": allowed,
    }
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def authorize(action: str, actor: str = "agent", audit_dir: Path | str | None = None) -> bool:
    """判断操作是否被允许。黑名单优先，未声明默认拒绝（最小权限）。"""
    if action in FORBIDDEN_ACTIONS:
        allowed = False
    elif action in ALLOWED_ACTIONS:
        allowed = True
    else:
        allowed = False  # 未声明操作默认拒绝

    if audit_dir is not None:
        _audit(Path(audit_dir), action, actor, allowed)

    return allowed

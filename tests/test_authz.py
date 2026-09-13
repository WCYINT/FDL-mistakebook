"""权限守门单元测试（C7.2，NFR-8）。

验证 DFD-5 黑名单 6 类越权被阻断，白名单 2 类被允许，未声明操作默认拒绝。
"""

from __future__ import annotations

from fdl_core.authz import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS, authorize


def test_six_forbidden_actions_blocked():
    """6 类越权（DFD-5 黑名单）全部被阻断。"""
    assert len(FORBIDDEN_ACTIONS) == 6
    for action in FORBIDDEN_ACTIONS:
        assert authorize(action) is False, f"越权操作 {action} 应被阻断"


def test_two_allowed_actions_permitted():
    """2 类白名单操作被允许。"""
    assert len(ALLOWED_ACTIONS) == 2
    for action in ALLOWED_ACTIONS:
        assert authorize(action) is True, f"白名单操作 {action} 应被允许"


def test_unknown_action_denied_by_default():
    """未声明操作默认拒绝（最小权限）。"""
    assert authorize("delete_everything") is False
    assert authorize("read_anything") is False


def test_audit_written_for_blocked_action(tmp_path):
    """被阻断的操作也写入审计日志。"""
    audit_dir = tmp_path / "auth_access"
    authorize("write_sqlite", actor="agent", audit_dir=audit_dir)
    files = list(audit_dir.glob("agent_actions-*.log"))
    assert files, "被阻断操作应写入审计日志"
    content = files[0].read_text(encoding="utf-8")
    assert "write_sqlite" in content
    assert '"allowed": false' in content

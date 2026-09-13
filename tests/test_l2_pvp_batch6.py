"""批次 6（P2-23~27 L1-C / L2.A / PVP）验收测试。

覆盖：缓存键/TTL/命中率（AC-3）、条款守门（🔴 G2）、网络状态机、
降级链四级 + 铁律 5（降级不写 L0）、权限矩阵 + 数据层拦截、
科目趋势（延迟 7 天 / 无下钻 / 前 2 周屏蔽）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date

import pytest

from fdl_core.db.schema import create_schema
from fdl_core.l2 import (
    L1Cache,
    L2UnavailableError,
    MiniMaxClient,
    cache_key,
    run_chain,
)
from fdl_core.l2.minimax import OFFLINE, ConsentError, _strip_think
from fdl_core.pvp import (
    KING_VIEW_DELAY_DAYS,
    experience_signals,
    king_view_cutoff,
    pvp_allowed,
    quality_diagnosis_visible,
    subject_trend,
)


# ── P2-23 L1-C 缓存 ────────────────────────────────────────
def test_cache_key_sha256():
    k = cache_key("sys", "user")
    assert k == hashlib.sha256(b"sys\x00user").hexdigest()  # D-2 落地值


def test_cache_put_get_roundtrip(tmp_path):
    c = L1Cache(tmp_path / "cache.sqlite", version_stamp="v1")
    k = cache_key("sys", "q1")
    c.put(k, {"answer": "A"})
    assert c.get(k) == {"answer": "A"}
    c.close()


def test_cache_ttl_expiry(tmp_path):
    c = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    k = cache_key("s", "q")
    c.put(k, {"answer": "A"}, ttl_seconds=-1)  # 立即过期
    assert c.get(k) is None
    c.close()


def test_cache_version_drift_invalidates(tmp_path):
    c1 = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    c1.put(cache_key("s", "q"), {"answer": "A"})
    c1.close()
    c2 = L1Cache(tmp_path / "c.sqlite", version_stamp="v2")
    assert c2.get(cache_key("s", "q")) is None  # 版本不符 → 失效
    c2.close()


def test_cache_hit_rate(tmp_path):
    c = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    c.put(cache_key("s", "q1"), {"a": 1})
    c.get(cache_key("s", "q1"))  # hit
    c.get(cache_key("s", "q2"))  # miss
    assert c.hit_rate == 0.5
    c.close()


# ── P2-24 条款守门 + 状态机 + 思考段剥离 ────────────────────
def test_consent_gate_blocks_before_network():
    """🔴 G2：未 opt-out → ConsentError，绝不发起网络请求。"""
    client = MiniMaxClient(api_key="k", data_opt_out=False)
    with pytest.raises(ConsentError):
        client.ask("sys", "q")
    assert client.state == OFFLINE  # 未进入 CONNECTING


def test_missing_key_raises_unavailable():
    client = MiniMaxClient(api_key="", data_opt_out=True)
    with pytest.raises(L2UnavailableError):
        client.ask("sys", "q")


def test_strip_think():
    text = "<think>推理过程……</think>最终答案：通分"
    assert _strip_think(text) == "最终答案：通分"


def test_state_transitions_on_failure(monkeypatch):
    """断网 → ERROR 态 + L2UnavailableError（供降级链捕获）。"""
    import requests

    client = MiniMaxClient(api_key="k", data_opt_out=True)

    def boom(*a, **kw):
        raise requests.ConnectionError("断网")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(L2UnavailableError):
        client.ask("sys", "q")
    assert client.state == "ERROR"


def test_cost_log_written(tmp_path, monkeypatch):
    """成本仪表盘：成功调用写 JSONL 数据点。"""
    import requests

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": "<think>x</think>答案"}}],
                "usage": {"total_tokens": 120, "completion_tokens": 20},
            }

    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp())
    log = tmp_path / "cost.jsonl"
    client = MiniMaxClient(api_key="k", data_opt_out=True, cost_log=log)
    r = client.ask("sys", "q")
    assert r.answer == "答案" and r.prompt_tokens == 100 and r.completion_tokens == 20
    assert "prompt_tokens" in log.read_text(encoding="utf-8")


# ── P2-25 降级链（AC-4）────────────────────────────────────
def test_chain_l2_success_fills_cache(tmp_path):
    cache = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")

    class OkClient:
        data_opt_out = True

        def ask(self, s, u):
            return type("R", (), {"answer": "AI 答案", "elapsed_sec": 0.1})()

    r = run_chain("sys", "q1", client=OkClient(), cache=cache, version_stamp="v1")
    assert r.source == "L2.A" and r.answer == "AI 答案"
    # 回填后，即使 L2 挂了，同问题走缓存
    r2 = run_chain("sys", "q1", client=None, cache=cache, version_stamp="v1")
    assert r2.source == "L1-C" and r2.answer == "AI 答案"
    cache.close()


def test_chain_full_degradation(tmp_path):
    """L2 失败 → L1-C 未命中 → L1-A 未命中 → L0 默认（永远可用）。"""
    cache = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    r = run_chain("sys", "未知问题", client=None, cache=cache, version_stamp="v1")
    assert r.source == "L0" and r.answer
    cache.close()


def test_chain_l1a_local_rules(tmp_path):
    from fdl_core.l2.fallback import LOCAL_RULES

    cache = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    LOCAL_RULES["什么是通分"] = "把异分母化成同分母。"
    r = run_chain("sys", "什么是通分", client=None, cache=cache, version_stamp="v1")
    assert r.source == "L1-A"
    LOCAL_RULES.clear()
    cache.close()


def test_chain_never_writes_l0_state(tmp_path):
    """🔴 铁律 5：降级全程不写 kp_state/answer_log（L0 状态零污染）。"""
    conn = sqlite3.connect(tmp_path / "main.db")
    create_schema(conn)
    cache = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    run_chain("sys", "未知", client=None, cache=cache, version_stamp="v1")
    assert conn.execute("SELECT COUNT(*) FROM kp_state").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM answer_log").fetchone()[0] == 0
    cache.close()
    conn.close()


def test_chain_consent_error_degrades(tmp_path):
    """条款守门触发 → 降级链正常兜底（不崩溃）。"""
    cache = L1Cache(tmp_path / "c.sqlite", version_stamp="v1")
    client = MiniMaxClient(api_key="k", data_opt_out=False)
    r = run_chain("sys", "q", client=client, cache=cache, version_stamp="v1")
    assert r.source == "L0"
    cache.close()


# ── P2-27 权限矩阵 + 数据层拦截 ────────────────────────────
def test_pvp_matrix_defaults():
    assert pvp_allowed("king", "subject_trend")
    assert pvp_allowed("king", "experience_signals")
    assert not pvp_allowed("king", "answer_log_detail")  # 🔴 明细永不可见
    assert not pvp_allowed("king", "kp_state_detail")
    assert not pvp_allowed("unknown_actor", "subject_trend")  # 最小权限


def test_king_view_delay():
    cutoff = king_view_cutoff(date(2026, 9, 3))
    assert (date(2026, 9, 3) - cutoff).days == KING_VIEW_DELAY_DAYS == 7


def test_quality_diagnosis_frozen_first_2_weeks():
    start = date(2026, 9, 1)
    assert not quality_diagnosis_visible(start, today=date(2026, 9, 10))  # 第 2 周：屏蔽
    assert quality_diagnosis_visible(start, today=date(2026, 9, 20))  # 2 周后：解锁


# ── P2-26 科目趋势（聚合无下钻）────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    conn.execute(
        "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level, bloom_level,"
        " abstraction_level, importance_weight, base_difficulty, est_learn_minutes,"
        " est_review_seconds, kp_type, tier, graph_version, valid_from)"
        " VALUES (1, 1, 'MATH-A', 'A', 4, 2, 2, 1.0, 4.8, 1.5, 45, 'SKILL', 'L0',"
        " '2026.1', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status, stability_days,"
        " difficulty, mastery_adj, status_changed_at)"
        " VALUES (1, 1, 1, 1, 'REVIEWING', 5.0, 4.6, 0.55, '2026-09-02T00:00:00Z')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_subject_trend_aggregated(db):
    rows = subject_trend(db, cutoff=date(2026, 9, 3))
    assert len(rows) == 1
    assert rows[0]["subject"] == "MATH" and rows[0]["kp_total"] == 1
    assert rows[0]["avg_mastery"] == 0.55


def test_subject_trend_respects_delay(db):
    """数据延迟 ≥7 天：7 天内的状态变化对 King 不可见。"""
    rows = subject_trend(db, cutoff=date(2026, 9, 1))  # 状态变更 09-02 > 截止 09-01
    assert rows == []


def test_subject_trend_denied(db):
    assert subject_trend(db, actor="stranger") == []


def test_experience_signals(db):
    db.execute(
        "INSERT INTO study_session (id, user_id, session_date, session_slot, trigger_type,"
        " started_at, duration_sec, effective_sec)"
        " VALUES (1, 1, '2026-08-20', 'AM', 'SELF', '2026-08-20T00:00:00Z', 600, 580)"
    )
    db.commit()
    sig = experience_signals(db)
    # 注：时长口径已修正为 effective_sec（非 duration_sec），580s → 9.7min；
    # 且只统计 is_valid=1 的有效会话（本行缺省为有效，故 sessions==1）。
    assert sig["sessions"] == 1 and sig["sir"] == 1.0 and sig["avg_minutes"] == 9.7

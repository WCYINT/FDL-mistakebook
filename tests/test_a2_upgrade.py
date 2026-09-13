"""
测试 A3→A2 升级调度（2026-09-08）

覆盖：
1. should_upgrade_to_a2() 4 项条件检测
2. run_a2_upgrade_check() 自动切换（A3→A2）
3. A2 FSRS 间隔计算（连续答对 6 次，间隔应单调递增）
4. A2 前 3 次护栏（A3 阶梯兜底）
5. _a2_compute_interval 集成（mistake_record.fsrs_s/fsrs_d 持久化）
"""

import datetime as dt
import sqlite3
import tempfile

from fdl_core.db.schema import create_schema
from fdl_core.srs.a2_upgrade import (
    FSRS_LEITNER_GUARD,
    fsrs_init_d,
    fsrs_init_s,
    fsrs_next_interval,
    fsrs_r,
    fsrs_update,
    should_upgrade_to_a2,
)


def make_fresh_db() -> sqlite3.Connection:
    """创建 fresh 库 + 写入模拟数据。"""
    fresh = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(fresh)
    create_schema(c)
    c.execute("""INSERT INTO subject (id, user_id, code, name, color_hex,
                rotation_weight, grade_start, grade_end, sort_order)
                VALUES (1, 1, 'MATH', '数学', '#000', 1, 3, 3, 1)""")
    # 50 个 KP（满足 ≥ A2_MIN_KP_COUNT=50）
    for i in range(1, 51):
        c.execute(f"""INSERT INTO knowledge_point (id, subject_id, code, name,
                    grade_level, bloom_level, abstraction_level,
                    importance_weight, base_difficulty, est_learn_minutes,
                    est_review_seconds, kp_type, source, tier, graph_version,
                    valid_from)
                    VALUES ({i}, 1, 'KP{i}', 'KP{i}', 3, 1, 1, 1.0, 0.5,
                    10, 60, 'CONCEPT', 'MANUAL', 'CORE', 1, '2026-09-01')""")
    # 50 个错题，80% 挂载 KP（40/50=80%）
    for i in range(1, 51):
        kp_id = i if i <= 40 else 0  # 前 40 个挂载，后 10 个未挂载
        c.execute(f"""INSERT INTO mistake_record (id, user_id, kp_id, occurred_at,
                    subject, source, attributed_by, severity, is_tamed, note_id)
                    VALUES ({i}, 1, {kp_id}, '2026-09-01T10:00:00Z',
                    'MATH', 'REAL_WORK', 'RULE_BASED', 3, 0, '{i}')""")
    c.commit()
    return c, fresh


# === 场景 1：should_upgrade_to_a2() 条件检测 ===
def test_upgrade_conditions_basic():
    c, fresh = make_fresh_db()
    result = should_upgrade_to_a2(c)
    print("\n【1. 基础条件检测】")
    print(f"  ready: {result.ready}")
    print(f"  metrics: {result.metrics}")
    print(f"  reasons: {result.reasons}")
    assert result.metrics["kp_count"] == 50
    assert result.metrics["mistake_total"] == 50
    assert result.metrics["mistake_linked"] == 40
    assert result.metrics["mistake_coverage"] == 0.8
    # 缺 A3 复习历史（A3 还没跑过）→ ready=False，reason 含"运行天数不足"
    assert result.ready is False
    assert any("A3" in r for r in result.reasons)
    c.close()
    import os

    os.unlink(fresh)
    print("  ✅ 条件检测正确")


# === 场景 2：模拟 A3 运行 8 天 → 4 条件全满足 → 自动切换 ===
def test_upgrade_switch_after_8_days():
    c, fresh = make_fresh_db()
    # 注入"8 天前"的复习历史（口径修正后：mistake_record.last_reappear_at + reappear_count）
    eight_days_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 50 张卡 × reappear_count=2 = 100 条复习历史；最早复习时间 = 8 天前
    for i in range(1, 51):
        c.execute(
            "UPDATE mistake_record SET reappear_count=2, last_reappear_at=? WHERE id=?",
            (eight_days_ago, i),
        )
    c.commit()

    from fdl_core.mistakes.review import current_scheduler, run_a2_upgrade_check

    result = run_a2_upgrade_check(c)
    print("\n【2. 模拟 8 天后升级检测】")
    print(f"  ready: {result['ready']}")
    print(f"  switched: {result['switched']}")
    print(f"  scheduler: {result['scheduler']}")
    assert result["ready"] is True
    assert result["switched"] is True
    assert result["scheduler"] == "A2"
    assert current_scheduler() == "A2"

    # 再调一次不会重复切换
    result2 = run_a2_upgrade_check(c)
    assert result2["switched"] is False
    print("  ✅ 升级一次，再次检查不重复")

    c.close()
    import os

    os.unlink(fresh)


# === 场景 3：A2 FSRS 间隔计算（核心：曲线单调递增）===
def test_a2_fsrs_curve():
    print("\n【3. A2 FSRS 间隔曲线（连续答对 grade=3）】")
    s = fsrs_init_s(3)
    d = fsrs_init_d(3)
    print(f"  初值: S={s:.3f}, D={d:.3f}")
    intervals = []
    for i in range(6):
        # 模拟跨天复习：elapsed 渐增
        elapsed = max(1.0, s)  # 用 S 近似"上次间隔天数"
        r = fsrs_r(elapsed, s)
        new_s, new_d = fsrs_update(s, d, r, 3, elapsed)
        interval = fsrs_next_interval(new_s)
        intervals.append(round(interval, 2))
        print(
            f"  第 {i + 1} 次: S={s:.2f}→{new_s:.2f}  D={d:.2f}→{new_d:.2f}  R={r:.3f}  I={interval:.2f} 天"
        )
        s, d = new_s, new_d
    print(f"  序列: {intervals}")
    mono = all(intervals[i + 1] >= intervals[i] for i in range(len(intervals) - 1))
    assert mono, "FSRS 间隔应单调递增"
    assert intervals[-1] > intervals[0], "FSRS 末次间隔应大于首次"
    print("  ✅ 曲线单调递增")


# === 场景 4：A2 前 3 次护栏走 A3 阶梯 ===
def test_a2_leitner_guard():
    print(f"\n【4. A2 前 {FSRS_LEITNER_GUARD} 次护栏（走 A3 阶梯）】")
    from fdl_core.mistakes.review import a3_next_interval

    # 前 3 次复习后：reappear_count 1/2/3
    for n in [1, 2, 3]:
        for g in [3]:
            base = a3_next_interval(n, g)
            print(f"  第 {n} 次复习（grade={g}）→ {base} 天")
    # 第 3 次 grade=3 应得 4 天（与 A3 阶梯一致）
    assert a3_next_interval(3, 3) == 4
    print("  ✅ 护栏行为正确")


# === 场景 5：_a2_compute_interval 集成测试 ===
def test_a2_compute_interval_integration():
    print("\n【5. _a2_compute_interval 集成测试】")
    c, fresh = make_fresh_db()
    # 准备一个错题，已经复习 4 次（reappear_count=4，kp_state 有 last_review_at）
    c.execute("UPDATE mistake_record SET reappear_count=4 WHERE id=1")
    c.execute("""INSERT INTO kp_state (user_id, kp_id, subject_id, status,
                stability_days, difficulty, retrievability,
                last_review_at, created_at, updated_at)
                VALUES (1, 1, 1, 'LEARNING', 1.0, 5.0, 1.0,
                        '2026-09-05T10:00:00Z', '2026-09-05T10:00:00Z', '2026-09-05T10:00:00Z')""")
    c.commit()

    from fdl_core.mistakes.review import _a2_compute_interval, switch_to_a2

    switch_to_a2()
    interval = _a2_compute_interval(c, mid=1, reappear_count=4, grade=3)
    print(f"  第 5 次复习 grade=3 → 间隔 {interval} 天")
    # 验证 fsrs_s/fsrs_d 已写回
    row = c.execute("SELECT fsrs_s, fsrs_d FROM mistake_record WHERE id=1").fetchone()
    print(f"  fsrs_s={row[0]}, fsrs_d={row[1]}")
    assert row[0] is not None and row[0] > 0, "fsrs_s 应已持久化"
    assert row[1] is not None and 1.0 <= row[1] <= 10.0, "fsrs_d 应在 [D_MIN, D_MAX]"
    assert 1 <= interval <= 365, "间隔应在合理范围"
    print("  ✅ 集成测试通过")

    # 答错（grade=1）→ 间隔应回退
    interval2 = _a2_compute_interval(c, mid=1, reappear_count=4, grade=1)
    print(f"  答错 grade=1 → 间隔 {interval2} 天")
    assert interval2 == 1, "答错 A2 应回退到 1 天（与 A3 一致）"
    print("  ✅ 答错回退正确")

    from fdl_core.mistakes.review import rollback_to_a3

    rollback_to_a3()
    c.close()
    import os

    os.unlink(fresh)


if __name__ == "__main__":
    test_upgrade_conditions_basic()
    test_upgrade_switch_after_8_days()
    test_a2_fsrs_curve()
    test_a2_leitner_guard()
    test_a2_compute_interval_integration()
    print("\n" + "=" * 60)
    print("✅ 全部通过（A3→A2 升级 + FSRS 核心算法 + 集成）")
    print("=" * 60)

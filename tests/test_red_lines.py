"""三条红线告警占位单元测试（C6.3）。"""

from __future__ import annotations

from fdl_core.alerts.red_lines import (
    alert_duration_out_of_control,
    alert_frank_refusal,
    alert_pass_rate_collapse,
)


def test_three_red_lines_write_alert_files(tmp_alerts_dir):
    """三个占位函数触发写入 logs/alerts/ 不报错。"""
    alert_frank_refusal(tmp_alerts_dir, days_refused=3, sir=0.15)
    alert_duration_out_of_control(tmp_alerts_dir, streak_days=5, minutes=30.0)
    alert_pass_rate_collapse(tmp_alerts_dir, rpr=0.65)

    files = list(tmp_alerts_dir.glob("*.jsonl"))
    assert len(files) == 3, "三条红线各生成一个告警文件"
    names = {f.name.split("-")[0] for f in files}
    assert names == {"frank_refusal", "duration_out_of_control", "pass_rate_collapse"}

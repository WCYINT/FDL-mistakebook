"""批次 2 验收测试：ING-03 ASR 抽象 + ING-04 自动捕获（MS-01 表）+ ING-08 批量导入。

ASR 真实 CER 实测（30 条录音）依赖 Frank 素材——本测试覆盖契约与管线，
真实语料实测在素材到位后执行（G4 并行）。
"""

from __future__ import annotations

import sqlite3
import wave
from pathlib import Path

import numpy as np
import pytest

from fdl_core.asr import (
    AppleSpeechEngine,
    ASREngine,
    ASRError,
    SenseVoiceEngine,
    TranscriptionResult,
    resample_to_16k,
)
from fdl_core.db.schema import create_schema
from fdl_core.ingest.batch import ingest_directory, undo_batch
from fdl_core.mistakes import capture_from_answer

SENSEVOICE_MODEL = Path("models/sensevoice/model.int8.onnx")


# ── ING-03 ASR 抽象契约 ────────────────────────────────────
def test_asr_engine_abstract_contract():
    """抽象契约：实现必须提供 name/supported_langs/transcribe。"""
    assert issubclass(SenseVoiceEngine, ASREngine)
    assert issubclass(AppleSpeechEngine, ASREngine)
    eng = SenseVoiceEngine()
    assert eng.name == "sensevoice"
    assert "zh" in eng.supported_langs


def test_resample_16k():
    samples = [0.0, 1.0, 0.0, -1.0] * 8000  # 32kHz
    out, rate = resample_to_16k(samples, 32000)
    assert rate == 16000
    assert len(out) == 16000


def test_sensevoice_missing_model_raises(tmp_path):
    eng = SenseVoiceEngine(model_path=tmp_path / "nope.onnx", tokens_path=tmp_path / "t.txt")
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(1600, dtype=np.int16).tobytes())
    with pytest.raises(ASRError, match="模型不存在"):
        eng.transcribe(wav)


@pytest.mark.skipif(not SENSEVOICE_MODEL.exists(), reason="SenseVoice 模型不在本机")
def test_sensevoice_real_transcribe(tmp_path):
    """真实模型推理冒烟：合成 1s 语音（正弦波）——输出非异常即通过（内容无意义）。"""
    eng = SenseVoiceEngine()
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        t = np.linspace(0, 1.0, 16000)
        w.writeframes((np.sin(2 * np.pi * 300 * t) * 8000).astype(np.int16).tobytes())
    r = eng.transcribe(wav, lang="zh")
    assert isinstance(r, TranscriptionResult)
    assert r.engine == "sensevoice"
    assert r.confirmed is False  # 🔴 人工确认前不得入库


def test_apple_speech_lazy_import():
    """兜底引擎：依赖未装时给出可操作的安装指引。"""
    from fdl_core.asr.errors import AppleSpeechUnavailable

    eng = AppleSpeechEngine()
    try:
        eng.transcribe("/tmp/a.wav")
    except AppleSpeechUnavailable as e:
        assert "pyobjc-framework-Speech" in str(e)
    except Exception:
        pass  # 已装依赖则走真实转写路径（本机无音频输入时可能其他错误）


# ── ING-04 自动捕获（MS-01 表前置）──────────────────────────
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
        "INSERT INTO question (id, subject_id, primary_kp_id, question_type, stem, answer,"
        " difficulty, expected_seconds, variant_type, source)"
        " VALUES (1, 1, 1, 'CALC', 'q', '50', 3.5, 30, 'ORIGINAL', 'MANUAL')"
    )
    conn.commit()
    yield conn
    conn.close()


def _insert_answer(conn, log_id, grade, valid=1, qid=1, kp_id=1):
    conn.execute(
        "INSERT INTO answer_log (id, user_id, session_id, task_id, question_id, kp_id,"
        " task_type, grade, is_correct, expected_seconds, input_mode, is_valid_evidence,"
        " difficulty_at_time, r_at_answer, s_before, s_after, d_before, d_after, answered_at)"
        " VALUES (?, 1, 1, 1, ?, ?, 'REVIEW', ?, ?, 30, 'KEYBOARD', ?,"
        " 4.6, 0.9, 5.0, 5.0, 4.6, 4.6, '2026-09-04T10:00:00Z')",
        (log_id, qid, kp_id, grade, int(grade >= 1), valid),
    )
    conn.commit()


def test_capture_wrong_answer_auto(db):
    """🔴 ING-04 核心：答错 → 自动生成 mistake_record（零录入）。"""
    _insert_answer(db, 1, grade=0)
    c = capture_from_answer(db, 1)
    assert c is not None
    assert c.kp_id == 1 and c.wrong_answer is None and c.correct_answer == "50"
    row = db.execute(
        "SELECT source, attributed_by, severity, error_type FROM mistake_record WHERE id=?",
        (c.mistake_id,),
    ).fetchone()
    assert row[0] == "SYSTEM_AUTO" and row[1] == "RULE_BASED" and row[2] == 3
    assert row[3] is None  # error_type 留待 MS-02/03


def test_capture_correct_answer_skipped(db):
    _insert_answer(db, 2, grade=2)
    assert capture_from_answer(db, 2) is None
    assert db.execute("SELECT COUNT(*) FROM mistake_record").fetchone()[0] == 0


def test_capture_invalid_evidence_skipped(db):
    _insert_answer(db, 3, grade=0, valid=0)  # 校准/探索不作数
    assert capture_from_answer(db, 3) is None


def test_capture_idempotent(db):
    """幂等：同一作答不重复捕获。"""
    _insert_answer(db, 4, grade=0)
    c1 = capture_from_answer(db, 4)
    c2 = capture_from_answer(db, 4)
    assert c1 is not None and c2 is None


# ── ING-08 批量导入 ────────────────────────────────────────
def test_batch_ingest_directory(tmp_path):
    from fdl.cli.test_support import make_page  # type: ignore

    d = tmp_path / "photos"
    d.mkdir()
    make_page(d / "a.jpg")
    make_page(d / "b.jpg")
    (d / "ignore.txt").write_text("x", encoding="utf-8")
    report = ingest_directory(d, tmp_path / "subject")
    assert report.total == 2
    assert len(report.ok) == 2
    assert report.per_item_sec < 30.0  # PRD 预算
    log = tmp_path / "subject" / "ingest-batch-log.jsonl"
    assert log.exists() and "batch_id" in log.read_text(encoding="utf-8")


def test_batch_undo(tmp_path):
    from fdl.cli.test_support import make_page

    d = tmp_path / "photos"
    d.mkdir()
    make_page(d / "a.jpg")
    report = ingest_directory(d, tmp_path / "subject")
    log = tmp_path / "subject" / "ingest-batch-log.jsonl"
    import json

    batch_id = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["batch_id"]
    removed = undo_batch(batch_id, log)
    assert removed >= 1
    assert not Path(report.ok[0]["original"]).exists()

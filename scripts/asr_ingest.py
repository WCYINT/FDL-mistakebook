#!/usr/bin/env python3
"""复习录音摄入管线（ASR → 结构化分析 → 草稿落盘）。

把「复习录音如何进入 FDL」变成一条可复跑的命令，替代此前"人工逐条转写"
的临时做法（2026-09-12 建立）。

管线（与既有下游约定一致）
--------------------------
    <音频文件>
      → ① SenseVoice 转写（本地离线，sherpa-onnx）
      → ② minimax-M3 结构化分析（学科/知识点/错因/掌握度三维/自评/关键词）
      → ③ 落草稿 data/asr_drafts/<MMDD>复习<N>.draft.json
            + 分析 data/asr_drafts/analyses/<同名>.analysis.json

下游自动接续（无需本脚本再做）：
    - `generate_report.py`（报告展示）与 `daily.py`（复习分钟聚合）读草稿；
    - `scripts/apply_asr_reviews.py` 把草稿匹配到错题并触发 SRS 闭环。

命名规则（与 asr_date 归日口径绑定）
------------------------------------
草稿文件名必须以 `MMDD` 开头（提取"复习业务日"）；音频原名保留在
draft["audio"] 字段——匹配器优先用文件名里的 77xxx 错题号命中错题。

用法
----
    # 处理指定文件（推荐：只给它今天的新录音）
    python scripts/asr_ingest.py "/path/a.m4a" "/path/b.m4a"

    # 扫描目录（跳过已摄入的音频）
    python scripts/asr_ingest.py --dir "~/错题照片文件夹/05-复习"

    # 只转写不分析（LLM 不可达时降级）
    python scripts/asr_ingest.py --dir <dir> --no-analyze

安全
----
- 幂等：音频名已存在于任何草稿 → 跳过（重跑安全）；
- 已存在同名草稿文件 → 跳过不覆盖；
- LLM 不可达 → 草稿仍落盘（analysis 留待后续补齐），不丢转写成果。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.asr.sensevoice import SenseVoiceEngine  # noqa: E402
from fdl_core.l2.fallback import run_chain  # noqa: E402
from fdl_core.mistakes.attribution_engine import (  # noqa: E402
    get_default_client,
    parse_llm_json,
)
from fdl_core.srs.time_layer import fmt_ts, local_date, now_utc  # noqa: E402

DRAFTS_DIR = ROOT / "data" / "asr_drafts"
ANALYSES_DIR = DRAFTS_DIR / "analyses"
AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".aac", ".flac")

PROMPT_VERSION = "asr-review-v1"


# ── ① 转写 ───────────────────────────────────────────────────
def transcribe(audio_path: Path) -> dict:
    """SenseVoice 转写 → 草稿 dict（字段与既有 0907 批次一致）。"""
    engine = SenseVoiceEngine()
    r = engine.transcribe(audio_path)
    return {
        "audio": audio_path.name,
        "audio_path": str(audio_path),
        "size_bytes": audio_path.stat().st_size,
        "engine": engine.name,
        "lang": "zh",
        "duration_sec": round(float(r.duration_sec), 2),
        "text_raw": r.text,
        "segments": [],
        "confirmed": False,
        "created_at": fmt_ts(now_utc()),
    }


# ── ② 结构化分析（LLM）────────────────────────────────────────
_SYSTEM = (
    "你是小学数学复习录音的结构化分析员。输入是家长/老师与学生的复习对话转写"
    "（口语、可能含重复与语气词）。请提取结构化信息：\n"
    "1. subject：学科码（MATH/CHINESE/ENGLISH/SCIENCE）。\n"
    "2. kps：涉及的知识点（name 用课标通用名，confidence 1-3，3=明确考查）。\n"
    "3. errors：本次暴露的错误（description 一句话，severity 1-3）。\n"
    "4. mastery：三维掌握度——familiar（熟练）/ confused（混淆）/ "
    "needs_practice（待练习），每项为字符串数组（各 0-3 条）。\n"
    '5. self_evaluation：学生自评（若转写中没有则给""）。\n'
    "6. keywords：检索关键词（3-6 个）。\n"
    "只输出一个 JSON 对象，不要 markdown 代码块：\n"
    '{"subject":"MATH","kps":[{"name":"...","confidence":3}],'
    '"errors":[{"description":"...","severity":2}],'
    '"mastery":{"familiar":[],"confused":[],"needs_practice":[]},'
    '"self_evaluation":"...","keywords":[]}'
)


def analyze(draft: dict, client) -> dict | None:
    """LLM 结构化分析；不可达/不可解析返回 None（草稿仍保留）。"""
    user = f"【复习录音转写】\n{draft.get('text_raw', '')}\n\n请结构化分析。"
    chain = run_chain(_SYSTEM, user, client=client, version_stamp=PROMPT_VERSION)
    if chain.source == "L0":
        return None
    parsed = parse_llm_json(chain.answer)
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("mastery"), dict):
        parsed["mastery"] = {"familiar": [], "confused": [], "needs_practice": []}
    return parsed


# ── ③ 落盘 ───────────────────────────────────────────────────
def _existing_audio_names() -> set[str]:
    """已摄入的音频名集合（幂等判据）。"""
    names: set[str] = set()
    if not DRAFTS_DIR.exists():
        return names
    for f in DRAFTS_DIR.glob("*.draft.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("audio"):
            names.add(str(d["audio"]))
    return names


def _next_draft_name(day_prefix: str) -> str:
    """同一天的下一个序号：`<MMDD>复习<N>.draft.json`（N 从 1 递增，不重复）。"""
    n = 1
    while (DRAFTS_DIR / f"{day_prefix}复习{n}.draft.json").exists():
        n += 1
    return f"{day_prefix}复习{n}"


def ingest_one(audio_path: Path, *, analyze_llm: bool, client) -> dict:
    """单文件全流程。返回 {ok, draft_path, analysis, reason}。"""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return {"ok": False, "reason": f"文件不存在：{audio_path}"}
    if str(audio_path.name) in _existing_audio_names():
        return {"ok": True, "skipped": True, "reason": "已摄入（草稿已存在）"}

    draft = transcribe(audio_path)
    # 归日：优先用音频 mtime（录音业务日）→ 兜底本地今天
    try:
        day = datetime.fromtimestamp(audio_path.stat().st_mtime).date()
    except OSError:
        day = local_date()
    name = _next_draft_name(f"{day:%m%d}")
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS_DIR / f"{name}.draft.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    analysis_written = False
    if analyze_llm:
        try:
            a = analyze(draft, client)
        except Exception as exc:  # noqa: BLE001 — 分析失败不丢转写
            a = None
            draft["_analysis_error"] = f"{type(exc).__name__}: {exc}"
        if a is not None:
            ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
            full = dict(draft)
            full["analysis"] = a
            full["analyzed_at"] = fmt_ts(now_utc())
            (ANALYSES_DIR / f"{name}.draft.analysis.json").write_text(
                json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            analysis_written = True

    return {
        "ok": True,
        "draft_path": str(draft_path),
        "name": name,
        "duration_sec": draft["duration_sec"],
        "chars": len(draft["text_raw"]),
        "analysis_written": analysis_written,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="复习录音摄入（ASR → 分析 → 草稿）")
    p.add_argument("files", nargs="*", help="音频文件路径（可多个）")
    p.add_argument("--dir", default=None, help="扫描目录（跳过已摄入）")
    p.add_argument("--no-analyze", action="store_true", help="只转写不调 LLM")
    args = p.parse_args(argv)

    targets: list[Path] = [Path(f) for f in args.files]
    if args.dir:
        d = Path(args.dir)
        if not d.exists():
            print(f"[asr-ingest] 目录不存在：{d}", file=sys.stderr)
            return 1
        targets += sorted(
            f for f in d.iterdir() if f.suffix.lower() in AUDIO_EXTS and not f.name.startswith(".")
        )
    if not targets:
        print("用法：asr_ingest.py <files...> ｜ --dir <dir> [--no-analyze]")
        return 0

    client = None if args.no_analyze else get_default_client()
    ok = skipped = failed = 0
    for t in targets:
        r = ingest_one(t, analyze_llm=not args.no_analyze, client=client)
        if r.get("ok") and r.get("skipped"):
            skipped += 1
            print(f"  [跳过] {t.name}（{r['reason']}）")
        elif r.get("ok"):
            ok += 1
            print(
                f"  [完成] {t.name} → {r['name']}"
                f"（{r['duration_sec']}s / {r['chars']} 字）"
                f"{' + 分析' if r['analysis_written'] else '（未分析）'}"
            )
        else:
            failed += 1
            print(f"  [失败] {t.name}：{r['reason']}", file=sys.stderr)

    print(f"\n[asr-ingest] 完成：新增 {ok} | 跳过 {skipped} | 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

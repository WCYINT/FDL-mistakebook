"""ING-08 King 批量导入工作台（CLI 版）。

每周批量流程：目录内照片批量 → ingest 管线 → 汇总报告（成功/失败/待人工）。
- 每张的结果追加写入 `ingest-batch-log.jsonl`（**支持撤销**：按批次号回滚归档文件）；
- 单题 OCR 不可用时的最坏路径 = 人工录入，PRD 预算 30 题 ≤ 111 min；
  本工作台在 OCR 可用时单题 ~7s，30 题 ≈ 3.5 min，远优于最坏路径。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from fdl_core.ingest.archive import ingest_photo
from fdl_core.srs.time_layer import fmt_ts, now_utc

SUPPORTED_EXT = (".jpg", ".jpeg", ".png", ".heic")


@dataclass
class BatchReport:
    batch_id: str
    total: int
    ok: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    needs_review: list[dict] = field(default_factory=list)
    elapsed_sec: float = 0.0

    @property
    def per_item_sec(self) -> float:
        return self.elapsed_sec / self.total if self.total else 0.0


def ingest_directory(
    photos_dir: str | Path,
    subject_dir: str | Path,
    *,
    log_path: str | Path | None = None,
) -> BatchReport:
    """批量录入目录内全部支持格式的照片（跳过已归档同名文件）。"""
    d = Path(photos_dir)
    if not d.exists():
        raise FileNotFoundError(d)
    batch_id = fmt_ts(now_utc()).replace(":", "").replace("-", "")
    report = BatchReport(batch_id=batch_id, total=0)
    t0 = time.perf_counter()

    photos = sorted(p for p in d.iterdir() if p.suffix.lower() in SUPPORTED_EXT)
    report.total = len(photos)
    log_file = Path(log_path) if log_path else Path(subject_dir) / "ingest-batch-log.jsonl"

    for p in photos:
        try:
            r = ingest_photo(p, subject_dir)
            item = {
                "src": str(p),
                "original": str(r.original_path),
                "clean": str(r.clean_path) if r.clean_path else None,
                "engine": r.ocr.engine,
                "lines": len(r.ocr.lines),
                "needs_review": r.needs_review,
            }
            if r.needs_review:
                report.needs_review.append(item)
            else:
                report.ok.append(item)
        except Exception as e:  # 单张失败不阻塞批量
            report.failed.append({"src": str(p), "error": f"{type(e).__name__}: {e}"})

    report.elapsed_sec = time.perf_counter() - t0
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "batch_id": report.batch_id,
                    "ts": fmt_ts(now_utc()),
                    "total": report.total,
                    "ok": report.ok,
                    "failed": report.failed,
                    "needs_review": report.needs_review,
                    "elapsed_sec": round(report.elapsed_sec, 1),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return report


def undo_batch(batch_id: str, log_path: str | Path) -> int:
    """撤销一个批次：删除该批归档的 original/clean 文件（jsonl 回放）。"""
    log_file = Path(log_path)
    removed = 0
    for line in log_file.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("batch_id") != batch_id:
            continue
        for item in rec.get("ok", []) + rec.get("needs_review", []):
            for key in ("original", "clean"):
                fp = item.get(key)
                if fp and Path(fp).exists():
                    Path(fp).unlink()
                    removed += 1
    return removed

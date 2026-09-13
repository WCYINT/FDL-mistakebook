"""fdl CLI 入口（C7.1）。

6 子命令骨架：ingest / review / metrics / doctor / backup / restore。
阶段一仅提供可运行骨架 + 权限守门，实际逻辑由阶段二起填充。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fdl_core import __version__
from fdl_core.authz import authorize


def _run(handler_name: str, action: str) -> int:
    """权限守门 + 调用子命令处理函数。"""
    if not authorize(action, actor="king"):
        print(f"[fdl] 拒绝执行 {handler_name}（数据层权限守门拦截）", file=sys.stderr)
        return 1
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """录入真实学习物（阶段三 ING 主链路，T3-05 实装）。

    退出码：0 成功 · 1 管线执行异常 · 2 文件/参数错误。
    """
    code = _run("ingest", "write_markdown")
    if code != 0:
        return code

    image = Path(args.image)
    if not image.exists():
        print(f"[fdl ingest] 文件不存在：{image}", file=sys.stderr)
        return 2
    if image.suffix.lower() not in (".jpg", ".jpeg", ".png", ".heic"):
        print(
            f"[fdl ingest] 不支持的图片格式 {image.suffix}（支持 jpg/jpeg/png/heic）",
            file=sys.stderr,
        )
        return 2

    from fdl_core.ingest.archive import ingest_photo
    from fdl_core.ingest.errors import IngestError, OriginalProtectedError

    subject_dir = Path(args.subject_dir)
    title = args.title or image.name
    clean_out = args.clean_out or str(
        subject_dir / "04-真实学习物" / "clean" / f"{Path(title).stem}-clean.jpg"
    )
    try:
        r = ingest_photo(
            image,
            subject_dir,
            title=title,
            clean_out=clean_out,
            force_engine=None if args.engine == "auto" else args.engine,
        )
    except OriginalProtectedError as e:
        print(f"[fdl ingest] 🔴 ING-06 原图保护拦截：{e}", file=sys.stderr)
        return 1
    except (IngestError, OSError) as e:
        print(f"[fdl ingest] 管线执行失败：{e}", file=sys.stderr)
        return 1

    print(f"[fdl ingest] 录入完成（引擎={r.ocr.engine}，耗时 {r.elapsed_sec:.1f}s）")
    print(f"  归档原图：{r.original_path}")
    if r.clean_path:
        print(f"  空白重做卷：{r.clean_path}")
    else:
        print("  空白重做卷：未生成（擦除失败走退化路径，保留原图 + 人工遮盖）")
    print(f"  OCR：{len(r.ocr.lines)} 行（平均置信度 {r.ocr.avg_confidence:.2f}，档位值）")
    for w in r.warnings:
        print(f"  ⚠️ {w}")
    if r.needs_review:
        print("  ⚠️ 待人工确认：OCR 结果置信度不足——请核对后入库，不要直接采信")
    return 0


def cmd_ingest_batch(args: argparse.Namespace) -> int:
    """批量录入工作台（ING-08）。退出码：0 完成 · 1 无可处理文件。"""
    from fdl_core.ingest.batch import ingest_directory, undo_batch

    if args.undo:
        log = args.log or Path(args.subject_dir) / "ingest-batch-log.jsonl"
        removed = undo_batch(args.undo, log)
        print(f"[fdl ingest-batch] 批次 {args.undo} 撤销：删除 {removed} 个归档文件")
        return 0

    d = Path(args.photos_dir)
    if not d.exists():
        print(f"[fdl ingest-batch] 目录不存在：{d}", file=sys.stderr)
        return 2
    report = ingest_directory(d, args.subject_dir, log_path=args.log)
    summary = (
        f"[fdl ingest-batch] 批次 {report.batch_id}：共 {report.total} 张，"
        f"成功 {len(report.ok)}，待人工确认 {len(report.needs_review)}，"
        f"失败 {len(report.failed)}，耗时 {report.elapsed_sec:.1f}s"
        f"（单张 {report.per_item_sec:.1f}s）"
    )
    print(summary)
    for item in report.needs_review:
        name = Path(item["src"]).name
        print(f"  ⚠️ 待人工确认：{name}（engine={item['engine']}，{item['lines']} 行）")
    for item in report.failed:
        print(f"  ✗ 失败：{Path(item['src']).name} — {item['error']}", file=sys.stderr)
    if report.total == 0:
        return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """每日复习（阶段二 SRS）。"""
    print("[fdl review] 骨架就绪，复习逻辑由阶段二填充")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """指标查询（阶段四 MT）。"""
    print("[fdl metrics] 骨架就绪，指标聚合由阶段四填充")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """系统诊断（阶段一基础版）。"""
    print("[fdl doctor] 骨架就绪，健康检查由阶段二填充")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """备份（阶段四 OPS）。"""
    print("[fdl backup] 骨架就绪，备份逻辑由阶段四填充")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """恢复（阶段四 OPS）。"""
    print("[fdl restore] 骨架就绪，恢复逻辑由阶段四填充")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fdl",
        description="FDL（Frank Deep Learning）命令行工具",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser(
        "ingest",
        help="录入真实学习物（拍照照 → 矫正/分离/OCR/擦除 → 归档）",
        description=(
            "录入一张真实学习物照片：方向/透视矫正 → 红黑笔分离 → "
            "OCR 双引擎（Apple Vision 主，RapidOCR 兜底）→ 手写擦除 → 归档。\n"
            "退出码：0 成功 · 1 管线执行异常 · 2 文件/参数错误"
        ),
    )
    p_ingest.add_argument("image", help="学习物照片路径（jpg/jpeg/png/heic）")
    p_ingest.add_argument(
        "--subject-dir",
        default="1-Math",
        help="学科目录（归档到其 04-真实学习物/ 下；默认 1-Math）",
    )
    p_ingest.add_argument("--title", default=None, help="归档文件名（默认沿用源文件名）")
    p_ingest.add_argument(
        "--clean-out",
        default=None,
        help="空白重做卷输出路径（默认 <subject-dir>/04-真实学习物/clean/<名>-clean.jpg）",
    )
    p_ingest.add_argument(
        "--engine",
        choices=("auto", "apple_vision", "rapidocr"),
        default="auto",
        help="OCR 引擎（默认 auto：Vision 主，异常/空结果降级 RapidOCR）",
    )

    p_batch = sub.add_parser(
        "ingest-batch",
        help="批量录入目录内全部照片（ING-08 King 工作台，支持撤销）",
        description=(
            "批量录入：目录内所有 jpg/png/heic 逐张走 ingest 管线，"
            "输出汇总（成功/失败/待人工确认）并写入 ingest-batch-log.jsonl（可撤销）。"
            "低置信结果一律标记待人工确认，不自动采信。"
        ),
    )
    p_batch.add_argument("photos_dir", help="照片目录")
    p_batch.add_argument("--subject-dir", default="1-Math", help="学科目录（默认 1-Math）")
    p_batch.add_argument(
        "--log", default=None, help="批次日志路径（默认 <subject-dir>/ingest-batch-log.jsonl）"
    )
    p_batch.add_argument(
        "--undo",
        metavar="BATCH_ID",
        default=None,
        help="撤销指定批次（删除其归档文件）",
    )
    sub.add_parser("review", help="每日复习")
    sub.add_parser("metrics", help="指标查询")
    sub.add_parser("doctor", help="系统诊断")
    sub.add_parser("backup", help="备份")
    sub.add_parser("restore", help="恢复")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "ingest": cmd_ingest,
        "ingest-batch": cmd_ingest_batch,
        "review": cmd_review,
        "metrics": cmd_metrics,
        "doctor": cmd_doctor,
        "backup": cmd_backup,
        "restore": cmd_restore,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

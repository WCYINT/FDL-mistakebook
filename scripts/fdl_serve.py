"""FDL 本地分析服务（阶段 1.6 · King 需求：录入错题地址 → 自动分析 → 进度条）。

用法：
    python scripts/fdl_serve.py [--port 8765]

端点：
    GET  /                  → 服务状态页（JSON）
    POST /api/enqueue       → body {"path": "<本地照片/目录路径>"} → 返回 {"job_id"}
    GET  /api/progress?id=  → {"percent", "stage", "message", "done", "error", "result"}

说明：
- 纯 stdlib（http.server + threading），零新依赖；
- 分析管线复用 fdl_core.ingest：逐张 ingest_photo（预处理→红黑分离→OCR→擦除→归档）；
- 进度按"已处理张数 / 总张数"实时更新（内存 job 表）；
- 仅监听 127.0.0.1（单机家庭场景，不暴露外网）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 项目根加入 sys.path（保证 python scripts/fdl_serve.py 直跑）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_ROOT))

from fdl_core.ingest.archive import ingest_photo  # noqa: E402
from fdl_core.ingest.batch import SUPPORTED_EXT  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402

# === 内存 job 表（单机够用；重启即清空，符合"临时分析任务"语义）===
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

SUPPORTED_EXT = (
    SUPPORTED_EXT
    if isinstance(SUPPORTED_EXT, (list, tuple, set))
    else {".jpg", ".jpeg", ".png", ".heic"}
)


def _subject_dir(target: Path) -> Path:
    """按输入路径智能归档：语文→2-语文、英语→3-英语、科学→4-科学，默认数学。

    2026-09-09 修复：此前硬编码 1-Math，导致录入 2-语文/H-黄冈作业 时
    语文听写照片被错误归档到数学目录。
    """
    root = get_paths().root
    s = str(target)
    if "语文" in s:
        sd = root / "2-语文" / "03-错题快照"
    elif "英语" in s:
        sd = root / "3-英语" / "03-错题快照"
    elif "科学" in s:
        sd = root / "4-科学" / "03-错题快照"
    else:
        sd = root / "1-Math" / "03-错题快照"
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def _diagnose_needs_review(r) -> tuple[str, str]:
    """根据 IngestResult 推断待复核的【具体问题】与【最可能原因】。"""
    if not r.ocr.lines:
        return (
            "OCR 未识别到任何文字行（0 行）",
            "最可能：封面/目录/空白页等非题目页；或照片过曝、失焦、拍摄距离过远",
        )
    conf = r.ocr.avg_confidence
    if conf < 0.60:
        detail = f"OCR 平均置信度 {conf:.0%}，低于 60% 阈值"
        if r.ocr.lines and len(r.ocr.lines) <= 3:
            return (
                detail + f"，仅识别出 {len(r.ocr.lines)} 行文字",
                "最可能：页面文字稀少（统计图/特殊版式），或拍摄不完整只拍到部分页",
            )
        return (detail, "最可能：手写作答过淡/被红笔批改干扰，或拍摄倾斜、模糊、页面褶皱")
    return ("未知原因（needs_review 标记但指标正常）", "建议人工查看原图判断")


def _append_review_queue(entries: list[dict]) -> None:
    """把待复核条目持久化到 data/review_queue.json（报告读取展示）。保留最近 50 条。"""
    qp = _ROOT / "data" / "review_queue.json"
    try:
        queue = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else []
    except Exception:
        queue = []
    queue.extend(entries)
    queue = queue[-50:]
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_analysis(job_id: str, target: Path) -> None:
    """后台线程：逐张 ingest_photo，实时更新 _JOBS[job_id] 进度。"""
    job = _JOBS[job_id]
    try:
        # 收集待分析图片（目录递归 / 单文件）
        if target.is_dir():
            photos = sorted(p for p in target.rglob("*") if p.suffix.lower() in SUPPORTED_EXT)
        else:
            photos = [target]
        # 过滤不存在的
        photos = [p for p in photos if p.exists()]
        if not photos:
            job.update(
                done=True,
                percent=100,
                stage="done",
                message="未找到可分析的照片文件",
                error=None,
                result={"total": 0},
            )
            return

        # ── 出处页识别（2026-09-09 King：封面/目录不是错题，仅作出处标注）──
        SOURCE_PAGE_PAT = ("封面", "目录")
        source_label = target.name if target.is_dir() else target.parent.name
        source_pages = [p.name for p in photos if any(k in p.stem for k in SOURCE_PAGE_PAT)]
        error_photos = [p for p in photos if not any(k in p.stem for k in SOURCE_PAGE_PAT)]

        job["total"] = len(photos)
        sd = _subject_dir(target)
        ok, failed, needs_review = [], [], []
        queue_entries = []
        for i, p in enumerate(error_photos, 1):
            job.update(
                percent=int((i - 1) / max(1, len(error_photos)) * 100),
                stage="analyzing",
                message=f"正在分析第 {i}/{len(error_photos)} 张：{p.name}"
                + (
                    f"（出处页 {len(source_pages)} 张已单独记录，不作错题）" if source_pages else ""
                ),
            )
            try:
                # 归档文件名带出处前缀（King：把出处记录到对应错题图片的名称中）
                title = (
                    f"{source_label}_{p.stem}" if source_label else p.stem
                )  # stem：无后缀（archive 侧统一加 .png）
                r = ingest_photo(p, sd, title=title)
                item = {"src": p.name, "engine": r.ocr.engine, "lines": len(r.ocr.lines)}

                # ── 拆题识别（2026-09-09 King：P2 策略升级）──
                # 整页置信低（needs_review）→ 红笔批注定位 + 分题裁剪：
                # 每个被打叉/圈出的区域裁为独立子图（无损 PNG），逐段 OCR，
                # "有红批注 或 置信<60%" 的段进入待人工确认队列（截图给人工）。
                page_segments = []
                if r.needs_review:
                    try:
                        from fdl_core.ingest import vlm
                        from fdl_core.ingest.preprocess import preprocess
                        from fdl_core.ingest.segment import save_segments, segment_page

                        # 2026-09-10 King："题目是横着的，截图要横着"——
                        # preprocess（detect_rotation_v2 已消 180° 歧义）输出的方向
                        # 即文字横排的正确方向；子图直接从该图裁剪（横向）。
                        # 归档整页同样为方向矫正版（archive.py，PNG 无损）。
                        bgr = preprocess(str(p))

                        # VLM 整页直读优先（配置驱动：未启用/失败 → 回退本地 CV 切分）
                        vlm_questions = vlm.read_page(bgr)
                        if vlm_questions:
                            page_segments = [
                                {
                                    "index": i + 1,
                                    "bbox": None,
                                    "red_marks": 0 if not q.get("is_wrong") else 1,
                                    "red_kind": "vlm",
                                    "confidence": None,
                                    "lines": None,
                                    "text_head": (q.get("stem") or "")[:120],
                                    "needs_review": bool(q.get("is_wrong")),
                                    "reason": f"第「{q.get('no', '?')}」题：VLM 判定为错题（{q.get('reason') or '—'}）",
                                    "likely_cause": f"作答：{q.get('answer') or '—'}｜批改：{q.get('marked') or '—'}",
                                    "crop_path": None,
                                    "vlm": True,
                                    "y_range": q.get("y_range"),
                                }
                                for i, q in enumerate(vlm_questions)
                            ]
                            job.update(
                                message=f"正在用多模态模型直读第 {i + 1}/{len(error_photos)} 页：{p.name}"
                            )
                        else:
                            segs = save_segments(
                                bgr,
                                segment_page(bgr),
                                sd / "05-拆题段",
                                f"{source_label}_{p.stem}" if source_label else p.stem,
                            )
                            page_segments = [
                                {
                                    "index": sg.index,
                                    "bbox": list(sg.bbox),
                                    "red_marks": sg.red_marks,
                                    "red_kind": sg.red_kind,
                                    "confidence": round(sg.confidence, 2),
                                    "lines": sg.ocr_lines,
                                    "text_head": sg.text_head,
                                    "needs_review": sg.needs_review,
                                    "reason": sg.reason,
                                    "likely_cause": sg.likely_cause,
                                    "crop_path": sg.crop_path,
                                }
                                for sg in segs
                            ]
                        item["segments"] = len(page_segments)
                    except Exception as seg_err:
                        item["segment_error"] = f"{type(seg_err).__name__}: {seg_err}"

                if r.needs_review:
                    reason, likely = _diagnose_needs_review(r)
                    item.update(
                        {
                            "reason": reason,
                            "likely_cause": likely,
                            "confidence": round(r.ocr.avg_confidence, 2),
                            "original": str(r.original_path),
                            "archived_to": str(sd),
                        }
                    )
                    needs_review.append(item)
                    # 页级条目（整页原图，供上下文）
                    queue_entries.append(
                        {
                            "ts": time.time(),
                            "src": p.name,
                            "original": str(r.original_path),
                            "engine": r.ocr.engine,
                            "lines": len(r.ocr.lines),
                            "confidence": round(r.ocr.avg_confidence, 2),
                            "reason": reason,
                            "likely_cause": likely,
                            "source_label": source_label,
                            "status": "pending",
                            "page_level": True,
                            "segments_found": len(page_segments),
                        }
                    )
                    # 段级条目（每个红批注/低置信子图一条 → 人工逐题确认）
                    for sg in page_segments:
                        if not sg["needs_review"]:
                            continue
                        queue_entries.append(
                            {
                                "ts": time.time(),
                                "src": f"{p.name} · 第 {sg['index']} 题",
                                # 🔴 2026-09-10 King："截图只有部分，如果无法精确截取一题
                                # 的图片，你还是显示整页吧"——主图一律用**整页原图**
                                # （方向矫正版 PNG 无损，包含全部上下文），段级 segment_crop
                                # 仅作可选附加。
                                "original": str(r.original_path),
                                "segment_crop": sg.get("crop_path"),
                                "engine": r.ocr.engine,
                                "lines": sg["lines"],
                                "confidence": sg["confidence"],
                                "reason": sg["reason"],
                                "likely_cause": sg["likely_cause"],
                                "red_marks": sg["red_marks"],
                                "red_kind": sg["red_kind"],
                                "text_head": sg["text_head"],
                                "source_label": source_label,
                                "status": "pending",
                                "page_level": False,
                            }
                        )
                else:
                    ok.append({**item, "source_label": source_label})
            except Exception as e:
                failed.append({"src": p.name, "error": f"{type(e).__name__}: {e}"})
            job["percent"] = int(i / max(1, len(error_photos)) * 100)

        if queue_entries:
            _append_review_queue(queue_entries)

        src_note = (
            f"；出处页 {len(source_pages)} 张（{('、'.join(source_pages))}）已记录出处「{source_label}」，不作错题"
            if source_pages
            else ""
        )

        # 🔴 2026-09-13（King：错题图鉴应由录入按钮触发后自动更新）：
        #   原实现只处理图片不刷新报告 → 错题图鉴/驾驶舱数字要等下次手动生成才更新。
        #   与"上传复习资料"管线对齐：有新入库或待人工项时重新生成报告。
        refresh_note = ""
        if ok or needs_review:
            job.update(
                percent=96, stage="refresh", message="正在刷新报告（错题图鉴 / 驾驶舱数字卡）…"
            )
            try:
                import sys as _sys

                _scripts_dir = str(Path(__file__).resolve().parent)
                if _scripts_dir not in _sys.path:
                    _sys.path.insert(0, _scripts_dir)
                from generate_report import main as _gen

                from fdl_core.paths import get_paths as _get_paths

                _gen(_get_paths().primary_db_path)
                refresh_note = " · 报告已刷新（错题图鉴已更新）"
            except Exception as exc:  # noqa: BLE001 — 刷新失败不推翻已完成的录入
                refresh_note = f"（报告刷新失败：{type(exc).__name__}）"

        job.update(
            done=True,
            percent=100,
            stage="done",
            message=f"分析完成：{len(ok)} 张入库、{len(needs_review)} 张待人工确认、{len(failed)} 张失败"
            + src_note
            + refresh_note,
            error=None,
            result={
                "total": len(photos),
                "ok": len(ok),
                "needs_review": len(needs_review),
                "failed": len(failed),
                "source_pages": source_pages,
                "source_label": source_label,
            },
            needs_review_detail=needs_review,
            failed_detail=failed,
        )
    except Exception as e:
        job.update(
            done=True,
            percent=100,
            stage="error",
            message=f"分析失败：{e}",
            error=f"{type(e).__name__}: {e}",
        )


def _run_resource_analysis(job_id: str, target: Path) -> None:
    """后台线程：多类型复习资料管线（预处理→识别→分析→参数更新）。

    2026-09-12 King 需求「上传复习资料」：与 `_run_analysis`（仅图片错题）
    互补——本管线支持 音频/图片/视频/文档 四类，收尾自动重生成报告
    （驾驶舱数字卡/近 14 天趋势等参数随库刷新）。
    """
    job = _JOBS[job_id]

    def _cb(stage: str, percent: int, message: str) -> None:
        job.update(stage=stage, percent=percent, message=message)

    try:
        from fdl_core.ingest.resource_pipeline import process_resources

        client = None
        try:
            from fdl_core.mistakes.attribution_engine import get_default_client

            client = get_default_client()
        except Exception:  # noqa: BLE001 — LLM 不可用则只做本地转写/录入
            client = None

        r = process_resources(
            target,
            progress_cb=_cb,
            client=client,
            image_subject_dir=_subject_dir(target),
        )

        # ④ 参数更新：重生成报告（数字卡/趋势/星图）
        _cb("refresh", 94, "正在刷新报告参数（驾驶舱数字卡/近 14 天趋势）…")
        report_path = ""
        try:
            from fdl_core.ingest.resource_pipeline import refresh_report

            report_path = refresh_report()
        except Exception as exc:  # noqa: BLE001 — 刷新失败不推翻已完成的摄入
            job.update(message=f"资料处理完成，但报告刷新失败：{exc}")

        ok_n = sum(1 for x in r["results"] if x["ok"] and not x["skipped"])
        skip_n = sum(1 for x in r["results"] if x["skipped"])
        job.update(
            done=True,
            percent=100,
            stage="done",
            message=(
                f"资料处理完成：{r['total']} 个（成功 {ok_n} · 跳过 {skip_n}"
                f" · 待人工 {len(r['needs_review'])}）"
                + (f" · 复习记录更新 {len(r['applied'])} 条" if r["applied"] else "")
                + " · 报告已刷新"
            ),
            error=None,
            result={
                **{k: r[k] for k in ("total", "by_kind", "applied")},
                "ok": ok_n,
                "review_needed": len(r["needs_review"]),
                "report_path": report_path,
            },
            needs_review_detail=r["needs_review"],
            failed_detail=r["errors"],
        )
    except Exception as e:
        job.update(
            done=True,
            percent=100,
            stage="error",
            message=f"资料处理失败：{e}",
            error=f"{type(e).__name__}: {e}",
        )


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 本地报告页可能从 file:// 打开 → 允许跨域（仅本机服务，风险可控）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/image"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._handle_serve_image(qs)
            return
        if self.path.startswith("/api/report/section/review_today"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._handle_report_review_today(qs)
            return
        if self.path.startswith("/api/report/section/completion_rate"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._handle_report_completion_rate(qs)
            return
        # === KP 挂载体系（2026-09-12 Phase 1）===
        if self.path.startswith("/api/kp/proposals"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._handle_kp_proposals_list(qs)
            return
        if self.path.startswith("/api/kp/candidates"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._handle_kp_candidates_list(qs)
            return
        if self.path.startswith("/api/kp/catalog"):
            self._handle_kp_catalog()
            return
        if self.path.startswith("/api/progress"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            jid = params.get("id", "")
            job = _JOBS.get(jid)
            if job is None:
                self._json(404, {"error": "job not found"})
                return
            self._json(
                200,
                {
                    k: job.get(k)
                    for k in (
                        "percent",
                        "stage",
                        "message",
                        "done",
                        "error",
                        "result",
                        "total",
                        "needs_review_detail",
                        "failed_detail",
                    )
                },
            )
            return
        # 服务状态页
        self._json(
            200,
            {
                "service": "FDL 本地分析服务",
                "status": "running",
                "jobs": len(_JOBS),
                "usage": {
                    "enqueue": 'POST /api/enqueue {"path": "<照片/目录路径>"}',
                    "enqueue_resource": 'POST /api/enqueue_resource {"path": "<教材/录音/视频路径>"}',
                    "progress": "GET /api/progress?id=<job_id>",
                },
            },
        )

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/match"):
            # 语音→错题人工指定映射（持久化到 data/asr_drafts/match_map.json）
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            audio = str(body.get("audio") or "").strip()
            mid = body.get("mistake_id")
            if not audio or not mid:
                self._json(400, {"error": "audio 与 mistake_id 必填"})
                return
            map_path = _ROOT / "data" / "asr_drafts" / "match_map.json"
            map_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                m = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {}
            except Exception:
                m = {}
            m[audio] = int(mid)
            map_path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
            self._json(
                200, {"ok": True, "audio": audio, "mistake_id": int(mid), "saved_to": str(map_path)}
            )
            return
        if self.path.startswith("/api/mistakes/confirm"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                _body = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            self._handle_confirm_mistake(_body)
            return
        if self.path.startswith("/api/review/feedback"):
            # 自读 body：body 变量只在 /api/match 分支内定义，此处不能复用
            try:
                length = int(self.headers.get("Content-Length") or 0)
                _body = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            self._handle_review_feedback(_body)
            return
        # === KP 挂载体系（2026-09-12 Phase 1）===
        # 路由形态：/api/kp/match/<id> · /api/kp/proposals/<id>/accept|reject
        #          /api/kp/candidates/<id>/promote
        if self.path.startswith("/api/kp/"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                _body = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            self._handle_kp_action(self.path.split("?", 1)[0], _body)
            return
        # === 反馈层闭环方案 B（2026-09-13）：干预动作回标 DONE ===
        if self.path.startswith("/api/intervention/done"):
            # 自读 body：body 变量只在 /api/match 分支内定义，此处不能复用
            try:
                length = int(self.headers.get("Content-Length") or 0)
                _body = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            self._handle_intervention_done(_body)
            return
        # 注意：/api/image 是 GET（见 do_GET），不要在这里再处理
        if not self.path.startswith("/api/enqueue"):
            self._json(404, {"error": "not found"})
            return
        # 2026-09-12 King 需求：多类型复习资料（音频/图片/视频/文档）
        # /api/enqueue_resource 走 resource_pipeline；/api/enqueue 保持原图片语义。
        is_resource = self.path.startswith("/api/enqueue_resource")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            self._json(400, {"error": f"bad json: {e}"})
            return
        raw = str(body.get("path") or "").strip()
        if not raw:
            self._json(400, {"error": "path 不能为空"})
            return
        target = Path(raw).expanduser()
        # 允许相对路径（相对于 FDL 根）
        if not target.is_absolute():
            target = get_paths().root / target
        if not target.exists():
            self._json(400, {"error": f"路径不存在: {target}"})
            return

        jid = uuid.uuid4().hex[:12]
        _JOBS[jid] = {
            "job_id": jid,
            "percent": 0,
            "stage": "queued",
            "message": "已加入队列，等待分析",
            "done": False,
            "error": None,
            "result": None,
            "total": None,
            "path": str(target),
            "created_at": time.time(),
        }
        runner = _run_resource_analysis if is_resource else _run_analysis
        t = threading.Thread(target=runner, args=(jid, target), daemon=True)
        t.start()
        self._json(
            200,
            {
                "job_id": jid,
                "accepted": True,
                "path": str(target),
                "mode": "resource" if is_resource else "photo",
            },
        )

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _handle_serve_image(self, qs: str) -> None:
        """GET /api/image?path=<abs> → 本地图片字节流（给 lightbox 读 file:// 受限场景）。（给 lightbox 读 file:// 受限场景）。"""
        from urllib.parse import unquote

        # qs 是 raw query 字符串（含 path=/...），parse_qs 会把整个串当 value
        # 这里改手动解析：取 'path=' 后的部分再 URL 解码
        kv = (qs or "").split("=", 1)
        p = unquote(kv[1]) if len(kv) == 2 else ""
        if not p:
            self._json(400, {"error": "path 必填"})
            return
        path = Path(p)
        if not path.exists() or not path.is_file():
            self._json(404, {"error": f"文件不存在: {path}"})
            return
        # 限缩媒体访问路径（防目录穿越）：默认取项目根目录的上一级，
        # 可用环境变量 FDL_ALLOWED_MEDIA_ROOT 覆盖（避开 daemon 模式下 get_paths() 的非预期行为）
        root = os.environ.get("FDL_ALLOWED_MEDIA_ROOT") or str(Path(__file__).resolve().parents[2])
        try:
            real = str(path.resolve())
        except Exception:
            self._json(400, {"error": "路径解析失败"})
            return
        # 🔴 2026-09-12 Bug 修复（"今日复习页面查看原题"排查中发现）：
        # 外置卷的挂载名大小写不固定（同一磁盘可能挂成大写或小写），而允许根目录常量
        # 只写了一种大小写；原实现用 os.path.normcase 做"大小写无关"比较，但
        # **normcase 在 POSIX 上是空操作**（只在 Windows 上 lowercase）→ 实际是大小写
        # 敏感比较 → 大小写不匹配时"查看原图/查看原题"功能全线 403 失效。
        # 改为显式 lower 比较（macOS 默认大小写不敏感文件系统，语义正确）。
        _real_c = real.lower()
        _root_c = root.lower()
        if not (_real_c == _root_c or _real_c.startswith(_root_c + "/")):
            self._json(403, {"error": "路径超出允许范围"})
            return
        suffix = path.suffix.lower()
        mime = {
            "png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix.lstrip("."), "application/octet-stream")
        try:
            data = path.read_bytes()
        except Exception as e:
            self._json(500, {"error": f"读取失败: {e}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # 短缓存：避免 lightbox 反复读；落盘图片本身不变
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def _handle_report_review_today(self, qs: str) -> None:
        """GET /api/report/section/review_today?cap=15

        实时"今日复习"列表（替代嵌入 D.review_today），让报告页"复习完立刻看到队列重排"。

        复用 fdl_core.srs.queue_scheduler.compute_states：它是间隔驱动排序的单一可信源。
        截断到 cap 条（默认 15，对应 DAILY_REVIEW_CAP），按 priority_score 降序。

        响应 schema 与前端 __fdRealtime 契约一致（缺字段降级 → ok=false）。
        """
        from fdl_core.db.schema import get_connection
        from fdl_core.paths import get_paths
        from fdl_core.srs.queue_scheduler import compute_states
        from fdl_core.srs.time_layer import fmt_ts
        from fdl_core.srs.time_layer import now_utc as now_local

        try:
            # 解析 cap（默认 15，上限 200 防止误用）
            cap = 15
            for kv in (qs or "").split("&"):
                if kv.startswith("cap="):
                    try:
                        v = int(kv.split("=", 1)[1])
                        if 0 < v <= 200:
                            cap = v
                    except Exception:
                        pass
            conn = get_connection(get_paths().primary_db_path)
            items_state = compute_states(conn)
            # 已有排序：priority_score DESC, overdue_days DESC, schedule_id ASC
            # build_daily_queue 内部就有 cap 截断，复用以保证与"今日复习"语义一致
            from fdl_core.srs.queue_scheduler import build_daily_queue

            top = build_daily_queue(conn, cap=cap)
            now_str = fmt_ts(now_local())
            items_out = []
            for it in top:
                # 找对应错题的 img_original（Plan B 不强依赖，缺则 null）
                try:
                    row = conn.execute(
                        "SELECT m.img_original, m.source_ref, m.diagnosis_type,"
                        " (SELECT s.code FROM subject s WHERE s.id = m.subject_id LIMIT 1) AS subj_code"
                        " FROM mistake_record m WHERE m.id = ?",
                        (it.mistake_id,),
                    ).fetchone()
                except Exception:
                    row = None
                img_path = row[0] if row else None
                source_ref = row[1] if row else ""
                diag = row[2] if row else (it.diag_type or "")
                subj_code = row[3] if (row and len(row) > 3) else None
                # status 文案（与 D.review_today 嵌入版保持字段名一致）
                if it.is_startup and it.overdue_days > 0:
                    status = "待复习"  # 从未复习但已逾期 → 今日必做
                elif it.overdue_days > 0:
                    status = "今日已复习"  # 已有过复习但需滚动
                else:
                    status = "待复习"
                items_out.append(
                    {
                        "id": it.mistake_id,
                        "schedule_id": it.schedule_id,
                        "subject": subj_code or "MATH",
                        "error_type": diag or "OTHER",
                        "date": now_str[:10],
                        "status": status,
                        "img_path": img_path,
                        "source_ref": (source_ref or "")[:120],
                        "interval_label": it.interval_label,
                        "priority_score": it.priority_score,
                        "is_startup": it.is_startup,
                        "overdue_days": it.overdue_days,
                    }
                )
            conn.close()
            self._json(
                200,
                {
                    "ok": True,
                    "items": items_out,
                    "generated_at": now_str,
                    "cap": cap,
                    "total_pool": len(items_state),
                },
            )
        except Exception as e:
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_report_completion_rate(self, qs: str) -> None:
        """GET /api/report/section/completion_rate?days=7

        复习"过程"指标：window_days 内应复习的卡数 vs 实际反馈次数 + 每天的反馈分布。

        语义：
          expected = DAILY_REVIEW_CAP × window_days  （应复习总量）
          done     = review_feedback 落在该闭开 UTC 区间内的 COUNT(*)
          rate     = done / expected (0..1，超 1 不截断)
          last7_checkins = 每天的反馈数 list，长度 = min(window_days, 实际有数据天数)
        """
        from datetime import datetime, time, timedelta

        from fdl_core.db.schema import get_connection
        from fdl_core.mistakes.review import DAILY_REVIEW_CAP
        from fdl_core.paths import get_paths
        from fdl_core.srs.time_layer import LOCAL_TZ, UTC, local_date

        try:
            days = 7
            for kv in (qs or "").split("&"):
                if kv.startswith("days="):
                    try:
                        v = int(kv.split("=", 1)[1])
                        if 0 < v <= 90:
                            days = v
                    except Exception:
                        pass
            today = None  # 本地今日
            from fdl_core.srs.time_layer import local_date

            today = local_date()
            # local_date_range 仅支持单日；多日窗口自定义闭开 [start_local, end_local)
            start_local = datetime.combine(
                today - timedelta(days=days - 1), time.min, tzinfo=LOCAL_TZ
            )
            end_local = datetime.combine(today + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
            start = start_local.astimezone(UTC)
            end = end_local.astimezone(UTC)
            conn = get_connection(get_paths().primary_db_path)
            # 期望量
            expected = DAILY_REVIEW_CAP * days
            # 实际反馈总数
            row = conn.execute(
                "SELECT COUNT(*) FROM review_feedback WHERE created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()
            done = int(row[0]) if row else 0
            rate = (done / expected) if expected > 0 else 0.0
            # 每天的反馈数（按本地日期分组）
            # 用 created_at 是 UTC 字符串，需要按本地日期聚合：直接做在 Python 端
            # （数据量小，最多 expected × days 条，性能不是瓶颈）
            rows = conn.execute(
                "SELECT created_at FROM review_feedback WHERE created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchall()
            from collections import Counter
            from datetime import datetime

            from fdl_core.srs.time_layer import LOCAL_TZ, to_utc

            per_day = Counter()
            for (cat,) in rows:
                try:
                    dt = datetime.fromisoformat(cat.replace("Z", "+00:00"))
                    local_d = to_utc(dt).astimezone(LOCAL_TZ).date().isoformat()
                    per_day[local_d] += 1
                except Exception:
                    pass
            last7 = [{"date": d, "count": c} for d, c in sorted(per_day.items())]
            conn.close()
            self._json(
                200,
                {
                    "ok": True,
                    "window_days": days,
                    "expected": expected,
                    "done": done,
                    "rate": round(rate, 3),
                    "last7_checkins": last7,
                },
            )
        except Exception as e:
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    # ── KP 挂载体系 handlers（2026-09-12 Phase 1）────────────
    def _kp_conn(self):
        """统一开库（含 PRAGMA）。调用方负责 close。"""
        from fdl_core.db.schema import get_connection
        from fdl_core.paths import get_paths

        return get_connection(get_paths().primary_db_path)

    def _handle_kp_proposals_list(self, qs: str) -> None:
        """GET /api/kp/proposals?status=PROPOSED&limit=200 → 挂载提案复核队列。"""
        from fdl_core.mistakes import kp_matcher as kpm

        try:
            params = dict(p.split("=", 1) for p in (qs or "").split("&") if "=" in p)
            status = params.get("status") or None
            try:
                limit = int(params.get("limit") or 200)
            except ValueError:
                limit = 200
            conn = self._kp_conn()
            try:
                rows = kpm.list_proposals(conn, status=status, limit=limit)
            finally:
                conn.close()
            self._json(200, {"ok": True, "count": len(rows), "items": rows})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_kp_candidates_list(self, qs: str) -> None:
        """GET /api/kp/candidates?status=PENDING → 新知识点候选队列。"""
        from fdl_core.mistakes import kp_matcher as kpm

        try:
            params = dict(p.split("=", 1) for p in (qs or "").split("&") if "=" in p)
            status = params.get("status") or "PENDING"
            conn = self._kp_conn()
            try:
                rows = kpm.list_candidates(conn, status=status)
            finally:
                conn.close()
            self._json(200, {"ok": True, "count": len(rows), "items": rows})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_kp_catalog(self) -> None:
        """GET /api/kp/catalog → 当前知识点清单（27+ 行）+ 挂载率概览。"""
        from fdl_core.mistakes import kp_matcher as kpm

        try:
            conn = self._kp_conn()
            try:
                catalog = kpm.load_kp_catalog(conn)
                total = conn.execute("SELECT COUNT(*) FROM mistake_record").fetchone()[0]
                mounted = conn.execute(
                    "SELECT COUNT(*) FROM mistake_record WHERE kp_id IS NOT NULL AND kp_id != 0"
                ).fetchone()[0]
            finally:
                conn.close()
            rate = round(mounted / total, 3) if total else 0.0
            self._json(
                200,
                {
                    "ok": True,
                    "count": len(catalog),
                    "mount_rate": rate,
                    "mounted": mounted,
                    "total": total,
                    "items": catalog,
                },
            )
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_kp_action(self, path: str, body: dict) -> None:
        """POST 路由分发：match / proposals accept|reject / candidates promote|reject。

        path 形如（已去 query）：
          /api/kp/match/77023
          /api/kp/proposals/12/accept
          /api/kp/proposals/12/reject
          /api/kp/candidates/3/promote
          /api/kp/candidates/3/reject
        """
        from fdl_core.mistakes import kp_matcher as kpm

        try:
            parts = [p for p in path.split("/") if p]  # ['api','kp',...]
            # 期望 ['api','kp',<verb>...]
            if len(parts) < 3 or parts[0] != "api" or parts[1] != "kp":
                self._json(404, {"ok": False, "error": "not found"})
                return
            verb = parts[2]
            decided_by = str(body.get("decided_by") or "PARENT")

            if verb == "match" and len(parts) == 4:
                mid = int(parts[3])
                conn = self._kp_conn()
                try:
                    res = kpm.match_kp_for_mistake(conn, mid, client=kpm.get_default_client())
                finally:
                    conn.close()
                # 错误码语义统一（2026-09-12 端点验证发现）：资源不存在 → 404，
                # 与 /proposals/<id>/accept（不存在 → 400）和未知路由（404）区分开。
                # ok=False 的唯一来源是"错题不存在"（其他异常走 except → 500）。
                self._json(200 if res.get("ok") else 404, res)
                return

            if verb == "proposals" and len(parts) == 5:
                pid = int(parts[3])
                action = parts[4]
                conn = self._kp_conn()
                try:
                    if action == "accept":
                        res = kpm.accept_proposal(conn, pid, decided_by=decided_by)
                    elif action == "reject":
                        res = kpm.reject_proposal(
                            conn,
                            pid,
                            decided_by=decided_by,
                            reason=str(body.get("reason") or "") or None,
                        )
                    else:
                        self._json(404, {"ok": False, "error": f"未知动作：{action}"})
                        return
                finally:
                    conn.close()
                self._json(200 if res.get("ok") else 400, res)
                return

            if verb == "candidates" and len(parts) == 5:
                cid = int(parts[3])
                action = parts[4]
                conn = self._kp_conn()
                try:
                    if action in ("promote", "approve"):
                        # approve = promote 的语义别名（2026-09-12 复习页"同意"按钮）
                        res = kpm.promote_candidate(
                            conn,
                            cid,
                            decided_by=decided_by,
                            parent_code=str(body.get("parent_code") or "") or None,
                        )
                    elif action == "reject":
                        # 2026-09-12：驳回支持带原因 + LLM 二审（King 需求）
                        # 有 reason → 走带原因路径（落库 + 二审 + 返回结论）；
                        # 无 reason → 兼容旧行为（纯驳回，无二审）。
                        reason = str(body.get("reason") or "").strip()
                        if reason:
                            res = kpm.reject_candidate_with_reason(
                                conn,
                                cid,
                                reason=reason,
                                decided_by=decided_by,
                                client=kpm.get_default_client(),
                            )
                        else:
                            res = kpm.reject_candidate(conn, cid, decided_by=decided_by)
                    else:
                        self._json(404, {"ok": False, "error": f"未知动作：{action}"})
                        return
                finally:
                    conn.close()
                self._json(200 if res.get("ok") else 400, res)
                return

            if verb == "suggestions" and len(parts) == 4 and parts[3] == "compute":
                # 2026-09-12：触发 LLM 生成/刷新全部 PENDING 候选的系统建议
                conn = self._kp_conn()
                try:
                    res = kpm.compute_suggestions_llm(conn, client=kpm.get_default_client())
                finally:
                    conn.close()
                self._json(200 if res.get("ok") else 500, res)
                return

            self._json(404, {"ok": False, "error": f"未知 KP 路由：{path}"})
        except ValueError as e:
            self._json(400, {"ok": False, "error": f"路径参数非法：{e}"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _handle_confirm_mistake(self, body: dict) -> None:
        """POST /api/mistakes/confirm → 写入 mistake_record + 标记 review_queue。

        字段映射（前端表单 → 错题本）：
          qno          → note_id 标题前缀 + 拼入 source_ref（保留题号定位）
          stem         → note_id 主干（错题文本/题干）
          optA-D       → source_ref 末尾（"A. ...|B. ...|..."），留作题目上下文
          answer       → wrong_answer
          correct      → correct_answer
          reason       → error_type（CONCEPT/CALC/MISREAD/NORM/OTHER）
          source_label → source_ref 前缀（H-黄冈作业）
          original     → img_original（整页原图路径）
          explain      → error_subtype（解析存次级）
        """
        from fdl_core.db.schema import get_connection
        from fdl_core.paths import get_paths

        try:
            qno = str(body.get("qno") or "").strip()
            stem = str(body.get("stem") or "").strip()
            opts = [str(body.get(k) or "").strip() for k in ("optA", "optB", "optC", "optD")]
            answer = str(body.get("answer") or "").strip()
            correct = str(body.get("correct") or "").strip()
            reason = str(body.get("reason") or "OTHER").strip()
            explain = str(body.get("explain") or "").strip()
            source_label = str(body.get("source_label") or "").strip()
            original = str(body.get("original") or "").strip()
            float(body.get("ts") or 0) or 0
            # 知识点挂载（2026-09-12 Phase 1）：读前端传入的 kp_id。
            # 前端日常不传（由 LLM 匹配引擎自动提炼）；保留该入参供人工覆盖/回填脚本使用。
            # 修复历史 bug：此前 VALUES 硬编码 0，即使前端传了也会被忽略。
            try:
                kp_id_val = int(body.get("kp_id") or 0)
            except (TypeError, ValueError):
                kp_id_val = 0
            if kp_id_val < 0:
                kp_id_val = 0
            if not qno:
                self._json(400, {"error": "qno 必填"})
                return
            if not (stem or any(opts)):
                self._json(400, {"error": "stem 与选项至少填一项"})
                return
            valid_reasons = ("CONCEPT", "CALC", "MISREAD", "NORM", "OTHER")
            if reason not in valid_reasons:
                reason = "OTHER"
            from datetime import datetime

            conn = get_connection(get_paths().primary_db_path)
            # source_ref：出处 + 题号 + 题干 + 选项（紧凑可读）
            opt_part = " | ".join(
                (o and (label + " " + o) or "")
                for o, label in zip(opts, ["A.", "B.", "C.", "D."], strict=False)
            )
            source_ref = (
                (source_label + " · " if source_label else "")
                + qno
                + " · "
                + (stem or "")
                + (" | " + opt_part if opt_part else "")
            )
            note_id = (qno + " · " + (stem[:60] or "未填题干"))[:200]
            # 列数(16) = 占位符数(16)：11 个 ? + 5 个字面量
            # 末尾补 diagnosis_type 列 + 末尾 ? + 参数 reason，修复"人工确认错题
            # 只写 error_type、报告统计口径读 diagnosis_type 导致真值丢失"的断链。
            # kp_id（2026-09-12）：改用 kp_id_val（读入参），不再硬编码 0。
            cur = conn.execute(
                """INSERT INTO mistake_record
                  (user_id, kp_id, note_id, occurred_at, subject, source, source_ref,
                   error_type, error_subtype, wrong_answer, correct_answer,
                   severity, attribution_confidence, attributed_by, img_original, diagnosis_type)
                  VALUES (?, ?, ?, ?, 'CHINESE', 'MANUAL', ?, ?, ?, ?, ?, 3, 1.0, 'MANUAL', ?, ?)""",
                (
                    1,
                    kp_id_val,
                    note_id,
                    datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    source_ref,
                    reason,
                    explain,
                    answer,
                    correct,
                    original,
                    reason,
                ),
            )
            mid = cur.lastrowid
            # 闭环补全（2026-09-12）：人工确认的错题必须进入复习调度，否则录完就沉底、
            # 永不复习（create_initial_review_schedule 此前全代码库零调用方）。建计划失败
            # 不应让整个 confirm 失败——错题已入库是主成果，故 try/except 容错。
            schedule_created = 0
            schedule_error = None
            try:
                from fdl_core.mistakes.review import create_initial_review_schedule

                schedule_created = create_initial_review_schedule(conn, [mid], interval_days=1)
            except Exception as e:
                schedule_error = f"{type(e).__name__}: {e}"
                import logging

                logging.getLogger("fdl_serve").error(
                    "建首条复习计划失败 mid=%s: %s", mid, schedule_error
                )
            conn.commit()
            # === 2026-09-12 Phase 1：LLM 自动匹配知识点 ===
            # 仅当本次未显式传 kp_id 时才触发（人工已指定则尊重人工）。
            # 此处 conn 尚未关闭——schedule_async_match 只取 db_path 并起后台线程
            # （线程 sleep 后自开新连接）；数据已 commit，子线程能读到。
            # 失败不影响 confirm 结果（异步函数自身 try/except 兜底）。
            if not kp_id_val:
                try:
                    from fdl_core.mistakes.kp_matcher import schedule_async_match

                    schedule_async_match(conn, mistake_id=mid)
                except Exception:
                    import logging

                    logging.getLogger("fdl_serve").warning(
                        "知识点匹配调度失败（不影响 confirm）mid=%s",
                        mid,
                        exc_info=True,
                    )
            conn.close()
            # 标记 review_queue 对应条目为已确认（按 source_label + qno + ts 匹配，找不到时全部跳过）
            qp = _ROOT / "data" / "review_queue.json"
            queue_index = None
            if qp.exists():
                try:
                    queue = json.loads(qp.read_text(encoding="utf-8"))
                except Exception:
                    queue = []
                for i, e in enumerate(queue):
                    if e.get("status") != "pending":
                        continue
                    e_src = str(e.get("src") or "")
                    e_label = str(e.get("source_label") or "")
                    # 段级 src 形如 "P2.jpg · 第 1 题"，页级 src 形如 "P2.jpg"
                    # 匹配：出处一致 + qno 出现在 src 末尾（段级）或 src == qno（页级少见）
                    if e_label == source_label and (qno in e_src or e_src.endswith(qno)):
                        e["status"] = "confirmed"
                        e["confirmed_at"] = time.time()
                        e["confirmed_mistake_id"] = mid
                        queue_index = i
                        break
                qp.write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
            # 错题图鉴自动更新（2026-09-13 King 需求 #3 补全）：
            # 「录入错题」对话框提交是**真正写 mistake_record 的时刻**——此前只有
            # _run_analysis（照片分析完成）与 _run_resource_analysis（资料上传）会刷新，
            # 确认录入这一关键写库点反而缺失 → 图鉴要等下次手动生成才可见新题。
            # 与既有两处对齐：刷新失败不推翻已完成的录入（容错非阻断）。
            refresh_note = ""
            try:
                import sys as _sys

                _scripts_dir = str(Path(__file__).resolve().parent)
                if _scripts_dir not in _sys.path:
                    _sys.path.insert(0, _scripts_dir)
                from generate_report import main as _gen

                from fdl_core.paths import get_paths as _get_paths

                _gen(_get_paths().primary_db_path)
                refresh_note = "；报告已刷新（错题图鉴已更新）"
            except Exception as exc:  # noqa: BLE001 — 刷新失败不推翻录入
                refresh_note = f"（报告刷新失败：{type(exc).__name__}）"
            self._json(
                200,
                {
                    "ok": True,
                    "mistake_id": mid,
                    "queue_index": queue_index,
                    "schedule_created": schedule_created,
                    "message": f"错题已录入（id=#{mid}）"
                    + (
                        f"；review_queue idx={queue_index} 已标记为 confirmed"
                        if queue_index is not None
                        else "；review_queue 未匹配条目（qno 不一致时正常）"
                    )
                    + ("；已建首条复习计划" if schedule_created else "；未建复习计划")
                    + refresh_note,
                    **({"schedule_error": schedule_error} if schedule_error else {}),
                },
            )
        except Exception as e:
            self._json(500, {"error": f"写入失败: {type(e).__name__}: {e}"})

    def _handle_review_feedback(self, body: dict) -> None:
        """POST /api/review/feedback → 把报告里的复习记录真实落库。

        复用 fdl_core.mistakes.review.mark_reviewed：它会更新 reappear_count、写
        review_feedback（含 self_rating）、旧 PENDING 计划标 DONE、按 A3 阶梯写新
        PENDING 计划。self_rating 与 mark_reviewed 的 grade 语义一致：
        1=陌生 / 2=模糊 / 3=掌握 / 4=熟练。

        幂等：mark_reviewed 自带当天幂等（同一题今天重复调用返回空列表），此时返回
        updated:false 并在 message 说明"今日已记录"，不报错。
        """
        from fdl_core.db.schema import get_connection
        from fdl_core.mistakes.review import mark_reviewed
        from fdl_core.paths import get_paths

        try:
            mid = body.get("mistake_id")
            # 严格校验整数（bool 是 int 子类，但这里错题 id 不会是布尔，排除）
            if not isinstance(mid, int) or isinstance(mid, bool):
                self._json(400, {"error": "mistake_id 必填且为整数"})
                return
            grade = body.get("self_rating")
            if not isinstance(grade, int) or isinstance(grade, bool) or grade < 1 or grade > 4:
                self._json(
                    400, {"error": "self_rating 必须为 1-4 的整数（1=陌生/2=模糊/3=掌握/4=熟练）"}
                )
                return
            duration = body.get("duration_seconds")
            note = body.get("note")
            if duration is not None and (
                not isinstance(duration, int) or isinstance(duration, bool)
            ):
                self._json(400, {"error": "duration_seconds 必须为整数或 null"})
                return
            if note is not None and not isinstance(note, str):
                self._json(400, {"error": "note 必须为字符串或 null"})
                return

            conn = get_connection(get_paths().primary_db_path)
            # 先校验 mistake 存在（避免对不存在的 id 静默成功）
            row = conn.execute("SELECT id FROM mistake_record WHERE id=?", (mid,)).fetchone()
            if row is None:
                conn.close()
                self._json(400, {"error": f"mistake_id={mid} 不存在"})
                return

            # mark_reviewed 内部已 commit（更新 reappear_count / 写 review_feedback /
            # 旧 PENDING 标 DONE / 按 A3 写新 PENDING）。返回今日实际更新的 id 列表。
            # duration_seconds / note 透传落库（record_review_feedback 已支持这两列）。
            updated_ids = mark_reviewed(
                conn,
                [mid],
                grade=grade,
                duration_seconds=duration,
                note=note,
            )
            conn.commit()

            # next_due：查该 mistake 最新的 PENDING 计划 due_date（mark_reviewed 已用
            # 本地日期算好，这里只读取，禁止用 datetime.date.today() 或 s[:10] 切片）。
            due_row = conn.execute(
                "SELECT due_date FROM review_schedule "
                "WHERE mistake_id=? AND status='PENDING' ORDER BY due_date LIMIT 1",
                (mid,),
            ).fetchone()
            next_due = due_row[0] if due_row else None
            conn.close()

            if updated_ids:
                self._json(
                    200,
                    {
                        "ok": True,
                        "mistake_id": mid,
                        "updated": True,
                        "next_due": next_due,
                        "message": "复习记录已保存",
                    },
                )
            else:
                # 当天幂等命中：重复提交不报错，返回 updated:false
                self._json(
                    200,
                    {
                        "ok": True,
                        "mistake_id": mid,
                        "updated": False,
                        "next_due": next_due,
                        "message": "今日已记录，本次为重复提交",
                    },
                )
        except Exception as e:
            self._json(500, {"error": f"写入失败: {type(e).__name__}: {e}"})

    def _handle_intervention_done(self, body: dict) -> None:
        """POST /api/intervention/done → 把一条干预动作标 DONE（反馈闭环回标）。

        幂等：已 DONE 再调用返回 already:true；PENDING/SKIPPED 都允许回标（以最新意图为准）。
        """
        from fdl_core.db.schema import get_connection
        from fdl_core.mistakes.feedback_loop import mark_done
        from fdl_core.paths import get_paths

        try:
            iid = body.get("id")
            # 严格校验整数（bool 是 int 子类，排除）
            if not isinstance(iid, int) or isinstance(iid, bool):
                self._json(400, {"error": "id 必填且为整数"})
                return
            conn = get_connection(get_paths().primary_db_path)
            try:
                res = mark_done(conn, iid)
            finally:
                conn.close()
            if res.get("not_found"):
                self._json(404, {"error": f"intervention id={iid} 不存在"})
                return
            if res.get("already"):
                self._json(200, {"ok": True, "already": True, "id": iid})
                return
            self._json(200, {"ok": True, "id": iid, "message": "干预动作已标记完成"})
        except Exception as e:
            self._json(500, {"error": f"标记失败: {type(e).__name__}: {e}"})


def _daemonize() -> None:
    """Unix double-fork 精灵化：脱离父会话（shell 退出/回收不影响服务）。"""
    import sys as _sys

    if os.fork() > 0:
        _sys.exit(0)  # 父进程退出
    os.setsid()
    if os.fork() > 0:
        _sys.exit(0)  # 第一子进程退出
    # 重定向 stdio 到日志（脱离终端）
    log = open("/tmp/fdl_serve.log", "ab", buffering=0)
    err = open("/tmp/fdl_serve.err.log", "ab", buffering=0)
    os.dup2(log.fileno(), 0)
    os.dup2(log.fileno(), 1)
    os.dup2(err.fileno(), 2)


def main() -> None:
    port = 8765
    daemon = "--daemon" in __import__("sys").argv
    # 简单 --port 参数
    import sys

    argv = sys.argv
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    if daemon:
        _daemonize()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"FDL 本地分析服务已启动: http://127.0.0.1:{port}", flush=True)
    print('  POST /api/enqueue  {"path": "<照片/目录路径>"}', flush=True)
    print("  GET  /api/progress?id=<job_id>", flush=True)
    print('  POST /api/match     {"audio": "<录音名>", "mistake_id": <id>}', flush=True)
    print("  GET  /api/image?path=<abs>", flush=True)
    print("  GET  /api/report/section/review_today?cap=15", flush=True)
    print("  GET  /api/report/section/completion_rate?days=7", flush=True)
    print("Ctrl+C 停止", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()

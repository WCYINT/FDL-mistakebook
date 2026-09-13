"""错题分段识别（2026-09-09 King 需求：P2 识别策略升级）。

策略：
1. 红笔批注定位——教师打叉/圈出的部分优先视为错题（红层 HSV 掩码 →
   形态学闭运算连接笔画 → 连通域聚类 → 批注 bbox）；
2. 每个批注簇向四周扩展出"题目区域"，从原图裁出**错题子图**（无损 PNG）；
3. 每个子图单独 OCR——置信 <60% 或含红笔批注 → 进入待人工确认队列
   （子图原图直接展示给人工，不再整页一张糊图）；
4. 无红批注且高置信的段 → 正常入库段。

🔴 King 通用规则：所有裁剪/落盘子图一律无损 PNG，不做任何压缩。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from fdl_core.ingest import ocr
from fdl_core.ingest.color_split import _red_mask, split_layers

# 批注簇过滤：小于该面积（px）视为笔尖噪声
_MIN_RED_AREA = 220
# 簇合并距离（px）：同一题的叉+圈通常相距很近。
# 2026-09-09 实测 P2：90px 会导致密集批注链式合并成"整页一簇"（87 处并 1），
# 降为 40px 保留每处批改的独立定位。
_MERGE_DIST = 40
# 子图外扩边距（px）：把批注周边的题目文本包进来
_PAD = 130
# 红像素占比阈值：批注 bbox 内红像素比例（判断是"批注"还是"印刷红字"）
_MIN_DENSITY = 0.04


@dataclass
class Segment:
    """一个错题段（一道题的裁剪区域）。"""

    index: int  # 段序号（1 起）
    bbox: tuple[int, int, int, int]  # x, y, w, h（在原图坐标系）
    red_marks: int  # 该段内红笔批注连通域数
    red_kind: str  # cross（打叉）/ circle（圈画）/ mixed / marks（未分类批注）
    ocr_lines: int
    confidence: float  # 段内 OCR 平均置信度
    text_head: str  # OCR 文本前 120 字
    needs_review: bool  # 有红批注 或 置信 <0.60
    reason: str
    likely_cause: str
    crop_path: str | None = None  # 子图无损 PNG 落盘路径


def _merge_boxes(
    boxes: list[tuple[int, int, int, int]], dist: int = _MERGE_DIST
) -> list[tuple[int, int, int, int]]:
    """贪心合并相近 bbox（同一题的多个批注归为一簇）。"""
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        out = []
        while boxes:
            x, y, w, h = boxes.pop()
            i = 0
            while i < len(boxes):
                x2, y2, w2, h2 = boxes[i]
                if (
                    x - dist <= x2 + w2
                    and x2 - dist <= x + w
                    and y - dist <= y2 + h2
                    and y2 - dist <= y + h
                ):
                    nx, ny = min(x, x2), min(y, y2)
                    nw = max(x + w, x2 + w2) - nx
                    nh = max(y + h, y2 + h2) - ny
                    x, y, w, h = nx, ny, nw, nh
                    boxes.pop(i)
                    merged = True
                else:
                    i += 1
            out.append((x, y, w, h))
        boxes = out
    return boxes


def _classify_mark(red_crop: np.ndarray) -> str:
    """按红像素分布粗分批注类型：叉（笔画分散）/圈（环形）/混合。"""
    ys, xs = np.nonzero(red_crop)
    if len(xs) == 0:
        return "marks"
    w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    fill = len(xs) / (w * h)
    if fill < 0.18 and w > 20 and h > 20:
        return "cross"  # 稀疏大框 → 两笔交叉的叉
    if 0.25 <= fill <= 0.6:
        return "circle"  # 中等填充环状 → 圈画
    return "mixed"


def _split_by_marks(s: Segment, bgr: np.ndarray, raw_boxes: list, H: int, W: int) -> list[Segment]:
    """巨型段二次细分：按段内红批注的垂直间隙切为 2-3 个子段。

    用于题号分界不足导致"多题挤进一段"的情况（2026-09-10 P2 实测：
    一段 3713px 占整页，内含一/二/三 三道大题）。
    """
    y0 = s.bbox[1]
    y1 = s.bbox[1] + s.bbox[3]
    ys = sorted(y + h / 2 for (x, y, w, h) in raw_boxes if y0 <= y + h / 2 < y1)
    if len(ys) < 3:
        return [s]
    # 找最大间隙作为切点（切成两段）
    gaps = [(ys[i + 1] - ys[i], i) for i in range(len(ys) - 1)]
    gap, gi = max(gaps)
    if gap < 0.12 * (y1 - y0):  # 批注分布均匀，无明显分界 → 不切
        return [s]
    cut = int((ys[gi] + ys[gi + 1]) / 2)
    out = []
    for a, b, tag in ((y0, cut, "上"), (cut, y1, "下")):
        if b - a < 200:
            continue
        x0e, y0e, x1e, y1e = 0, max(0, a), W, min(H, b)
        hits = [(x, y, w, hh) for (x, y, w, hh) in raw_boxes if y0e <= y + hh / 2 < y1e]
        crop = bgr[y0e:y1e, x0e:x1e]
        rc = ocr.recognize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
        conf = rc.avg_confidence
        out.append(
            Segment(
                index=0,
                bbox=(x0e, y0e, x1e - x0e, y1e - y0e),
                red_marks=len(hits),
                red_kind=s.red_kind,
                ocr_lines=len(rc.lines),
                confidence=conf,
                text_head=rc.text[:120],
                needs_review=len(hits) > 0 or (rc.lines and conf < 0.60),
                reason=f"{s.reason.split('：')[0]}·{tag}段：检测到红笔批注 ×{len(hits)}"
                if hits
                else f"{s.reason.split('：')[0]}·{tag}段：OCR 置信 {conf:.0%}",
                likely_cause="批注即判错证据，按错题处理"
                if hits
                else "手写作答过淡或版面特殊，需人工复核",
            )
        )
    return out or [s]


def segment_page(bgr: np.ndarray) -> list[Segment]:
    """整页 → 错题段列表（v2 · 题号分界法，2026-09-09）。

    v1 缺陷：以"红批注簇+固定外扩"为段 → 页缘小批注（分数框/等级章）
    外扩后不含题目主体 → 第 1/2/3/5/6/7 题内容被截断；密集批注又整页并簇。

    v2（借鉴 exam-paper-reader 的 question-number anchoring 与
    "擦除手写还原试卷"类 App 的红黑分离 + 自动框选）：
    1. black 层（去红笔干扰）整页 OCR；
    2. 大题号行（一、/二、/…/1./2.）作为题目分界锚；
    3. 段 = 相邻分界之间的全宽区域（保证题目完整不截断）；
    4. 红批注簇按中心 y 归属到段；
    5. 每段裁原图（无损 PNG）+ black 裁剪 OCR 取置信；
       needs = 段内红批注>0 或 段置信 <0.60。
    分界 <2 个时退回 v1 红批注簇法（保证总有输出）。
    """
    H, W = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = _red_mask(hsv).astype(np.uint8) * 255

    # ── 红批注簇（供归属）──
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    closed = cv2.morphologyEx(red, cv2.MORPH_CLOSE, kernel, iterations=2)
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    raw_boxes = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < _MIN_RED_AREA:
            continue
        if red[y : y + h, x : x + w].mean() / 255.0 < _MIN_DENSITY:
            continue
        raw_boxes.append((x, y, w, h))

    def _kind(cy: float) -> str:
        """该 y 邻域红批注的粗类型（按与 raw_boxes 的命中）。"""
        kinds = set()
        for x, y, w, h in raw_boxes:
            if y <= cy <= y + h:
                crop = red[y : y + h, x : x + w]
                kinds.add(_classify_mark(crop))
        return "/".join(sorted(kinds)) if kinds else "marks"

    # ── black 层整页 OCR（去红笔干扰，置信更高）──
    black_layer, _ = split_layers(bgr)
    gray = cv2.cvtColor(black_layer, cv2.COLOR_BGR2GRAY)
    page_ocr = ocr.recognize(gray)

    # ── 大题号分界 ──
    cn = "一二三四五六七八"
    bounds: list[tuple[int, str]] = []
    for ln in page_ocr.lines:
        t = ln.text.strip()
        if re.match(rf"^[{cn}][、.．]", t) and len(t) <= 40:
            bounds.append((ln.bbox[1], t[:26]))
        elif re.match(r"^\d{1,2}[、.．]\s*\S", t) and len(t) <= 30:
            bounds.append((ln.bbox[1], t[:26]))
    bounds.sort()
    # 合并 y 差 < 60 的分界（同一行被切两条）
    dedup: list[tuple[int, str]] = []
    for y, t in bounds:
        # 20px（原 60px）：2026-09-10 实测 60px 会把相邻大题号行合并成一条分界，
        # 导致分界只剩 2 个、中间 3713px 变成"巨型段"（题目全挤一段）。
        if dedup and y - dedup[-1][0] < 20:
            continue
        dedup.append((y, t))
    bounds = dedup

    # 🔴 合并过近的分界（2026-09-10 King："P2 第 2 题识别不对"）
    # 实测：页边碎片区域（高 216-259px，仅含 2-3 处红批注、OCR 出"眼睛模/空里，？/
    # 2.语/1. 海上/一切。"等上下文碎片）被当成独立题目。根因是相邻大题号分界间距
    # 过小（< 页高 8%）时会产生"碎片段"。此处把过近的分界合并（保留前者），
    # 使碎片区域并入相邻的完整大题块，不再独立成题。
    min_seg_h = max(250, int(0.08 * H))
    merged_bounds: list[tuple[int, str]] = []
    for y, t in bounds:
        if merged_bounds and y - merged_bounds[-1][0] < min_seg_h:
            continue  # 与上一分界过近 → 丢弃（区域并入上一段）
        merged_bounds.append((y, t))
    bounds = merged_bounds
    # 首段/末段过短同样并入相邻段（2026-09-10：P2 末段 291px 页边碎片
    # "五、阅 发展 眼睛模/空里，：/我望着/一切。/4. 下万"被误判为独立题目）
    # 注意：只 pop 一次（while 会把有效分界全清掉 → 退回 v1 簇法，切分退化）
    if len(bounds) >= 2 and (H - bounds[-1][0]) < min_seg_h:
        bounds.pop()  # 末段过短 → 并入前一段
    if len(bounds) >= 2 and bounds[0][0] < min_seg_h:
        bounds.pop(0)  # 首段过短 → 并入后一段

    # ── 生成分段 ──
    segments: list[Segment] = []

    def _build(y0: int, y1: int, label: str) -> Segment | None:
        if y1 - y0 < 80:
            return None
        x0, y0e, x1, y1e = 0, max(0, y0 - 16), W, min(H, y1 + 16)
        hits = [(x, y, w, h) for (x, y, w, h) in raw_boxes if y0e <= y + h / 2 < y1e]
        crop = bgr[y0e:y1e, x0:x1]
        rc = ocr.recognize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
        conf = rc.avg_confidence
        kind = _kind((y0e + y1e) / 2)
        needs = len(hits) > 0 or (rc.lines and conf < 0.60)
        if hits:
            reason = f"{label}：检测到红笔批注 ×{len(hits)}（{kind}）"
            likely = "批注即判错证据，按错题处理；题目区域为完整大题块"
        elif rc.lines and conf < 0.60:
            reason = f"{label}：段内 OCR 置信 {conf:.0%} 低于 60% 阈值"
            likely = "手写作答过淡或版面特殊，需人工复核"
        else:
            reason = f"{label}：无红笔批注，OCR 置信 {conf:.0%}"
            likely = "非错题段（自动判定）"
        return Segment(
            index=len(segments) + 1,
            bbox=(x0, y0e, x1 - x0, y1e - y0e),
            red_marks=len(hits),
            red_kind=kind,
            ocr_lines=len(rc.lines),
            confidence=conf,
            text_head=rc.text[:120],
            needs_review=needs,
            reason=reason,
            likely_cause=likely,
        )

    if len(bounds) >= 2:
        # 首个分界之上的区域（若有红批注则单独成段）
        first_y = bounds[0][0]
        if first_y > 120:
            seg = _build(0, first_y, "页首区")
            if seg and (seg.red_marks > 0 or seg.confidence < 0.60):
                segments.append(seg)
        for i, (y, t) in enumerate(bounds):
            y1 = bounds[i + 1][0] if i + 1 < len(bounds) else H
            t[:14] if t else "末段"
            seg = _build(y, y1, f"第「{t[:8]}」题")
            if seg:
                segments.append(seg)
    else:
        # 退回 v1：红批注簇法（保证总有输出）
        clusters = _merge_boxes(raw_boxes)
        for idx, (x, y, w, h) in enumerate(clusters, 1):
            x0, y0 = max(0, x - _PAD), max(0, y - _PAD)
            x1, y1 = min(W, x + w + _PAD), min(H, y + h + _PAD)
            crop = bgr[y0:y1, x0:x1]
            rc = ocr.recognize(crop)
            conf = rc.avg_confidence
            needs = not rc.lines or conf < 0.60
            reason = (
                f"OCR 置信 {conf:.0%} 低于阈值（红批注簇法）" if needs else "OCR 正常（红批注簇法）"
            )
            likely = "手写过淡/红笔干扰或拍摄模糊"
            segments.append(
                Segment(
                    index=idx,
                    bbox=(x0, y0, x1 - x0, y1 - y0),
                    red_marks=1,
                    red_kind="marks",
                    ocr_lines=len(rc.lines),
                    confidence=conf,
                    text_head=rc.text[:120],
                    needs_review=needs,
                    reason=reason,
                    likely_cause=likely,
                )
            )

    # 生成后处理（2026-09-10）：
    #   a) 丢弃"内容空洞"的碎片段（无红批注 且 中文<20 且 高<300px）→ 其批注并入相邻段；
    #   b) 对高 > 页高 55% 的巨型段，按红批注的垂直间隙二次细分（避免多题挤一段）。
    def _cn_of(s: Segment) -> int:
        return len(re.findall(r"[\u4e00-\u9fff]", s.text_head))

    refined: list[Segment] = []
    for s in list(segments):
        hh = s.bbox[3]
        if s.red_marks == 0 and _cn_of(s) < 20 and hh < 300:
            continue  # 空白/碎片，丢弃（其区域由相邻段外扩覆盖）
        if hh > 0.55 * H and s.red_marks >= 4:
            refined.extend(_split_by_marks(s, bgr, raw_boxes, H, W))
        else:
            refined.append(s)
    if refined:
        segments = refined

    # 丢弃"内容空洞的碎片段"（2026-09-10 King："P2 第 2 题识别不对"）
    # 页边碎片（高 < min_seg_h 且 中文 < 20，如"五、阅 发展/状元作业/听朗读"）
    # 不是题目——此前它们也能因"含 1-3 处红批注"而独立成题，产出假题目。
    # 此处一律丢弃；这些区域的红批注由主体段的外扩覆盖统计，不丢失批改证据。
    if segments:
        kept = [
            s
            for s in segments
            if not (s.bbox[3] < min_seg_h and len(re.findall(r"[\u4e00-\u9fff]", s.text_head)) < 20)
        ]
        if kept:
            segments = kept

    segments.sort(key=lambda s: (s.bbox[1], s.bbox[0]))  # 按
    for i, s in enumerate(segments, 1):
        s.index = i
    return segments


def _bbox_to_source(
    bbox: tuple[int, int, int, int], rotation_k: int, src_h: int, src_w: int
) -> tuple[int, int, int, int]:
    """把"矫正坐标系"的 bbox 逆映射回"源图（EXIF 后、未旋转）"坐标。

    preprocess 正变换：bgr = np.rot90(orig, k=rotation_k)（rotation_k = angle//90）。
    逆映射各角度（x/y 为列/行，w/h 为宽/高；src_h×src_w 为源图高×宽）：
      k=0: 恒等
      k=1（逆时针 90°）: (x,y,w,h) → (src_w-y-h, x, h, w)
      k=2（180°）:       (x,y,w,h) → (src_w-x-w, src_h-y-h, w, h)
      k=3（逆时针 270°）: (x,y,w,h) → (y, src_h-x-w, h, w)
    """
    x, y, w, h = bbox
    if rotation_k == 0:
        return (x, y, w, h)
    if rotation_k == 1:
        return (src_w - y - h, x, h, w)
    if rotation_k == 2:
        return (src_w - x - w, src_h - y - h, w, h)
    return (y, src_h - x - w, h, w)  # k=3


def save_segments(
    bgr: np.ndarray,
    segments: list[Segment],
    out_dir: Path,
    page_stem: str,
    *,
    source_bgr: np.ndarray | None = None,
    rotation_k: int = 0,
) -> list[Segment]:
    """把每个段的裁剪子图落盘为无损 PNG（King 规则：不压缩）。返回更新后的段。

    🔴 方向一致性（2026-09-10 King：截图与正确位置差 90° 的根因修复）：
    segment/OCR 在 preprocess 旋转矫正后的坐标系上进行（文字水平，OCR 必需），
    但归档整页 = 源字节（未旋转方向）。若从旋转图直接裁剪，子图与整页差 90°。
    修复：提供 source_bgr（load_bgr 后的原始方向图）+ rotation_k 时，
    先把 bbox 逆映射回源坐标、从源图裁剪落盘 → 子图方向与源图/归档一致。
    perspective 矫正生效时（非线性变换）无法纯旋转逆映射，保持矫正方向并交由
    调用方通过 shape 比对检测（本模块不做透视逆映射）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    use_source = source_bgr is not None
    src_h, src_w = (source_bgr.shape[0], source_bgr.shape[1]) if use_source else (0, 0)
    for s in segments:
        x, y, w, h = s.bbox
        if use_source:
            sx, sy, sw, sh = _bbox_to_source(s.bbox, rotation_k, src_h, src_w)
            crop = source_bgr[sy : sy + sh, sx : sx + sw]
        else:
            crop = bgr[y : y + h, x : x + w]
        png = out_dir / f"{page_stem}_题{s.index}.png"
        cv2.imwrite(str(png), crop)  # PNG 无损
        s.crop_path = str(png)
    return segments

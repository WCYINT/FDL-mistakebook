"""FDL 录入子包（阶段三 ING，T3-01~05）。

预处理 / 红黑分离 / OCR 双引擎 / 手写擦除 / 端到端归档。
"""

from fdl_core.ingest.archive import IngestResult, ingest_photo
from fdl_core.ingest.color_split import red_coverage, split_layers
from fdl_core.ingest.errors import IngestError, OriginalProtectedError
from fdl_core.ingest.ocr import OcrLine, OcrResult, recognize
from fdl_core.ingest.preprocess import detect_rotation, load_bgr, perspective_crop, preprocess

__all__ = [
    "IngestError",
    "IngestResult",
    "OcrLine",
    "OcrResult",
    "OriginalProtectedError",
    "detect_rotation",
    "ingest_photo",
    "load_bgr",
    "perspective_crop",
    "preprocess",
    "recognize",
    "red_coverage",
    "split_layers",
]

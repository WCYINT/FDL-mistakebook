"""FDL ASR 子包（ING-03）：可替换引擎抽象 + SenseVoice 首版 + Apple 兜底。

🔴 业务层只依赖 ASREngine 抽象；严禁 PyTorch 路线。
首版定位：录入辅助（转写 → 人工确认后入库），不做作答通道。
"""

from fdl_core.asr.apple import AppleSpeechEngine
from fdl_core.asr.base import ASREngine, TranscriptionResult, resample_to_16k
from fdl_core.asr.errors import AppleSpeechUnavailable, ASRError
from fdl_core.asr.sensevoice import SenseVoiceEngine

__all__ = [
    "ASREngine",
    "ASRError",
    "AppleSpeechEngine",
    "AppleSpeechUnavailable",
    "SenseVoiceEngine",
    "TranscriptionResult",
    "resample_to_16k",
]

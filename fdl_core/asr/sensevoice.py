"""SenseVoiceEngine（ING-03 首版实现）：FunASR SenseVoice-Small int8，经 sherpa-onnx。

🔴 严禁 PyTorch 路线——走 sherpa-onnx（ONNX Runtime），模型 254MB 不构成内存压力。
模型文件（阶段一已下载）：`models/sensevoice/model.int8.onnx` + `tokens.txt`。
"""

from __future__ import annotations

from pathlib import Path

from fdl_core.asr.base import ASREngine, TranscriptionResult, load_audio_any
from fdl_core.asr.errors import ASRError

DEFAULT_MODEL = "model.int8.onnx"
DEFAULT_TOKENS = "tokens.txt"


class SenseVoiceEngine(ASREngine):
    """SenseVoice-Small int8（sherpa-onnx 离线推理）。"""

    def __init__(
        self,
        model_path: str | Path | None = None,
        tokens_path: str | Path | None = None,
    ):
        models_dir = Path(__file__).resolve().parent.parent.parent / "models" / "sensevoice"
        self.model_path = Path(model_path) if model_path else models_dir / DEFAULT_MODEL
        self.tokens_path = Path(tokens_path) if tokens_path else models_dir / DEFAULT_TOKENS
        self._recognizer = None

    @property
    def name(self) -> str:
        return "sensevoice"

    @property
    def supported_langs(self) -> list[str]:
        return ["zh", "en", "ja", "ko", "yue"]

    def _ensure_recognizer(self):
        if self._recognizer is None:
            if not self.model_path.exists():
                raise ASRError(f"SenseVoice 模型不存在：{self.model_path}")
            import sherpa_onnx

            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self.model_path),
                tokens=str(self.tokens_path),
                use_itn=True,
            )
        return self._recognizer

    def transcribe(self, audio_path: str | Path, lang: str = "zh") -> TranscriptionResult:
        if lang not in self.supported_langs:
            raise ASRError(f"不支持语言 {lang}（支持：{self.supported_langs}）")
        samples_16k, rate16 = load_audio_any(audio_path)
        recognizer = self._ensure_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(rate16, samples_16k)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        duration = len(samples_16k) / rate16 if rate16 else 0.0
        return TranscriptionResult(
            text=text, engine=self.name, lang=lang, duration_sec=round(duration, 2)
        )

"""ASREngine 抽象层（ING-03，PRD §3.2）。

🔴 业务层只依赖本抽象，换引擎不动业务代码。
🔴 路线约束：GGUF 二进制或 sherpa-onnx（ONNX Runtime），**严禁 PyTorch 路线**。
首版定位：**录入辅助**（口述 → 离线转写 → 人工确认后入库）——
不做作答通道、不参与评分与归因。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TranscriptionResult:
    """一次转写结果（人工确认前仅为草稿）。"""

    text: str
    engine: str
    lang: str
    duration_sec: float
    segments: list[dict] = field(default_factory=list)
    confirmed: bool = False  # 🔴 人工确认标记；未确认不得入库


class ASREngine(ABC):
    """ASR 引擎抽象（可替换：SenseVoice / Apple Speech / 未来引擎）。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def supported_langs(self) -> list[str]: ...

    @abstractmethod
    def transcribe(self, audio_path: str | Path, lang: str = "zh") -> TranscriptionResult:
        """离线转写音频文件（wav，建议 16kHz 单声道）。"""

    def transcribe_stream(self, mic_stream, lang: str = "zh") -> TranscriptionResult:
        """流式转写（麦克风）；首版未启用——录入辅助用文件转写即可。"""
        raise NotImplementedError(f"{self.name} 暂不支持流式转写")


def load_audio_any(path: str | Path) -> tuple[list[float], int]:
    """加载任意音频格式（wav 走 wave；m4a/mp3/opus 走 PyAV 解码）→ 16k 单声道。"""
    p = Path(path)
    if p.suffix.lower() == ".wav":
        import wave

        with wave.open(str(p), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError(f"仅支持 16bit PCM wav，实际 sampwidth={width}")
        import numpy as np

        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return resample_to_16k(data.tolist(), rate)
    # 非 wav：PyAV 解码（FFmpeg 绑定，覆盖 m4a/mp3/opus 等）
    try:
        import av
    except ImportError as e:
        raise ValueError(f"非 wav 格式需要 PyAV：pip install av（{e}）") from e
    import numpy as np

    container = av.open(str(p))
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for packet in container.demux(stream):
        for frame in packet.decode():
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("音频解码结果为空")
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    return resample_to_16k(samples.tolist(), 16000)


def resample_to_16k(samples: list[float], orig_rate: int) -> tuple[list[float], int]:
    """线性插值重采样到 16kHz（SenseVoice 期望输入；零 scipy 依赖）。"""
    target = 16000
    if orig_rate == target:
        return samples, target
    n_out = int(len(samples) * target / orig_rate)
    if n_out == 0:
        return [], target
    out: list[float] = []
    step = (len(samples) - 1) / max(n_out - 1, 1) if len(samples) > 1 else 0.0
    for i in range(n_out):
        pos = i * step
        lo = int(pos)
        hi = min(lo + 1, len(samples) - 1)
        frac = pos - lo
        out.append(samples[lo] * (1 - frac) + samples[hi] * frac)
    return out, target

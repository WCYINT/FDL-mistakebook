"""AppleSpeechEngine（ING-03 兜底）：macOS SFSpeechRecognizer（pyobjc，离线 on-device）。

依赖 `pyobjc-framework-Speech`（当前未安装）——惰性导入，未装时抛
`AppleSpeechUnavailable` 并给出安装指引；业务层捕获后可降级人工录入。
"""

from __future__ import annotations

from pathlib import Path

from fdl_core.asr.base import ASREngine, TranscriptionResult
from fdl_core.asr.errors import AppleSpeechUnavailable


class AppleSpeechEngine(ASREngine):
    """macOS 语音识别兜底（SFSpeechRecognizer，on-device 支持 zh-CN）。"""

    @property
    def name(self) -> str:
        return "apple_speech"

    @property
    def supported_langs(self) -> list[str]:
        return ["zh", "en"]

    def transcribe(self, audio_path: str | Path, lang: str = "zh") -> TranscriptionResult:
        try:
            import Speech  # noqa: F401  pyobjc-framework-Speech
            from Foundation import NSURL
        except ImportError as e:
            raise AppleSpeechUnavailable(
                "Apple Speech 兜底引擎需要 pyobjc-framework-Speech："
                "pip install pyobjc-framework-Speech"
            ) from e

        import time

        import AppKit

        url = NSURL.fileURLWithPath_(str(Path(audio_path).resolve()))
        locale = "zh-CN" if lang == "zh" else "en-US"
        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
            AppKit.NSLocale.alloc().initWithLocaleIdentifier_(locale)
        )
        if recognizer is None:
            raise AppleSpeechUnavailable(f"系统不支持 locale {locale}")
        request = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
        request.setShouldReportPartialResults_(False)
        if recognizer.supportsOnDeviceRecognition():
            request.setRequiresOnDeviceRecognition_(True)

        done: list = []
        result_holder: list = []
        error_holder: list = []

        def handler(task, _):
            if task.state == 3:  # completed
                result_holder.append(task)
                done.append(True)
            elif task.state == 2:  # failed
                error_holder.append(task.error)
                done.append(True)

        recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)
        deadline = time.time() + 120
        while not done and time.time() < deadline:
            time.sleep(0.2)
        if error_holder:
            from fdl_core.asr.errors import ASRError

            raise ASRError(f"Apple Speech 转写失败：{error_holder[0]}")

        best = result_holder[0].bestTranscription().formattedString() if result_holder else ""
        return TranscriptionResult(text=best.strip(), engine=self.name, lang=lang, duration_sec=0.0)

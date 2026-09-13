"""ASR 子包共享错误。"""


class ASRError(RuntimeError):
    """ASR 引擎错误（模型缺失/格式不支持/推理失败）。"""


class AppleSpeechUnavailable(ImportError):
    """Apple Speech 兜底引擎依赖未安装（pyobjc-framework-Speech）。"""

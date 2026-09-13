"""版本号唯一源解析（发布版）。

设计：**pyproject.toml 的 ``[project] version`` 是唯一权威源**，本模块只做派生，
不硬编码任何版本字面量——从而消除「pyproject 与 __version__ 各写一份、彼此漂移」
的隐患（历史上 pyproject=0.1.0 与仓库发布标签 v1.2.1 不一致）。

派生顺序：
1. **同仓库的 ``pyproject.toml`` 存在** → 直接读它。这是源码树/开发检出场景，
   保证 ``__version__`` 始终等于仓库当前声明的版本，不受环境中其它同名安装干扰。
2. 反之（已作为依赖安装进 site-packages，pyproject 不在旁边）→ 取打包元数据。
3. 两者都不可得 → 明确返回 ``0.0.0+unknown``（不伪装成某个真实版本）。
"""

from __future__ import annotations

from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def resolve_version() -> str:
    """返回当前版本号；权威源为 pyproject.toml。"""
    if _PYPROJECT.exists():
        try:
            import tomllib

            data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
            return str(data["project"]["version"])
        except Exception:
            pass

    try:
        from importlib.metadata import version as _metadata_version

        return _metadata_version("fdl")
    except Exception:
        return "0.0.0+unknown"


__version__ = resolve_version()

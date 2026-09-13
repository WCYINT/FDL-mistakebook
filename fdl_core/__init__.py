"""FDL 业务内核（L0 核心层，100% 离线）。

L0 核心层在无网络、无外接电源下 100% 可用（NFR-1）。

版本号不再硬编码：权威源为 ``pyproject.toml``，在此派生（见 ``fdl_core._version``）。
"""

from fdl_core._version import __version__

__all__ = ["__version__"]

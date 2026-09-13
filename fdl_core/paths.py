"""FDL 路径解析与配置消费层（P2-02 / OPS-02）。

职责：
- 读取 `config/fdl_paths.yaml`，消费 `layout_version` 开关（v1 平级 / v2 嵌套）
- 解析 FDL 主目录、SWE 目录、四学科目录、各子目录
- 🔴 零硬编码绝对路径（NFR-6，CI 扫描挂载点绝对路径字符串拦截）

根目录定位（零硬编码）：
1. 从本模块文件位置向上搜索 `.git` 目录 → 仓库根目录
2. 回退：按 config `anchor_file` 相对路径向上搜索匹配目录
3. 最终回退：按代码位置推断（`fdl_core` 的祖父目录）
"""

from __future__ import annotations

from pathlib import Path

import yaml

# 本模块位置 → 推断 FDL 主目录与默认 config 路径（不硬编码绝对路径）
_MODULE_DIR = Path(__file__).resolve().parent  # .../0-SWE/2-FDL/fdl_core
_FDL_ROOT = _MODULE_DIR.parent  # .../0-SWE/2-FDL
_DEFAULT_CONFIG = _FDL_ROOT / "config" / "fdl_paths.yaml"


def load_config(config_path: str | Path | None = None) -> dict:
    """读取 fdl_paths.yaml（默认取 `{fdl_root}/config/fdl_paths.yaml`）。"""
    cfg = Path(config_path) if config_path else _DEFAULT_CONFIG
    return yaml.safe_load(cfg.read_text(encoding="utf-8"))


def locate_root(start: Path, anchor_rel: str) -> Path:
    """从 `start` 向上搜索，返回项目根（仓库根目录）。

    每一层同时检查 `.git`（优先）与 `anchor_rel` 锚文件，返回首个命中。
    无命中时回退到代码位置推断（`fdl_core` 的祖父目录）。
    """
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
        if anchor_rel and (d / anchor_rel).exists():
            return d
    # 最终回退：按代码位置推断（fdl_core 的祖父目录）
    return _FDL_ROOT.parents[1]


class FdlPaths:
    """FDL 路径解析器。

    示例::

        p = FdlPaths()
        p.fdl_root            # .../0-SWE/2-FDL
        p.subject_dir("MATH") # .../1-Math（layout_version=1）
        p.db_path             # .../0-SWE/2-FDL/data/fdl.db
    """

    def __init__(self, config_path: str | Path | None = None, root: str | Path | None = None):
        self.config = load_config(config_path)
        self._version = int(self.config["layout_version"])
        self._layout = self.config["layout"][f"v{self._version}"]
        anchor = str(self.config.get("anchor_file", "0-SWE/2-FDL/README.md"))
        # root 可注入（测试/特殊场景），默认按 .git 或锚文件定位
        self.root = Path(root) if root else locate_root(_MODULE_DIR, anchor)
        self.swe_dir = self.root / self._layout["swe_dir"]
        self.fdl_root = self.root / self._layout["fdl_dir"]

    # ── 布局版本 ──────────────────────────────────────────────
    @property
    def layout_version(self) -> int:
        return self._version

    # ── 学科目录 ──────────────────────────────────────────────
    @property
    def subject_codes(self) -> list[str]:
        """学科编码列表，与 subject_dirs 顺序一一对应（MATH/CHINESE/ENGLISH/SCIENCE）。"""
        return list(self.config.get("subject_codes", []))

    @property
    def subjects(self) -> dict[str, Path]:
        """学科编码 → 学科目录 映射（按 layout_version 解析）。"""
        dirs = self._layout["subject_dirs"]
        return {code: self.root / d for code, d in zip(self.subject_codes, dirs, strict=True)}

    def subject_dir(self, code: str) -> Path:
        """返回指定学科编码的目录（如 `MATH` → `1-Math/`）。"""
        return self.subjects[code.upper()]

    # ── 子目录 / 派生路径 ──────────────────────────────────────
    def subdir(self, name: str) -> Path:
        """FDL 子目录（data/logs/backups/config/...，相对 fdl_root）。"""
        return self.fdl_root / self.config["subdirs"][name]

    @property
    def db_path(self) -> Path:
        """SQLite 主库路径（PRD §5.2 `data/fdl.db`）。"""
        return self.subdir("data") / "fdl.db"

    @property
    def primary_db_path(self) -> Path:
        """🆕 高频写入主库（内置盘，SSD 过热 I/O 故障后迁移 2026-09-06）。

        路径由 config `primary_db_dir` 驱动（NFR-6 零硬编码）。
        `db_path`（外置 SSD）降级为每日同步镜像。
        """
        primary = self.config.get("primary_db_dir")
        if not primary:
            return self.db_path  # 未配置时回退原路径
        return Path(primary).expanduser() / "fdl.db"

    @property
    def config_dir(self) -> Path:
        return self.subdir("config")

    @property
    def anchor_path(self) -> Path:
        """锚文件绝对路径（SSD 双重判定用，见 OPS-03）。"""
        return self.root / self.config["anchor_file"]

    @property
    def ssd_mount(self) -> Path:
        """SSD 挂载点（git 根的父目录）。"""
        return self.root.parent


# 单例：模块级默认解析器（配置固定，进程内共享）
_default_paths: FdlPaths | None = None


def get_paths() -> FdlPaths:
    """返回进程级单例路径解析器。"""
    global _default_paths
    if _default_paths is None:
        _default_paths = FdlPaths()
    return _default_paths

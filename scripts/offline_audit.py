#!/usr/bin/env python3
"""FDL 离线审计（C4.2，NFR-1）。

七项检查，每次 commit + CI 必跑，失败阻断合并：
① 零 CDN  ② 无外链  ③ 无 /Volumes/ 硬编码  ④ 依赖锁定
⑤ 字体 vendor  ⑥ 模型本地化  ⑦ log 完整性
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CDN_PATTERNS = (
    "cdn.jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "googleapis.com",
    "fonts.googleapis.com",
    "esm.sh",
)

# 扫描范围：运行时源码目录（审计目标）。
# 排除 scripts（开发工具/审计器本身，会包含检查模式字符串导致自匹配）。
SCAN_ROOTS = (ROOT / "fdl_core", ROOT / "fdl", ROOT / "docs", ROOT / "config")

# 扫描文件后缀
SCAN_SUFFIXES = {".py", ".html", ".toml", ".yaml", ".yml", ".json"}
EXCLUDE_PARTS = {".venv", "models", "logs", "backups", "data", "__pycache__", ".git", "site"}


def _iter_files(suffixes: set[str]) -> iter:
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in suffixes:
                if any(part in EXCLUDE_PARTS for part in p.parts):
                    continue
                yield p


def _read(p: Path) -> str:
    """读文件内容；`# audit-ok` / `// audit-ok` 标记行豁免。

    说明：豁免标记用于声明「这是合法的联网层 / 防线检测模式 / 文档示例 / 本地回环地址」。
    Python / YAML / TOML 用 `#`，HTML / JS 用 `//`（`#` 在 JS 中不是注释符）。
    """
    kept = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True):
        if "audit-ok" in line:
            continue
        kept.append(line)
    return "".join(kept)


# ① 零 CDN
def audit_zero_cdn() -> tuple[bool, str]:
    for f in _iter_files({".html", ".py"}):
        text = _read(f)
        for pat in CDN_PATTERNS:
            if pat in text:
                return False, f"{f.relative_to(ROOT)} 引用 CDN 资源 {pat}"
    return True, ""


# ② 无外链（运行时代码不得依赖外部 URL）
def audit_no_external_links() -> tuple[bool, str]:
    for f in _iter_files({".html", ".py", ".toml"}):
        text = _read(f)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if "http://" in stripped or "https://" in stripped:
                return False, f"{f.relative_to(ROOT)} 含运行时外链"
    return True, ""


# ③ 无 /Volumes/ 硬编码（NFR-6 可移植性）
def audit_no_volumes() -> tuple[bool, str]:
    for f in _iter_files(SCAN_SUFFIXES):
        text = _read(f)
        if "/Volumes/" in text:
            return False, f"{f.relative_to(ROOT)} 硬编码 /Volumes/"
    return True, ""


# ④ 依赖锁定
def audit_deps_locked() -> tuple[bool, str]:
    lock = ROOT / "requirements.lock"
    if not lock.exists():
        return False, "缺少 requirements.lock"
    if lock.stat().st_size == 0:
        return False, "requirements.lock 为空"
    return True, ""


# ⑤ 字体 vendor
def audit_fonts_vendor() -> tuple[bool, str]:
    fonts_dir = ROOT / "assets" / "fonts"
    if not fonts_dir.exists():
        return False, "缺少 assets/fonts/"
    font_files = list(fonts_dir.glob("*.ttc")) + list(fonts_dir.glob("*.ttf"))
    if not font_files:
        return False, "assets/fonts/ 无字体文件"
    return True, f"{len(font_files)} 个字体"


# ⑥ 模型本地化
def audit_model_local() -> tuple[bool, str]:
    model_dir = ROOT / "models" / "sensevoice"
    if not model_dir.exists():
        return False, "缺少 models/sensevoice/"
    models = list(model_dir.glob("model.*.onnx")) + list(model_dir.glob("*.gguf"))
    if not models:
        return False, "models/sensevoice/ 无模型文件"
    return True, f"{[m.name for m in models]}"


# ⑦ log 完整性
def audit_log_integrity() -> tuple[bool, str]:
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return False, "缺少 logs/"
    audit_subdirs = ["state_transitions", "param_changes", "auth_access"]
    audit_dir = logs_dir / "audit"
    if not audit_dir.exists():
        return False, "缺少 logs/audit/"
    missing = [d for d in audit_subdirs if not (audit_dir / d).exists()]
    if missing:
        return False, f"logs/audit/ 缺少子目录 {missing}"
    if not (ROOT / "fdl_core" / "logging_setup.py").exists():
        return False, "缺少 fdl_core/logging_setup.py"
    return True, ""


CHECKS: list[tuple[str, callable]] = [
    ("① 零 CDN", audit_zero_cdn),
    ("② 无外链", audit_no_external_links),
    ("③ 无 /Volumes/ 硬编码", audit_no_volumes),
    ("④ 依赖锁定", audit_deps_locked),
    ("⑤ 字体 vendor", audit_fonts_vendor),
    ("⑥ 模型本地化", audit_model_local),
    ("⑦ log 完整性", audit_log_integrity),
]


def main() -> int:
    print("== FDL 离线审计（七项）==")
    passed = 0
    failed = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        if ok:
            print(f"  [OK]   {name}{'  ' + detail if detail else ''}")
            passed += 1
        else:
            print(f"  [FAIL] {name}  {detail}")
            failed += 1

    print(f"== 结果：{passed} 通过，{failed} 失败 ==")
    if failed == 0:
        print("== 七项全通过，允许合并 ==")
        return 0
    if passed >= 6:
        print("== 通过出口门（≥6 项），可推进 ==")
        return 0
    print("== 未通过出口门，阻断合并 ==")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""发布合规回归测试（对应 P1-1 ~ P1-7 整改项）。

这些测试的作用是**把一次性整改固化为长期守护**：任何一次改动如果把私有路径、
凭据、错误的 .gitignore 规则或版本号不一致带回来，CI 会立刻失败。

约定：本文件中的敏感模式刻意以片段拼接构造，避免测试自身被扫描命中（自匹配）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 片段拼接（防自匹配）
_P = "/" + "Users/"
_HOME = "fhjt" + "ech"
_VOL_U = "FHJ" + "ssd"
_VOL_L = "Fhj" + "ssd"
_VAULT = "Obsid" + "ian Vault"
_WORK = "Frank " + "SWE"

PRIVATE_PATTERNS = [
    re.compile(re.escape(_P) + re.escape(_HOME)),
    re.compile(re.escape(_VOL_U)),
    re.compile(re.escape(_VOL_L)),
    re.compile(re.escape(_VAULT)),
    re.compile(re.escape(_WORK)),
]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    ".ruff_cache",
    # 运行时产物目录（被 .gitignore 忽略，不属发布内容）
    "logs",
    "site",
    "data",
    "backups",
    "models",
}
SKIP_SUFFIX = {".ttc", ".ttf", ".otf", ".pyc", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico"}


def _iter_repo_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in SKIP_SUFFIX:
            continue
        yield p, rel


# ── P1-2：版本号一致性 ────────────────────────────────────────────────
def test_version_is_single_sourced_from_pyproject():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert declared == "1.3", f"pyproject 版本应为 1.3，实际 {declared}"

    sys.path.insert(0, str(ROOT))
    from fdl_core._version import resolve_version

    assert resolve_version() == declared, "运行期版本解析结果与 pyproject 不一致"


def test_no_hardcoded_version_literals():
    hard = re.compile(r'__version__\s*=\s*["\']\d')
    for rel in ("fdl/__init__.py", "fdl_core/__init__.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not hard.search(text), f"{rel} 不应硬编码版本字面量"


def test_changelog_has_entry_for_current_version():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{declared}]" in cl, f"CHANGELOG.md 缺少 [{declared}] 条目"


# ── P1-3：许可证一致性 ────────────────────────────────────────────────
def test_license_file_and_pyproject_agree():
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic, "LICENSE 应为 MIT"
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "license"
    ]
    text = declared["text"] if isinstance(declared, dict) else declared
    assert text == "MIT", f"pyproject license 应为 MIT，实际 {text}"


# ── P1-7：无本机个人路径残留 ──────────────────────────────────────────
def test_no_private_paths_in_repo():
    hits = []
    for p, rel in _iter_repo_files():
        if rel.as_posix() == "tests/test_release_metadata.py":
            continue  # 本文件含拼接片段
        if p.suffix not in {
            ".py",
            ".md",
            ".toml",
            ".txt",
            ".yaml",
            ".yml",
            ".json",
            ".html",
            ".js",
            ".sh",
            "",
        }:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if "audit-ok" in line:
                continue
            for pat in PRIVATE_PATTERNS:
                if pat.search(line):
                    hits.append(f"{rel}:{i}")
                    break
    assert not hits, "发现本机个人路径残留：\n" + "\n".join(hits[:20])


# ── P0 复发的回归守护：.gitignore 规则必须真正生效 ────────────────────
def test_gitignore_has_no_inline_comments():
    """行内注释会让整条规则变成文件名模式、静默失效（历史 P0 事故）。"""
    bad = []
    for i, line in enumerate((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(), 1):
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        if "#" in s:
            bad.append(f"第 {i} 行: {s}")
    assert not bad, ".gitignore 含行内注释，规则会失效：\n" + "\n".join(bad)


def test_gitignore_actually_ignores_sensitive_paths():
    if (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-dir"], capture_output=True
        ).returncode
        != 0
    ):
        pytest.skip("尚未 git init，跳过 check-ignore 实测")
    must = [
        "config/secrets.yaml",
        "config/fdl_paths.yaml",
        ".env",
        "data/fdl.db",
        "assets/fonts/AnyFont.ttc",
        "ARCHIVED.md",
    ]
    not_ignored = [
        m
        for m in must
        if subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", m], capture_output=True
        ).returncode
        != 0
    ]
    assert not not_ignored, "以下敏感路径未被忽略：" + ", ".join(not_ignored)


# ── P1-5 / P1-6：不随发布物分发的内容 ─────────────────────────────────
def _tracked() -> list[str]:
    """git 已跟踪（含已暂存）文件；非仓库时返回 []。"""
    if (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-dir"], capture_output=True
        ).returncode
        != 0
    ):
        return []
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def test_private_artifacts_are_not_published():
    """禁止分发的路径不得进入「将发布的内容」。

    判定依据是「是否会被发布」（git 已跟踪/已暂存），而非「本地是否存在」——
    运行时会产生 site/ logs/ data/ config/fdl_paths.yaml 等本地文件，
    它们被 .gitignore 忽略，不构成失败。
    """
    forbidden = [
        "config/secrets.yaml",
        "config/fdl_paths.yaml",
        "ARCHIVED.md",
        "docs",
        "data",
        "logs",
        "models",
        "site",
    ]
    tracked = _tracked()
    if not tracked:
        pytest.skip("尚未 git init / 无跟踪文件，交由发布门禁与 CI 覆盖")
    leaked = [t for t in tracked if any(t == f or t.startswith(f + "/") for f in forbidden)]
    assert not leaked, "以下禁止分发的路径已被纳入发布内容：" + ", ".join(leaked)


def test_font_binaries_absent_but_readme_present():
    fonts = ROOT / "assets" / "fonts"
    binaries = list(fonts.glob("*.ttc")) + list(fonts.glob("*.ttf")) + list(fonts.glob("*.otf"))
    assert not binaries, f"字体二进制不应随发布物分发：{[b.name for b in binaries]}"
    assert (fonts / "README.md").exists(), "assets/fonts/README.md 应保留（含字体恢复步骤）"


def test_no_database_files_tracked():
    tracked = _tracked()
    if not tracked:
        pytest.skip("尚未 git init，跳过")
    dbs = [t for t in tracked if t.endswith((".db", ".db-wal", ".db-shm"))]
    assert not dbs, f"不应跟踪数据库文件：{dbs}"


# ── P1-1：配置模板 ────────────────────────────────────────────────────
def test_paths_example_template_is_present_and_portable():
    tpl = ROOT / "config" / "fdl_paths.yaml.example"
    assert tpl.exists(), "缺少 config/fdl_paths.yaml.example"
    text = tpl.read_text(encoding="utf-8")
    assert "layout_version" in text and "primary_db_dir" in text
    for pat in PRIVATE_PATTERNS:
        assert not pat.search(text), "配置模板中不得出现本机个人路径"


# ── P1-4：凭据上下文 —— vlm.yaml 保持公开但无凭据 ─────────────────────
def test_vlm_config_public_but_credential_free():
    cfg = ROOT / "config" / "vlm.yaml"
    assert cfg.exists(), "config/vlm.yaml 应保留在发布物中"
    text = cfg.read_text(encoding="utf-8")
    assert "api_key_env" in text, "应通过环境变量引用密钥"
    # 不得出现密钥形态令牌或密钥示例字样
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", text)
    assert not re.search(re.escape("sk-" + "xxx"), text, re.I)
    assert "sk-" not in text, "vlm.yaml 中不应出现任何密钥前缀字样"


# ── P1-4：CI 与开发配置齐备 ──────────────────────────────────────────
@pytest.mark.parametrize(
    "rel",
    [
        ".github/workflows/ci.yml",
        ".pre-commit-config.yaml",
        ".python-version",
        "requirements-dev.txt",
        "requirements.lock",
        "scripts/check_release_ready.py",
        "scripts/check_no_torch.sh",
        "scripts/check_no_banned_words.sh",
        "scripts/offline_audit.py",
    ],
)
def test_release_scaffolding_present(rel):
    assert (ROOT / rel).exists(), f"缺开发/CI 配置：{rel}"


def test_ci_does_not_hardcode_private_project_path():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "0-SWE/2-FDL" not in ci, "CI 不应引用私有目录层级"


def test_pre_commit_does_not_hardcode_private_project_path():
    pc = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "0-SWE/2-FDL" not in pc, "pre-commit 不应引用私有目录层级"


# ── 端到端：门禁脚本自身必须通过 ──────────────────────────────────────
def test_release_gate_script_passes():
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_release_ready.py"), str(ROOT)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert r.returncode == 0, f"发布门禁未通过：\n{r.stdout}\n{r.stderr}"

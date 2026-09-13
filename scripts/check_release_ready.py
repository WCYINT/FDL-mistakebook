#!/usr/bin/env python3
"""FDL 发布就绪门禁（release gate）。

在推送前与 CI 中运行，逐项检查公开发布物是否满足：
  1. 无凭据 / 密钥形态令牌 / 私有配置文件
  2. 无本机个人路径（用户名、私有卷名、私有数据目录名）
  3. 版本号一致性（pyproject.toml 为唯一权威源）
  4. 许可证一致性（LICENSE 文件 vs pyproject license 字段）
  5. 无禁止分发的产物（数据库、字体二进制、内部文档、归档标记）
  6. .gitignore 规则有效（含"行内注释导致规则失效"这一类历史事故的回归守护）
  7. （--history）git 全历史无敏感内容

用法：
    python scripts/check_release_ready.py [仓库根目录] [--history]

退出码：0 = 全部通过；1 = 存在阻断项。
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = (
    Path(sys.argv[1]).resolve()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
    else Path.cwd()
)

SELF = "scripts/check_release_ready.py"

# 敏感模式刻意用片段拼接，避免本脚本自身被扫描命中
_USER = "/" + "Users/"
_HOME_MARK = "fhjt" + "ech"  # 构建者用户名
_PRIV_VOL_U = "FHJ" + "ssd"  # 私有卷名（大写形态）
_PRIV_VOL_L = "Fhj" + "ssd"  # 私有卷名（小写形态）
_VAULT = "Obsid" + "ian " + "Vault"  # 私有笔记库目录名
_WORK_TREE = "Frank " + "SWE"  # 私有工作根目录名
_KEY_EXAMPLE = "sk-" + "xxx"  # 密钥示例字样

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(re.escape(_KEY_EXAMPLE), re.I),
]

PRIVATE_PATH_PATTERNS = [
    re.compile(rf"{re.escape(_USER)}{re.escape(_HOME_MARK)}"),
    re.compile(re.escape(_PRIV_VOL_U)),
    re.compile(re.escape(_PRIV_VOL_L)),
    re.compile(re.escape(_VAULT)),
    re.compile(re.escape(_WORK_TREE)),
]

# 禁止随发布物分发的路径（glob，相对仓库根）
FORBIDDEN_PATHS = [
    "config/secrets.yaml",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "assets/fonts/*.ttc",
    "assets/fonts/*.ttf",
    "assets/fonts/*.otf",
    "ARCHIVED.md",
    "ARCHIVED.local-bak-*",
    "docs/*",
    "models/*",
    "data/*",
    "logs/*",
]

# 必须存在的文件（P1 清单的落地物）
REQUIRED_PATHS = [
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    ".gitignore",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.lock",
    ".python-version",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    "config/fdl_paths.yaml.example",
    "config/vlm.yaml",
    "scripts/check_release_ready.py",
    "tests/test_release_metadata.py",
]

# .gitignore 必须覆盖的敏感路径（防止历史 P0 事故复发）
MUST_BE_IGNORED = [
    "config/secrets.yaml",
    "config/fdl_paths.yaml",
    ".env",
    "data/fdl.db",
    "assets/fonts/AnyFont.ttc",
    "ARCHIVED.md",
    "ARCHIVED.local-bak-20260913_220055/x",
]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", ".ruff_cache"}
SKIP_SUFFIX = {
    ".ttc",
    ".ttf",
    ".otf",
    ".pyc",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".ico",
    ".zip",
    ".pdf",
}
TEXT_SUFFIX = {
    ".py",
    ".md",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".html",
    ".js",
    ".sh",
    ".cfg",
    ".ini",
    "",
    ".example",
    ".lock",
    ".in",
}

FAILS: list[str] = []
WARNS: list[str] = []
PASSES: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)


def ok(msg: str) -> None:
    PASSES.append(msg)


def warn(msg: str) -> None:
    WARNS.append(msg)


def iter_text_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() == SELF:
            continue  # 门禁脚本自身含检测模式，跳过
        if p.suffix in SKIP_SUFFIX:
            continue
        if p.suffix not in TEXT_SUFFIX:
            continue
        yield p, rel


def strip_tolerant(text: str) -> str:
    """去掉带 audit-ok 标记的行（合法防线/审计模式声明）。"""
    return "\n".join(ln for ln in text.splitlines() if "audit-ok" not in ln)


# ── ① 凭据扫描 ────────────────────────────────────────────────────────
def check_secrets() -> None:
    hits = []
    for p, rel in iter_text_files():
        text = strip_tolerant(p.read_text(encoding="utf-8", errors="ignore"))
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{rel}: 疑似凭据 {m.group(0)[:24]}")
    if hits:
        fail("① 凭据扫描：\n    " + "\n    ".join(hits))
    else:
        ok("① 凭据扫描：未发现密钥形态令牌 / 密钥示例字样")


# ── ② 私有路径扫描 ────────────────────────────────────────────────────
def check_private_paths() -> None:
    hits = []
    for p, rel in iter_text_files():
        raw = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(raw.splitlines(), 1):
            if "audit-ok" in line:
                continue
            for pat in PRIVATE_PATH_PATTERNS:
                m = pat.search(line)
                if m:
                    hits.append(f"{rel}:{i}: {m.group(0)}  ← {line.strip()[:90]}")
                    break
    if hits:
        fail("② 私有路径扫描：\n    " + "\n    ".join(hits))
    else:
        ok("② 私有路径扫描：无本机用户名 / 私有卷名 / 私有数据目录残留")


# ── ③ 版本号一致性 ────────────────────────────────────────────────────
def _pyproject_version() -> str | None:
    try:
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception as e:  # pragma: no cover
        fail(f"③ 版本号：无法解析 pyproject.toml（{e}）")
        return None


def check_version() -> None:
    ver = _pyproject_version()
    if not ver:
        return
    ok(f"③ 版本号：pyproject.toml 权威源 = {ver}")

    # 禁止在 __init__.py / _version.py 中硬编码版本字面量
    hard = re.compile(r'__version__\s*=\s*["\']\d')
    for rel in ("fdl/__init__.py", "fdl_core/__init__.py", "fdl_core/_version.py"):
        f = ROOT / rel
        if not f.exists():
            fail(f"③ 版本号：缺少 {rel}")
            continue
        if hard.search(f.read_text(encoding="utf-8")):
            fail(f"③ 版本号：{rel} 硬编码了版本字面量（应派生自 pyproject.toml）")

    # CHANGELOG 必须有对应条目
    cl = ROOT / "CHANGELOG.md"
    if not cl.exists():
        fail("③ 版本号：缺少 CHANGELOG.md")
    elif f"[{ver}]" not in cl.read_text(encoding="utf-8"):
        fail(f"③ 版本号：CHANGELOG.md 中找不到 `[{ver}]` 条目")
    else:
        ok(f"③ 版本号：CHANGELOG.md 含 [{ver}] 条目")

    # __version__ 运行期一致性
    sys.path.insert(0, str(ROOT))
    try:
        from fdl_core._version import resolve_version

        live = resolve_version()
        # 未安装时 resolve_version 读 pyproject，两者必须一致
        if live != "0.0.0+unknown" and live != ver:
            fail(f"③ 版本号：运行期 __version__={live} 与 pyproject={ver} 不一致")
        else:
            ok(f"③ 版本号：运行期 __version__ 与 pyproject 一致（{live}）")
    except Exception as e:
        warn(f"③ 版本号：无法导入 fdl_core._version 做运行期校验（{e}）")


# ── ④ 许可证一致性 ────────────────────────────────────────────────────
def check_license() -> None:
    lic = ROOT / "LICENSE"
    if not lic.exists():
        fail("④ 许可证：缺少 LICENSE 文件")
        return
    text = lic.read_text(encoding="utf-8")
    expected = None
    for name in ("MIT License", "Apache License", "GNU GENERAL PUBLIC LICENSE"):
        if name in text:
            expected = {
                "MIT License": "MIT",
                "Apache License": "Apache-2.0",
                "GNU GENERAL PUBLIC LICENSE": "GPL",
            }[name]
            break
    if expected is None:
        fail("④ 许可证：无法从 LICENSE 文件识别许可证类型")
        return
    try:
        import tomllib

        declared = str(
            tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
                "license"
            ]["text"]
        )
    except Exception:
        try:
            import tomllib

            declared = str(
                tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
                    "license"
                ]
            )
        except Exception as e:
            fail(f"④ 许可证：无法读取 pyproject license 字段（{e}）")
            return
    if declared != expected:
        fail(f"④ 许可证：pyproject license={declared} 与 LICENSE 文件（{expected}）不一致")
    else:
        ok(f"④ 许可证：LICENSE 与 pyproject 一致（{expected}）")


# ── ⑤ 禁止分发的产物 ──────────────────────────────────────────────────
def _tracked_paths() -> list[str] | None:
    """返回 git 已跟踪（含已暂存）文件列表；非 git 仓库返回 None。"""
    if (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-dir"], capture_output=True
        ).returncode
        != 0
    ):
        return None
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _matches_forbidden(rel: str) -> bool:
    return any(
        fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + "/*") for pat in FORBIDDEN_PATHS
    )


def check_forbidden_artifacts() -> None:
    """判定「会不会被发布出去」，而非「本地是否存在」。

    运行时会产生 data/ logs/ site/ config/fdl_paths.yaml 等本地文件，它们被
    .gitignore 忽略、不会进入提交，因此不构成本地即失败。在 git 仓库中只检查
    已跟踪/已暂存文件；非仓库场景才回退为存在性检查。
    """
    tracked = _tracked_paths()
    if tracked is None:
        hits = [rel.as_posix() for _p, rel in _all_files() if _matches_forbidden(rel.as_posix())]
        scope = "工作区（非 git 仓库，按存在性检查）"
    else:
        hits = [p for p in tracked if _matches_forbidden(p)]
        scope = f"git 已跟踪文件（{len(tracked)} 个）"

    if hits:
        fail(
            f"⑤ 发布物内容：{scope} 中出现禁止分发的路径：\n    "
            + "\n    ".join(sorted(set(hits))[:20])
        )
    else:
        ok(f"⑤ 发布物内容：{scope} 无数据库 / 字体二进制 / 内部文档 / 模型 / 归档标记 / 凭据配置")

    missing = [r for r in REQUIRED_PATHS if not (ROOT / r).exists()]
    if missing:
        fail("⑤ 发布物内容：缺少必需文件：\n    " + "\n    ".join(missing))
    else:
        ok(f"⑤ 发布物内容：{len(REQUIRED_PATHS)} 个必需文件齐备")


def _all_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


# ── ⑥ .gitignore 有效性 ───────────────────────────────────────────────
def check_gitignore() -> None:
    gi = ROOT / ".gitignore"
    if not gi.exists():
        fail("⑥ .gitignore：文件缺失")
        return

    # 6a) 结构检查：不允许"规则后跟行内注释"（.gitignore 不支持行内注释）
    bad_lines = []
    for i, line in enumerate(gi.read_text(encoding="utf-8").splitlines(), 1):
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        if "#" in s:
            bad_lines.append(f"第 {i} 行: {s}")
    if bad_lines:
        fail(
            "⑥ .gitignore：以下规则含行内注释（.gitignore 不支持行内注释，整行会成为文件名模式，规则静默失效）：\n    "
            + "\n    ".join(bad_lines)
        )
    else:
        ok("⑥ .gitignore：所有规则均独占一行，无行内注释")

    # 6b) 生效性检查：用 git check-ignore 实测
    in_repo = (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    if not in_repo:
        warn("⑥ .gitignore：当前不是 git 工作区，跳过 check-ignore 实测（git init 后会由 CI 覆盖）")
        return
    not_ignored = []
    for p in MUST_BE_IGNORED:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", p], capture_output=True, text=True
        )
        if r.returncode != 0:
            not_ignored.append(p)
    if not_ignored:
        fail(
            "⑥ .gitignore：以下敏感路径未被忽略（实测 check-ignore 不匹配）：\n    "
            + "\n    ".join(not_ignored)
        )
    else:
        ok(f"⑥ .gitignore：{len(MUST_BE_IGNORED)} 条敏感路径实测均被忽略")


# ── ⑦ git 历史扫描 ────────────────────────────────────────────────────
def check_history() -> None:
    G = ["git", "-C", str(ROOT)]
    if subprocess.run([*G, "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
        fail("⑦ git 历史：不是 git 仓库，无法扫描（请先 git init）")
        return
    refs = subprocess.run([*G, "rev-list", "--all"], capture_output=True, text=True).stdout.split()
    if not refs:
        warn("⑦ git 历史：仓库尚无提交，跳过")
        return

    names = subprocess.run(
        [*G, "log", "--all", "--pretty=format:", "--name-only"], capture_output=True, text=True
    ).stdout
    bad_paths = sorted(
        {
            n
            for n in names.split()
            if any(
                fnmatch.fnmatch(n, pat) or fnmatch.fnmatch(n, pat + "/*") for pat in FORBIDDEN_PATHS
            )
        }
    )
    if bad_paths:
        fail("⑦ git 历史：历史中出现禁止分发的路径：\n    " + "\n    ".join(bad_paths[:20]))

    # 逐 blob 内容扫描
    obj_out = subprocess.run(
        [*G, "rev-list", "--objects", "--all"], capture_output=True, text=True
    ).stdout
    shas = [ln.split(maxsplit=1)[0] for ln in obj_out.splitlines() if ln.strip()]
    hits = []
    for sha in shas:
        t = subprocess.run(
            [*G, "cat-file", "-t", sha], capture_output=True, text=True
        ).stdout.strip()
        if t != "blob":
            continue
        blob = subprocess.run([*G, "cat-file", "blob", sha], capture_output=True).stdout
        if len(blob) > 2_000_000:
            continue
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text = strip_tolerant(text)
        for pat in SECRET_PATTERNS + PRIVATE_PATH_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append(f"{sha[:10]}: {m.group(0)}")
                break
    if hits:
        fail("⑦ git 历史：历史中存在敏感内容：\n    " + "\n    ".join(hits[:20]))
    elif not bad_paths:
        ok(f"⑦ git 历史：{len(shas)} 个对象扫描完毕，无敏感内容 / 无禁止路径")


# ── ⑧ 离线审计（仅开源检出适用的前四项）────────────────────────────────
# offline_audit.py 的 ⑤⑥⑦（字体 vendor / 模型本地化 / log 完整性）面向完整本地
# 部署环境，开源仓库按设计不含 macOS 系统字体（版权）、ASR 模型与运行日志。
# 这里强制要求 ①②③④（零 CDN / 无外链 / 无挂载点硬编码 / 依赖锁定）必须全部通过。
OFFLINE_APPLICABLE = ("① 零 CDN", "② 无外链", "③ 无 /Volumes/ 硬编码", "④ 依赖锁定")


def check_offline_applicable() -> None:
    script = ROOT / "scripts" / "offline_audit.py"
    if not script.exists():
        warn("⑧ 离线审计：未找到 scripts/offline_audit.py，跳过")
        return
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=str(ROOT))
    out = r.stdout + r.stderr
    failed = []
    for name in OFFLINE_APPLICABLE:
        line = next((ln for ln in out.splitlines() if name in ln), "")
        if not line:
            failed.append(f"{name}（未出现在审计输出中）")
        elif "[FAIL]" in line:
            failed.append(line.strip())
    if failed:
        fail("⑧ 离线审计（开源检出适用项）：\n    " + "\n    ".join(failed))
    else:
        ok(f"⑧ 离线审计：{'、'.join(OFFLINE_APPLICABLE)} 全部通过（⑤⑥⑦ 面向完整本地部署，豁免）")


def main() -> int:
    print(f"== FDL 发布就绪门禁 ==  根目录：{ROOT}")
    check_secrets()
    check_private_paths()
    check_version()
    check_license()
    check_forbidden_artifacts()
    check_gitignore()
    check_offline_applicable()
    if "--history" in sys.argv:
        check_history()

    for m in PASSES:
        print(f"  [OK]   {m}")
    for m in WARNS:
        print(f"  [WARN] {m}")
    for m in FAILS:
        print(f"  [FAIL] {m}")

    print(f"== 结果：{len(PASSES)} 通过，{len(WARNS)} 警告，{len(FAILS)} 失败 ==")
    if FAILS:
        print("== 未通过发布门禁，阻断推送 ==")
        return 1
    print("== 通过发布门禁 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())

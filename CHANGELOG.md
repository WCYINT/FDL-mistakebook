# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

版本号唯一权威源：`pyproject.toml` 的 `[project] version`。

---

## [1.3] — 2026-09-13

首个独立公开发布版本（v1.2.1 之后的首次发布）。

### 新增

- 间隔重复调度器 `fdl_core/srs/queue_scheduler.py` —— 由复习队列驱动的间隔调度。
- 归因引擎 `fdl_core/mistakes/attribution_engine.py` + 归因分类法 `attribution_taxonomy.py`。
- 反馈闭环 `fdl_core/mistakes/feedback_loop.py` 与干预机制 `fdl_core/mistakes/intervention.py`。
- 知识点挂载 `fdl_core/mistakes/kp_matcher.py`；知识点分类/维护/关联 `fdl_core/notes/kp_*.py`。
- 学习星图 `fdl_core/notes/starmap.py`。
- 净化体检 `scripts/fdl_self_purify.py`。
- 本地分析服务 `scripts/fdl_serve.py`（含报告页编辑与图片回看接口）。
- 周报 `scripts/fdl_weekly_report.py`。
- 数据库迁移框架 `fdl_core/migrations/`（含 2026-09 系列迁移脚本）。
- A2/A3 双阶段调度：`fdl_core/srs/a2_upgrade.py`、`graduation.py`、`session_orchestrator.py`。
- A2 升级持久化测试、反馈闭环测试、kp_state 重建测试等（详见 `tests/`）。
- 离线审计七项（E2E 层）：`scripts/offline_audit.py`。
- CI 工作流 `.github/workflows/ci.yml`、pre-commit 配置 `.pre-commit-config.yaml`。

### 变更

- **版本号单一化（P1-2）**：`pyproject.toml` 的 `version` 成为唯一权威源，`fdl.__version__` /
  `fdl_core.__version__` 改为派生（`fdl_core/_version.py`）。此前 `pyproject.toml` 写 `0.1.0`
  而仓库发布标签为 `v1.2.1`，两处不一致。本版统一为 `1.3`。
- **许可证归位（P1-3）**：`pyproject.toml` 的 `license` 由 `Proprietary` 改为 `MIT`，与仓库
  根 `LICENSE` 文件（MIT）一致。此前二者自相矛盾。
- **配置模板恢复（P1-1）**：新增 `config/fdl_paths.yaml.example`。真实
  `config/fdl_paths.yaml` 因含本机绝对路径而被 `.gitignore` 忽略，此前该目录下唯一的
  已跟踪文件被误删，导致新使用者无模板可依。
- **打包配置修正**：`[tool.setuptools]` 由显式 6 个包的 `packages` 列表改为
  `[tool.setuptools.packages.find]` 自动发现，避免子包漏装。
- **文档目录不再随发布物分发（P1-5）**：内部研究笔记（`docs/`）属过程性材料，不含在公开发布物中。
- **新增 `fdl_core/_version.py`**：版本号派生模块。

### 安全 / 隐私（本次发布的关键整改）

- **移除全部本机个人路径**：共 27 处替换，涉及 `fdl_core/report/template.html`、
  `scripts/fdl_serve.py`、`scripts/wire_launchd.sh`、`scripts/asr_ingest.py`、
  `scripts/verify_*_render.js`、`fdl/ui/i18n/zh.yaml` 及 `tests/` 中多个文件。
  改为可移植写法（脚本相对定位 / 环境变量覆盖 / 统一的 `conftest.py` 路径引导）。
- **`.gitignore` 规则修复**：原配置在 `config/secrets.yaml` 规则后写了行内注释，
  而 `.gitignore` **不支持行内注释** —— 整行被当作文件名模式，导致该凭据文件实际
  未被忽略。本版重写 `.gitignore`，每条规则独占一行，并新增回归测试守护
  （`tests/test_release_metadata.py::test_gitignore_rules_are_effective`）。
- **`config/vlm.yaml` 去除密钥示例**：删除文中的密钥形态示例字样，明确标注
  「所有密钥仅经环境变量注入、不落盘」。
- **不随发布物分发的内容**：数据库文件（`*.db`）、错题照片、字体 vendor
  （macOS 系统字体，含版权）、ASR 模型、日志、备份、`config/secrets.yaml`。

### 已知事项

- `assets/fonts/` 仅提供 `README.md`（含恢复步骤），字体二进制不随仓库分发。
- `scripts/offline_audit.py` 的第二、三项检查（字体 vendor / 模型本地化 / 日志完整性）
  面向完整本地部署环境，在纯源码检出下不适用；CI 中该步骤以非阻断方式运行。
- 部分测试依赖私有数据（真实错题照片、项目 PRD 文档等），缺失时自动跳过而非失败。

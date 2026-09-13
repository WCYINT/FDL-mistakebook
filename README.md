# FDL — Frank Deep Learning

个人记忆与理解资产系统。

> 用间隔重复把「学过」变成「掌握」，用行为化归因把「错了」变成「知道为什么错」，用探究式追问把「会了」变成「想再往下一层」。

**版本 1.3** ｜ MIT License ｜ 100% 离线可运行

## 定位

- 场景：K12 学科错题的采集 → 归因 → 间隔重复复习 → 掌握度度量
- 北极星指标：周净掌握知识点数 NMKP
- 设计约束：离线优先（无 CDN、无运行时外链）、儿童友好文案（禁用负面评判词）

## 目录结构

```
.
├── fdl_core/            # L0 业务内核（100% 离线）
│   ├── authz.py         # 数据层权限守门（NFR-8）
│   ├── logging_setup.py # 日志按月轮转（C6.1）
│   ├── alerts/          # 三条红线告警（C6.3）
│   ├── ingest/          # 采集：预处理 / 红黑分离 / OCR / VLM 整页直读
│   ├── mistakes/        # 归因引擎 / 反馈闭环 / 干预 / KP 挂载 / 复习状态
│   ├── notes/           # 知识点：分类 / 关联 / 星图 / 维护
│   ├── srs/             # 间隔重复：调度器 / FSRS A2 升级 / 毕业 / 时间层
│   ├── migrations/      # 数据库迁移框架与迁移脚本
│   ├── metrics/ report/ # 指标聚合与报告渲染
│   └── ops/             # 备份 / 磁盘健康检查
├── fdl/                 # CLI 包（ingest/review/metrics/doctor/backup/restore）
│   ├── cli/             # 子命令实现
│   └── ui/              # NiceGUI 界面 + i18n 文案表
├── tests/               # pytest（含 shadow 影子测试 + e2e）
├── scripts/             # 工具脚本（bootstrap / offline_audit / fdl_serve / check_*）
├── config/              # fdl_paths.yaml.example（布局开关）/ vlm.yaml / model_params.yaml
├── assets/fonts/        # 字体说明（字体二进制由本机系统提供，见该目录 README）
├── .github/workflows/   # CI
├── requirements.txt     # 运行时依赖（精确版本）
├── requirements-dev.txt # 开发/测试依赖
└── requirements.lock    # 锁定文件
```

> 数据（`data/`）、日志（`logs/`）、备份（`backups/`）、ASR 模型（`models/`）与
> 报告页（`site/`）为运行时产物，不随仓库分发，均已列入 `.gitignore`。

## 快速开始

```bash
git clone <本仓库地址> && cd <仓库目录>

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -e .

cp config/fdl_paths.yaml.example config/fdl_paths.yaml   # 按本机情况修改
fdl --help                                               # 查看子命令
python scripts/launch_ui.py                              # 启动界面
```

字体：离线渲染中文报告需要字体文件。仓库不含字体二进制（macOS 系统字体含版权，
不随分发）。按 `assets/fonts/README.md` 中的命令从本机系统字体目录恢复即可。

## 质量门禁

```bash
pytest                                  # 单元测试
python scripts/check_release_ready.py . # 发布就绪门禁（凭据 / 私有路径 / 版本 / 许可证 / gitignore）
bash scripts/check_no_torch.sh          # 禁 PyTorch（§4.4 #15）
bash scripts/check_no_banned_words.sh   # 儿童友好禁用词扫描（UX-05/09）
python scripts/offline_audit.py         # 离线审计七项（NFR-1）
```

> 离线审计中的「字体 vendor / 模型本地化 / 日志完整性」三项面向完整本地部署环境，
> 纯源码检出下不适用，CI 中以非阻断方式运行。

## 隐私与合规

本仓库为公开发布物，**不含**任何凭据、本机绝对路径或个人数据：

- 所有密钥仅通过环境变量注入（见 `config/vlm.yaml` 的 `api_key_env`），不落盘、不入库。
- `config/fdl_paths.yaml`（含本机路径）与 `config/secrets.yaml` 均被 `.gitignore` 忽略，
  仓库只提供 `config/fdl_paths.yaml.example` 模板。
- 错题照片、数据库文件、报告页与内部研究文档不在发布范围内。

## 开发状态

核心链路（采集 → 归因 → 调度 → 度量 → 报告）已可用；见 `CHANGELOG.md`。

## 许可证

[MIT](LICENSE)

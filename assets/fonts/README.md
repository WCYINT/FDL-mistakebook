# FDL 字体 vendor（C1.4）

本地化字体，用于离线渲染中文报告（MkDocs / matplotlib / NiceGUI），不依赖系统字体（NFR-1 离线审计第⑤项「字体 vendor」）。

## 字体清单

| 文件 | 字体 | 用途 | 来源 |
|---|---|---|---|
| Hiragino Sans GB.ttc | 冬青黑体 | 中文无衬线正文 / UI | macOS 系统字体 |
| STHeiti Medium.ttc | 华文黑体 | 中文黑体标题 | macOS 系统字体 |
| Songti.ttc | 宋体 | 中文衬线教材正文 | macOS 系统字体 |
| Arial Unicode.ttf | Arial Unicode | 英文 / 符号兜底 | macOS 系统字体 |

## 说明

- 计划原要求「中易宋体 / PingFang」，但 macOS 26.6.2 已移除 PingFang（中文无衬线改用冬青黑体），中易宋体（SimSun）为 Windows 字体。故以 macOS 原生等价字体替代。
- 字体文件约 162MB，不纳入 git 版本控制（`.gitignore` 已忽略），可从系统用以下命令恢复：

```bash
cp "/System/Library/Fonts/Hiragino Sans GB.ttc"          assets/fonts/
cp "/System/Library/Fonts/STHeiti Medium.ttc"            assets/fonts/
cp "/System/Library/Fonts/Supplemental/Songti.ttc"        assets/fonts/
cp "/System/Library/Fonts/Supplemental/Arial Unicode.ttf" assets/fonts/
```

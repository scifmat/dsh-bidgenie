# 变更日志

本文件记录 `dsh-bidgenie` 每个对外发布版本的变更。版本号规则见开发工作区 `docs/BidGenie-版本号与发布计划-20261128.md`；历史以 git tag + 本文件为准。

## [0.1.0-alpha.1] - 2026-11-28

首个带 tag 的内测版本（Alpha），基线为 `0.1.0-alpha.0`。包含第三轮冒烟修复与发布资料整改。

### Added

- 转换时默认把原始招标文件复制到 `sources/raw/`，并在转换清单写入 `rawCopyPath`（可用 `--no-copy-raw` 关闭）。
- `bg_state.py check` 返回下一个包 `nextScope`，切换提示更明确。
- `bg_review_issues.py verify` 失败时返回 `remediation`，直接给出可执行的修复命令。
- 审查报告新增只读「跨章数量索引」，供跨章数量核对。
- `bg_render` 预判给出 `lrAutoRevert` 与 TB 近线性链高宽比估算，避免无效的布局调整建议。
- 插件面板补充应用图标与中英文标题、描述。

### Changed

- 定稿锁定默认原因改为「定稿锁定（Agent 执行）」。
- 写作批次、多轮审查、优化支持复用同一子智能体（技能中的可选建议）。
- 发布仓库 README 重写为面向用户的能力 / 安装 / 使用 / 边界说明。

### Fixed

- 修复沙箱环境下脚本自检临时目录 `[WinError 5] 拒绝访问`：统一改用 `_lib/tempdir`。
- 修复 auto 模式下人员 / 设备角色文案的错误拼接与引号不配平。
- 修复大纲 `--init-draft` 图题按标题语义生成后仍被误判为编号的问题提示链路。

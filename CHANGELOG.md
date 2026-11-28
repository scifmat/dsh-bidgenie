# 变更日志

本文件记录 `dsh-bidgenie` 每个对外发布版本的变更。版本号规则见开发工作区 `docs/BidGenie-版本号与发布计划-20261128.md`；历史以 git tag + 本文件为准。

## [0.1.0-alpha.4] - 2026-11-28

README / 发布资料更新（无功能变更）。

### Changed

- 插件 README 徽章改为同一行（修复 DSH 插件详情页里徽章竖排、间距不均的问题），并保留 npm 徽章。
- 安装说明以 npm 包名 `dsh-bidgenie` 为主，GitHub 地址作为备选；说明 npm 安装才会出现「更新」提示。
- 同步刷新 npm 页面 README（alpha.3 打包后 README 才更新，本次补上）。

## [0.1.0-alpha.3] - 2026-11-28

首个 npm 发布版本（staged publish）。功能与 `0.1.0-alpha.2` 一致，仅增加发布能力。

### Changed

- 移除 `package.json` 的 `private` 限制并声明 `publishConfig.access=public`，可按 `dsh-bidgenie` 从 npm 安装/更新。
- npm 包内容：135 个文件、约 2.4 MB（运行时代码、技能、模板、assets、tools、README/CHANGELOG/LICENSE；不含 tests/pylibs）。

## [0.1.0-alpha.2] - 2026-11-28

修复 VM 流程冒烟（0.1.0-alpha.1）发现的 2 个 P0 与若干体验问题。

### Added

- 环境检查识别「依赖目录被 DSH 沙箱拒绝」（`sandbox_permission`）：给出「切完全权限」的明确处置，不再抛 traceback、不再误判为依赖缺失。
- 提取校验新增 `fallbackReview.summary`（区分转换清单新发现 / 提取稿已标注风险，含 3 行摘要）与 `sharedExtractionFiles`（多包共用文件可复用提示）。
- `--init-draft` 返回 `rewriteHints`（需改写标题/图表题注数量、与父级同名清单）。
- `--all-selected` 返回 `autoPlan`（各包人员/设备推导粒度差异）。
- run-manifest 与 `_latest.json` 增加 `runStatus`（进行中 / 已收尾）。

### Changed

- 沙箱权限处理改为技能引导：识别到 `WinError 5/拒绝访问` 时请求用户把会话切「完全权限」后重跑；不重装依赖、不逐条升级授权。
- `--init-draft` 的叶子标题与父级同名时自动补「（分项响应）」；图表 purpose 改为按图类型的具体说明。
- auto 模式按项目类型给终端图参考值：施工/货物类默认 0，软件/系统类保持默认。

### Fixed

- 特殊章节 `coverage=notApplicable` 不再被 `bg_finalize --lock` 误挡（`REVIEW_COVERAGE` 已纳入）。
- `--brief` 截断数组时补 `<key>Count`，避免 `chapters: [4 项]` 被误读成只有 4 章。

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

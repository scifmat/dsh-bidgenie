---
name: bg-optimize
description: BidGenie 技术方案优化（F6.4/F6.5）。当项目处于 optimize 阶段，需要按 issues 分级修复章节、记录修改、关闭问题并完成 T8 回到下一轮审查时使用；优化正文、修复审查问题、补充章节内容、统一跨章口径时加载。
---

# bg-optimize（F6 优化）

## 原则

- 只处理 `issues.json` 中 `status ∈ {open, fixing}` 的问题；只改问题涉及的章节/资源，不顺手重写无关内容。
- 修复后只写 `fixed` + `resolutionNote`；`verified` 由下一轮审查写，不能自证。
- 结构化问题（缺章、大纲/配额本身错误）不在优化内改，停下来走 R1。

## 范围

- 只改技术正文与图表资源；不改 outline/不新增章节/不改配额/不改 package-meta。
- 图表类问题先加载 `bg-figures`：只改布局/拆图/精简标签/删减终端行，不新增未规划的图，不编造数据。
- 文风类修改按 `bg-writing-style` 执行，不复制规则。
- 商务/报价内容不修改。

## 进入

- `currentStage == optimize`（T6 已迁移）；issues.json 合法；`draftLocked` 不为 true；`bg_review_issues.py` 就绪。

## 分组与修复

1. 按 severity：blocker → major → minor；同章/相邻主题合并给**一个**优化子智能体（每批约 5–10 issue 或 3–5 章），禁止每 issue 一个。
2. 每问题：`fixing` → 修改 → `fixed` + `resolutionNote`（做了什么/文件/前后字数）；每批生成 `rounds/round-<k>.optimization.json`。
3. blocker 必修复；无法在现有大纲内修复 → 停走 R1；major 补内容/换图；minor 微调。
4. 允许统一口径（响应时限、自设指标、模板化开头），但必须记录统一前后与涉及章节，不静默、不扩范围。
5. **字数类问题按诊断定点修**：先看 `bg_chapter_merge.py` 输出的 `wordCountDiagnostics.chaptersOverTarget/UnderTarget`（或 review 的逐章字数），只改超出目标最多的章节；不要整包重写。
6. **优化子智能体不渲染 png**：只改章节 md、`.mmd`、`.txt` 源文件；不要运行 `bg_render.py`，也不要在包内/临时目录留下预览 png。需要预览时由主 Agent 在全部修复后统一渲染一次，并清理临时产物。

## M6 遗留专项

- 响应时限统一到商务承诺档位（以提取稿为准），各章同一口径。
- 招标 09 无明确数值的自设指标标注「内部管控目标，不替代招标承诺」。
- 模板化开头/机械列举按 `bg-writing-style` 改。

## T8

- `bg_review_issues.py status` 确认 `openCount==0`、`verify` 通过 → `bg_state.py check --to review` → `transition --to review`；下一轮加载 `bg-review`。
- 发现新结构性问题 → 停止，报告用户走 R1。
- 迁移后断言 `currentStage=="review"`；不成立报 `E_STATE_CORRUPT`。

## 硬约束

1. 只改问题涉及文件；不自证 verified；不改 outline/配额/meta。
2. 统一口径要留记录（issue note + round 快照）。
3. 锁定态禁止写正文（`E_LOCKED`）。
4. 状态只由脚本写；T8 前置 open 清零 + check 通过。
5. 文风引用 `bg-writing-style`；不承诺 package-meta 之外的人/设备/日期。
6. 渲染只由主 Agent 执行；子智能体不产 png、不留临时文件。

## 汇报

本轮修复数/剩余 open、统一项清单、涉及文件与 optimization.json 路径、下一动作（T8 或 R1）。

## 参考

- 命令与异常：`references/cli.md`（按需读）
- 运行时路径见文末 Host 注入块。

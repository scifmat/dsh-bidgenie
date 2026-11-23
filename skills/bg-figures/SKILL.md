---
name: bg-figures
description: BidGenie 图表规划与生成规则（F5-1 大纲必用，F5-2/F6/F6.4 共用）。当需要选择 mermaid 图类型、规划每章图表数量、写终端图、判断图表是否契合正文或需要修正布局时使用。
---

# bg-figures（图表规划/生成/审查规则）

## 使用时机

- **F5-1 大纲**：图表规划前必须加载本技能，按第 1 节规划 `figurePlan`。
- **F5-2 正文**：按大纲的 `type/subtype/purpose` 生成 `.mmd/.txt`，按第 3 节控制布局与篇幅。
- **F6 审查**：C 层审图按第 4 节核对“图型/内容/位置是否契合”；A 层机检指标作为参考，不替代人工审图。
- **F6.4 优化**：只改布局/拆图/修正内容，不新增未规划的图。

## 1. 规划规则（F5-1）

### 1.1 图型与数量

- `type` 只取 `table`、`mermaid`、`terminal`。
- `mermaid` 必须写 `subtype`：`flowchart`、`sequence`、`gantt`、`state`、`er`、`timeline`、`mindmap`、`block`、`architecture`、`quadrant`、`pie`、`xychart`。
- 每末级章节：`table ≤2`、`mermaid ≤2`、`terminal ≤4`。
- `table/mermaid` 规划数不低于 `figureQuota`；`terminal` 是参考值（0=不生成），按适合章节实际规划，**不按章节平均分配、不为凑数硬塞**。
- 一个章节适合时可以规划多张 terminal（同一场景的多组命令）；不适合的章节一张也不放。
- **风险提示**：政府采购技术方案评委未必熟悉终端截图；除非该章确实在演示操作/命令（或用户明确要求），优先不规划或少规划 terminal，别把参考值当硬指标。
- 特殊章节不规划图。
- 每张图必须有 `caption`（纯题名）和 `purpose`（这张图要证明/说明什么），不能只写题名。

### 1.2 规划粒度

- 只规划“章、type/subtype、数量、题名、purpose”；不写具体命令、输出、节点文案（写作阶段填）。
- 只有章节的 `contentRequirements` 或评分因素能支撑时才规划；拿不准的宁可不规划。
- 规划后写作阶段不重新设计图型；确实缺图回大纲调整。

## 2. 类型选择

| 内容意图 | 首选 `subtype` | 说明 |
|---|---|---|
| 流程、判定分支、作业步骤 | `flowchart` | 默认 TB 纵向 |
| 多角色/系统交互、接口时序、故障处置时序 | `sequence` | 适合联调、派单、应急联动 |
| 工期、季度排期、培训/演练计划 | `gantt` | 日期必须有来源，不得虚构 |
| 设备/工单/告警状态流转 | `state` | 状态+迁移 |
| 数据表关系、数据字典 | `er` | 数据库/平台设计 |
| 需求、风险、知识结构拆分 | `mindmap` | 重点难点/风险分析 |
| 里程碑、发展历程、年度节奏 | `timeline` | 不需要精确日期 |
| 系统模块、平台分层、部署架构 | `block` / `architecture` | 架构类优先 |
| 风险分级、优先级矩阵 | `quadrant` | 风险/问题分级 |
| 占比、趋势（有可靠数据） | `pie` / `xychart` | 数据必须有来源 |

不列入：`gitGraph`、`classDiagram`、`journey`、`requirementDiagram`、`C4Context`。

## 3. 生成与布局

### 3.1 mermaid

- 默认 `flowchart TB`；不要用长链 `LR`。
- 单图建议 ≤12 个节点；超过拆图。
- 节点文字尽量 ≤10 个汉字；长标签用 `<br/>` 折行。
- 图固有宽度目标 ≤950px（A4 正文约 8pt）；1000–1200px 需精简，>1200px 优先拆图。
- 先写 `.mmd` 源文件到 `resources/mermaid/<anchorId>.mmd`；不要把 png 嵌进 md。
- 图源首行类型必须与大纲 `subtype` 一致；`.mmd` 用 UTF-8 无 BOM 写入（BOM 会误报 subtype 不匹配）。
- 渲染依赖 node + Chrome/Edge；`bg_render.py` 会自动找 `BG_NODE`/PATH/DSH 运行时 node。若 `render-manifest.json` 出现 `status=fallback`（pillow-fallback），说明 node/浏览器不可用，必须按 env-check 修复后 `bg_render.py --force` 重渲染，**不得把占位图当成品交付**。
- 长图、宽图按“拆图/改 TB/精简节点”处理，不要靠缩字号。

### 3.2 终端图

- 只用于真实工作中通过命令行/脚本/数据库/接口完成的场景；GUI/硬件面板/纸质验收不用。
- 一张图 1–2 条命令 + 关键输出，8–18 行；不是完整日志。
- 单行 ≤42 列（中文按 2 列）；超长自动折行，但总行数超过 22 行要删减或拆图。
- 命令、平台名、参数必须与正文一致；不得出现真实密码、真实内外网 IP、真实人员信息。
- 每末级最多 4 张；同一场景多组命令可拆多张。

## 4. 审查/优化

C 层审图逐项核对：

- 图型是否与内容匹配（流程/时序/排期/状态/架构/数据关系）；
- 图是否放在对应正文之后，图旁有解释文字；
- 终端图是否真实、是否与正文对象一致、是否只是通用命令；
- 是否有明显文字过小、遮挡、空泛、与正文重复；
- A 层已报的 `<8pt`、太宽、太细高等硬问题是否已修；C 层仍保留对明显视觉问题的兜底判断。

优化时优先“改 TB/拆图/精简标签/删减终端行”，不编造数据、不新增未规划图。

## 参考

- 类型语法骨架：`references/mermaid-types.md`
- 终端场景与正反例：`references/terminal-scenarios.md`
- 字号/布局/机检口径：`references/layout-rules.md`

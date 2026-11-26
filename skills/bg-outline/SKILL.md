---
name: bg-outline
description: BidGenie 技术方案大纲编写（F5-1）。当项目处于 outline 阶段，需要按技术评审因素生成大纲草稿、校验结构/覆盖率/字数/图表配额、落盘章节树并完成 T4 迁移进入正文撰写时使用；写大纲、生成目录、规划章节结构、调整章节时触发。
---

# bg-outline（F5-1 大纲）

## 范围

- 输入：extraction 01/06/07/09（只读技术/评分）、package-meta（字数/配额/人员设备/工期）、`reviewLoop` 返工点。
- 产出：`<packageDir>/chapters/_outline/outline.draft.json`（agent 写；**唯一默认路径**）→ 脚本校验/落盘同目录 `outline.json` + `chapters/**` 文件树。
- 排除：商务/报价/合同分包不写；技术文件/技术评分要求投标人提供证书、检测报告、偏离表、承诺函、样品等，按特殊章节处理（加载 `bg-special-chapters`），不写成普通技术方案章节。

## 硬约束

1. 标题 1–6 级；父级 ≤4、末级 ≤5；不越级。
2. 父级=文件夹、末级=`.md`；文件树与 JSON 严格一致（`--check-tree`）。
3. 每个一级标题来自技术评审因素（唯一例外：招标明确要求章节，score=0 并在 writingNotes 记依据）。
4. 每个末级必须有 `criterionRefs`（≥1）、`contentRequirements`（≥1，逐条可验收）、`wordTarget`；特殊章节另有 `handling`（mode/materialType/requirements/userAction），`wordTarget` 100–300（默认 200），`figurePlan=[]`、`insertAnchors=[]`。
5. 字数 Σ=expectedWordCount（±1%）：特殊章节固定 100–300，普通章节分摊剩余字数；图表规划：table/mermaid 不低于 `figureQuota`（下限、多出不返工）；terminal 为**参考值**（0=不生成，>0 可少规划，不强制凑数）；`figureQuota=0` 的类型规划数必须为 0。T4 guard 与 `bg_outline_validate` 同口径。
6. 图表 type=table/mermaid/terminal；mermaid 必须带 `subtype`（flowchart/sequence/gantt/state/er/timeline/mindmap/block/architecture/quadrant/pie/xychart）；每末级 table ≤2、mermaid ≤2、terminal ≤4；terminal 同一场景多组命令可拆多张，但不得为凑数硬塞；`anchorId=fig-<chapterId>-<seq2>`；`caption` 纯题名（不带 ▼/▲；**不要以「数字+量词」开头**，如「3 条路段…」会和 `▼表` 连读成「表3」，改写为「各条路段…」「各类…」；`bg_outline_validate` 会报 `figure_caption_number_like` warning）。特殊章节不规划图。
7. table 只规划为 Markdown 表格（不写 `BG:FIGURE`、不建 resources/tables）；补充表登记 `outline.supplementTables`；不创建 resources/network。
8. 覆盖率 100%（每个 criterionId 至少 1 个末级覆盖）；无商务/报价章节；无模板化无关章节。

## 流程

1. 读输入并确认字数/配额；抽技术评审因素 → `criterionIndex[]`（稳定 criterionId）。可先跑 `bg_outline_apply.py --init-draft` 从 07 技术评分表生成骨架（一级=评分因素、分项=末级、criterionIndex/分值/字数/图表配额预填；分值列 `15` 或 `15分` 均可，自动跳过「合计/小计/总分」，按 ①②③ 拆末级并受总字数 floor 约束合并；**只拆「一、评审内容」的 ①②③，「二、评审标准」的符合度/完整性/合理性/针对性/科学性维度自动跳过**；先用 `--preview` 看“将生成 N 个末级、M 个评审标准维度已跳过”再落盘），再人工/Agent 补充 06/09 事实与重难点；已有草稿时用 `--force-draft` 覆盖。**init-draft 的章节标题和图表题注只是骨架（叶子带 `extensions.titleSource="skeleton"`；图表题注已按标题语义给「参数对照表/施工流程图」等具体后缀，仍可能偏泛），F5-1 必须按本包 06/09 实际改写为通顺短语，不能直接交付半句标题。**
2. 拆章节树（按语义拆子项、分值拆分）；计算 scoreWeight/priority/dependsOn/relatedChapters。
3. **识别特殊章节（加载 `bg-special-chapters`）**：命中证书/检测报告/偏离表/承诺函/样品等，末级写 `handling`、`wordTarget=200`、`figurePlan=[]`；普通技术方案不标记。
4. 字数分配：特殊章节固定 200（100–300），普通章节按 share→floor→取整百分摊剩余，Σ=expectedWordCount；写 `wordTarget` + basis。
5. **图表规划（先加载 `bg-figures`）**：按内容意图选 `type`/`subtype`；特殊章节不规划图；terminal 只在适合的章节规划（可 0 或多张，不按配额平均分布，不硬凑）；每张图写清 `caption/purpose`；登记补充表。
6. 自检：覆盖率/层级/字段/依赖/路径；特殊章节 handling 完整、无图；table/mermaid ≥ 配额，terminal 按参考值（quota=0 时必须为 0）。
7. 写 `chapters/_outline/outline.draft.json`（不写 outline.json、不建 chapters/**）。
8. 校验：`bg_outline_validate.py --strict`；最多修 3 轮；通过后 `bg_outline_apply.py --materialize` → `--check-tree`。
9. T4：`bg_state.py check --to draft` → `transition --to draft`；post-check 后加载 `bg-write`。

## 汇报

章节数/一级/末级、字数分配摘要、评分覆盖、特殊章节数（材料类型/待用户补充项）、图表规划（table/mermaid/terminal 各多少；terminal 参考值与实际规划；quota=0 类型为 0）、T4 结果、需用户确认项、产物绝对路径。

## 参考

- 特殊章节判定/字段/模板：加载 `bg-special-chapters` 技能
- 图表选择/布局/终端场景：加载 `bg-figures` 技能
- 字段/字数算法/命令/异常：`references/rules.md`（按需读）
- 运行时路径见文末 Host 注入块。

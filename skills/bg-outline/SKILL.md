---
name: bg-outline
description: BidGenie 技术方案大纲编写（F5-1）。当项目处于 outline 阶段，需要按技术评审因素生成大纲草稿、校验结构/覆盖率/字数/图表配额、落盘章节树并完成 T4 迁移进入正文撰写时使用；写大纲、生成目录、规划章节结构、调整章节时触发。
---

# bg-outline（F5-1 大纲）

## 范围

- 输入：extraction 01/06/07/09（只读技术/评分）、package-meta（字数/配额/人员设备/工期）、`reviewLoop` 返工点。
- 产出：`chapters/_outline/outline.draft.json`（agent 写）→ 脚本校验/落盘 `outline.json` + `chapters/**` 文件树。
- 排除：商务/报价/资格/合同分包章节。

## 硬约束

1. 标题 1–6 级；父级 ≤4、末级 ≤5；不越级。
2. 父级=文件夹、末级=`.md`；文件树与 JSON 严格一致（`--check-tree`）。
3. 每个一级标题来自技术评审因素（唯一例外：招标明确要求章节，score=0 并在 writingNotes 记依据）。
4. 每个末级必须有 `criterionRefs`（≥1）、`contentRequirements`（≥1，逐条可验收）、`wordTarget`。
5. 字数 Σ=expectedWordCount（±1%）；图表规划数 ≥ `figureQuota`（table/mermaid/terminal，**下限、上不封顶、多出不返工**）。
6. 图表 type=table/mermaid/terminal；每末级 ≤2 个；超额标 `deferred`；`anchorId=fig-<chapterId>-<seq2>`；`caption` 纯题名（不带 ▼/▲）。
7. table 只规划为 Markdown 表格（不写 `BG:FIGURE`、不建 resources/tables）；补充表登记 `outline.supplementTables`；不创建 resources/network。
8. 覆盖率 100%（每个 criterionId 至少 1 个末级覆盖）；无商务/报价章节；无模板化无关章节。

## 流程

1. 读输入并确认字数/配额；抽技术评审因素 → `criterionIndex[]`（稳定 criterionId）。
2. 拆章节树（按语义拆子项、分值拆分）；计算 scoreWeight/priority/dependsOn/relatedChapters。
3. 字数分配（share→raw→floor/clamp→取整百→尾差）；写 `wordTarget` + basis。
4. 图表规划与补充表登记。
5. 自检：覆盖率/层级/字段/配额（≥）/依赖/路径。
6. 写 `outline.draft.json`（不写 outline.json、不建 chapters/**）。
7. 校验：`bg_outline_validate.py --strict`；最多修 3 轮；通过后 `bg_outline_apply.py --materialize` → `--check-tree`。
8. T4：`bg_state.py check --to draft` → `transition --to draft`；post-check 后加载 `bg-write`。

## 汇报

章节数/一级/末级、字数分配摘要、评分覆盖、图表规划（含 ≥配额）、T4 结果、需用户确认项、产物绝对路径。

## 参考

- 字段/字数算法/命令/异常：`references/rules.md`（按需读）
- 运行时路径见文末 Host 注入块。

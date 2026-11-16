---
name: bg-review
description: BidGenie 技术方案审查（F6）。当项目处于 review 阶段，需要跑脚本审查（结构/字数/图表）、子智能体语义审查（评分覆盖/图文契合）、合并问题清单并判定通过或进入优化时使用；审查正文、检查章节覆盖度、判定是否可以定稿时加载。
---

# bg-review（F6 审查）

## 原则

- 审查产出**可计算的问题清单**：以 `issues.summary` 与通过判据为准，Agent 不口头宣布通过。
- 三层顺序固定 **A 脚本 → B 覆盖度语义 → C 图文语义**，按短路规则；每轮重跑，不复用上轮结论。

## 范围

- 只审技术正文与大纲一致性；不改正文/资源（问题交优化）。
- 文风判断加载 `bg-writing-style`，不复制规则。
- 商务/报价内容不审。

## 进入

- `currentStage == review`（T5 已迁移）；outline/chapters/resources/extraction 01/06/07/09/package-meta 就绪；review 脚本就绪。

## 三层与短路

1. **A 脚本**：结构/完整性/标题越级/字数/图表标记与产物（table 只校题注与行列，**table 出现 `BG:FIGURE` 记 blocker/major**；mermaid/terminal 校标记↔figureIndex+png+源文件；产出多于配额不算问题）/题注/引用/JSON/禁用内容/手工修改。
2. **A blocker → 短路 B/C**；无 blocker 继续进行 B、C。
3. **B 覆盖度**：逐章 summary+criterionRefs，先判 pass/weak/missing；weak/missing 才读该章全文核实（引用 ≤200 字）。
4. **C 图文**：脚本机检（png 尺寸/方差、.mmd 可解析、.txt 非空）+ 语义判断；无视觉能力时读源文件并标注未做视觉核验；无图表直接通过。
5. **配额**：figureQuota 是最低要求、上不封顶；多于配额不报问题。

## 合并与报告

- B/C 子智能体只交临时 JSON；`bg_review_issues.py merge --round <k> --layer coverage|graphic` 合并（脚本分配 issueId/去重/校验/快照）。
- 产出：`reviews/issues.json`、`reviews/rounds/round-<k>.issues.json`、`reviews/review-report.md`。

## 判定与迁移

- 通过判据：`blocker==0 ∧ major==0 ∧ (minor==0 ∨ 剩余 minor 全 wontfix 且 userConfirmed)`；覆盖率全 pass/notApplicable；三层本轮已执行。
- 通过 → `bg_review_issues.py verify` 确认 → `bg_state.py check --to finalize` → `transition --to finalize`（T7）。
- 不通过且 open>0 → `check --to optimize` → `transition --to optimize`（T6），加载 `bg-optimize`。
- `reviewLoop.maxRounds` 未收敛 → 停 review，输出未收敛报告（每轮问题/修复/残留 blocker/major），不自动通过。

## 硬约束

1. 不口头通过；通过以 summary + guard 为准。
2. verified 由下一轮审查写；本技能不自证。
3. issues 只由脚本写；Agent 只提交合并输入。
4. 审查阶段不改正文/资源。
5. 不评价商务/报价；文风引用 `bg-writing-style`。
6. 每轮重跑三层；语义判断保留人工抽检，不苛求机械一致。
7. 用户要求关闭 blocker → 不允许。

## 汇报

轮次与三层执行（短路/未做视觉核验）、问题统计与轮次对比（质量回退）、覆盖率摘要、图表对账、下一动作（T6/T7/未收敛）与产物绝对路径。

## 参考

- 命令、issue schema、异常：`references/cli.md`（按需读）
- 运行时路径见文末 Host 注入块。

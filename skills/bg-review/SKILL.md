---
name: bg-review
description: BidGenie 技术方案审查（F6）。当项目处于 review 阶段，需要跑脚本审查（结构/字数/图表）、子智能体语义审查（评分覆盖/图文契合）、合并问题清单并判定通过或进入优化时使用；审查正文、检查章节覆盖度、判定是否可以定稿时加载。
---

# bg-review（F6 审查）

## 原则

- 审查产出**可计算的问题清单**：以 `issues.summary` 与通过判据为准，Agent 不口头宣布通过。
- 三层顺序固定 **A 脚本 → B 覆盖度语义 → C 图文语义**，按短路规则；每轮重跑，不复用上轮结论。
- **跨轮 carry-forward（P1-3）**：`bg_review_script.py --round <k>` 自动把上一轮台账里未 verified 的条目带入本轮，保留原 `issueId`；A 层（script）条目由脚本本轮复检自动写 `verified`（复现则复开 `open`），B/C 语义条目保持 `fixed` 待复审，由复审 `close --status verified --review` 写 `verified`。**同一轮重跑**按「同轮复检」处理：台账整表保留（不整体重写），A 层新检出按条件去重，已修复项复检通过写 `verified`。`issues.json` 是**单一台账**，不得整体重写成空表。

## 范围

- 只审技术正文与大纲一致性；不改正文/资源（问题交优化）。
- 文风判断加载 `bg-writing-style`，不复制规则。
- **特殊章节**：审前读 `handling`，知道该章为什么短；只审四项（要求摘录准确、处理方式合理、请用户补充明确、没有编造），不因字数少/无材料/无图判问题。
- 商务/报价内容不审；技术文件要求的证书/材料按特殊章节只审四项。

## 审查口径（与写作/导出统一）

- **字数**唯一实现：`_lib/text.py` 的 `count_body_words`（CJK 字符 + ASCII 词，剥离 HTML 注释与 Markdown 标题行）；审查、合并、导出、概览必须同口径，禁止各自实现。
- 章节字数：目标 **90%–120%** 为 pass；60%–90% / 120%–140% 记 major；<60% / >140% 记 blocker。
- 全包字数：`expectedWordCount` ±10% 内 pass，±10%–20% 记 major，>±20% 记 blocker。
- summary 的 200–400 是**字符长度**（`len(str)`，含标点），不要与本脚本“字数”混用。
- **未完成章节（R-14）**：`completion.status` 为 pending/writing 的章节单列 `pendingChapters`，不按空文档/字数报问题、全包字数不做偏差判定；写作中途 `--dry-run` 看到 `pendingChapters` 是正常中间态，**不等于审查通过**，写完后再跑。
- **题注（R-13）**：以「数字+量词」开头（如「3 条路段…」）记 minor `caption_format`（会读成「表3」）；真正编号（`表1`/`1、`/`第1章`）仍是 major。

## 进入

- `currentStage == review`（T5 已迁移）；outline/chapters/resources/extraction 01/06/07/09/package-meta 就绪；review 脚本就绪。

## 三层与短路

1. **A 脚本**：结构/完整性/标题越级/字数/图表标记与产物（table 只校题注与行列，**table 出现 `BG:FIGURE` 记 major（`marker_format`，A 层拦截）**；mermaid/terminal 校标记↔figureIndex+png+源文件；产出多于配额不算问题）；图表机检读取 `render-manifest.json` 的 `layout`（字号/宽高/行数），**mermaid 有效字号 <8pt 记 minor、<6.5pt 或固有宽 >2000px 记 major**；**mermaid 渲染降级（`status=fallback`/`engine=pillow-fallback`，通常是 node 不在 PATH）记 blocker `figure_render_fallback`，必须修环境后 `bg_render --force` 重渲染**。/题注/引用/JSON/禁用内容/手工修改。**特殊章节免字数检查、免图表检查**：只要求文件存在、非空、done；报告标注“特殊章节（免字数检查）”。报告里的「跨章数量索引」只供 B 层核对（不判问题）；`verify` 失败会直接返回 `remediation` 可执行命令清单（D5）。
2. **A blocker → 短路 B/C**；无 blocker 继续进行 B、C。短路只看**仍未关闭**（open/fixing）的 blocker，带入且已 verified 的历史 blocker 不短路。
3. **B 覆盖度**：逐章 summary+criterionRefs，先判 pass/weak/missing；weak/missing 才读该章全文核实（引用 ≤200 字）。特殊章节按四项审，不按普通章节覆盖度扣分。
4. **C 图文（保留审图）**：先加载 `bg-figures`；脚本机检（png 尺寸/方差、.mmd 可解析、.txt 非空、manifest 布局指标）+ **Agent 审图**（图型是否选对、图文是否契合、终端是否真实贴合、有无文字过小/遮挡/空泛）；无视觉能力时读源文件并标注未做视觉核验；无图表直接通过。**A 层机检不替代 C 层审图**。特殊章节无图，C 层只审四项中的“没有编造/要求准确/请用户补充明确”。
5. **配额**：table/mermaid 是最低要求、上不封顶；terminal 是参考值（0=不生成，少于参考不报问题）；多于参考不报问题。

## 轮次与台账

- 每轮固定四条产物：`reviews/issues.json`（单一台账）、`reviews/rounds/round-<k>.issues.json`（当轮快照）、`reviews/review-report.md`（含 carry-forward 复检表）、`reviews/rounds/round-<k>.optimization.json`（本轮优化记录）。
- 带入条目新增字段：`carriedFrom`（首次带入前的轮次）、`lastRound`（最近参与轮次）、`recheck{round, previousStatus, result, note}`；`result` 取值：`verified` / `recurred` / `still-open` / `regressed` / `verified-previously` / `pending-semantic` / `carried-open` / `wontfix-carried`。后续 `close` 会补 `recheck.resolvedStatus/resolvedAt`（当前状态以 `status` 为准，报告复检表为带入时点结论）。
- 本轮新问题分配 `RV-<k>-<seq4>`；带入条目**沿用旧 ID**（如 `RV-1-0001`），同条件不再生成新副本。
- 旧版本脚本把台账整体重写后的修复：`bg_review_issues.py carry-forward --round <k>`（从 `rounds/round-<k-1>.issues.json` 补回，幂等，可 `--dry-run`）。

## 合并与报告

- B/C 子智能体只交临时 JSON；`bg_review_issues.py merge --round <k> --layer coverage|graphic` 合并（脚本分配 issueId/去重/校验/快照）。
- **B 层无新问题**：`merge` 传空 `issues: []` 不会写 `outline.reviewState.coverage`；看到 `coverageHint` 后跑 `bg_review_issues.py coverage --round <k> --default pass`（默认只补 unreviewed，保留 weak/missing）。
- 复审 B/C 语义修复项：`bg_review_issues.py close --issue <RV-...> --status verified --review --note "<复检依据>"`；优化方不得自证。
- `status.summary` 是全量历史计数（含 verified）；未关闭数看 `openBySeverity`/`summary.openCount`，不要因 `major:1` 误判未通过。

## 判定与迁移

- 通过判据：`blocker==0 ∧ major==0 ∧ (minor==0 ∨ 剩余 minor 全 wontfix 且 userConfirmed)`；覆盖率全 pass/notApplicable；三层本轮已执行；**major 必须 verified 或 wontfix+userConfirmed**（fixed 不算通过）。特殊章节不因短/无材料/无图产生问题。
- 通过 → `bg_review_issues.py verify` 确认 → `bg_state.py check --to finalize` → `transition --to finalize`（T7）。
- 不通过且 open>0 → `check --to optimize` → `transition --to optimize`（T6），加载 `bg-optimize`。
- `reviewLoop.maxRounds` 未收敛 → 停 review，输出未收敛报告（每轮问题/修复/残留 blocker/major），不自动通过。

## 硬约束

1. 不口头通过；通过以 summary + guard 为准。
2. `verified` 只能由下一轮审查写：A 层条目由 `bg_review_script.py` 复检自动写；B/C 语义条目由复审 `close --status verified --review` 写。优化方不得自证。
3. issues 只由脚本写；Agent 只提交合并输入；不得手工改/清空 `reviews/issues.json`。
4. 审查阶段不改正文/资源。
5. 不评价商务/报价；文风引用 `bg-writing-style`。
6. 每轮重跑三层；语义判断保留人工抽检，不苛求机械一致。
7. 用户要求关闭 blocker → 不允许。

## 汇报

轮次与三层执行（短路/未做视觉核验）、carry-forward 复检结果（上轮 N 项 → verified/复开/待复审）、问题统计与轮次对比（质量回退）、覆盖率摘要、图表对账、下一动作（T6/T7/未收敛）与产物绝对路径。

## 参考

- 特殊章节四项审查口径：加载 `bg-special-chapters` 技能
- 图表类型/布局/终端场景：加载 `bg-figures` 技能
- 命令、issue schema、异常：`references/cli.md`（按需读）
- 运行时路径见文末 Host 注入块。

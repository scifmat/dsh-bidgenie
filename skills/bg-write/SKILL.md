---
name: bg-write
description: BidGenie 技术方案正文撰写（F5-2）。当项目处于 draft 阶段，需要按 4–6 章一批派发子智能体写作、合并章节结果（chapter-result）、生成图表资源并完成 T5 迁移进入审查时使用；写正文、开始撰写章节、继续写方案、批量生成章节内容时触发。
---

# bg-write（F5-2 正文）

## 范围

- 只写技术正文（`chapters/**/*.md`、`resources/mermaid`、`resources/terminal`）；**不创建 `resources/network`、`resources/tables`**。
- 执行大纲：按章写、按批合并、按规则自检；不重新设计结构、不改 outline.json、不改 completion。
- **图表生成前加载 `bg-figures`**：按大纲的 `type/subtype/purpose` 写 `.mmd/.txt`，不重新选图型、不新增未规划的图；terminal 按大纲数量写，可一章多张但不得为凑数硬塞。
- **特殊章节加载 `bg-special-chapters`**：节点带 `handling` 时按“要求 + 处理方式 + 请用户补充”模板写 150–250 字，不写图/表、不代填材料、不承诺已具备。
- 商务/报价内容不进正文；技术文件要求的证书/证明材料按特殊章节处理，不写成普通方案承诺。
- 文风规则加载 `bg-writing-style`，不复制规则。

## 批次（4–6 章一批）

1. 建图：节点=leaf，边=`dependsOn`；拓扑分层；层内按 priority→scoreWeight→seq 排序。
2. 每批 **4–6 个末级章节（约 6000–9000 字）**，批内不跨硬依赖；**一个批次交给一个子智能体，不要每章一个**。
3. **优先用生成器**：`bg_write_brief.py --batch <k>` 输出 `chapters/_batches/batch-<k>.md/.json`（含本批章节、criterionRefs、contentRequirements、figurePlan、projectFacts、dependencySummaries、maxBodyHeadingLevel），直接把 md 交给子智能体，不再手工拼 prompt；`--all` 可一次生成全部批次，`--no-write` 只预览。批次按大纲 `seq` 连续切分，生成器同时维护 `_batch-state.json` 台账（含实际 batchSize）。
4. 批间并发受 `maxParallelWriters`/DSH 并发上限限制；每批结束调 `bg_chapter_merge.py` 合并本批结果，再下一批。
5. 中断恢复：先用 `bg_state.py doctor` 看 `staleResults`/待做章节；读 `_batches/_batch-state.json` 与 `pending`；**优先 `send_message` 唤醒原批次子代理**（保留已读原文上下文，省重读成本），原会话不可用再重派；`writing` 超 30 分钟无更新视为失败，重置 pending。

## 子智能体交付（逐章）

1. **正文**：写 `filePath`；首行标题 `#`×level；正文标题只允许 level+1 及以内；表格用真实 Markdown，题注在表上方 `▼表<表名>`，**table 不写 `BG:FIGURE` 标记**；图/终端写 `<!-- BG:FIGURE:<anchorId> -->` + 下一行 `▲图<caption>`，不嵌图片。**带 `handling` 的特殊章节按模板写，不写图/表/标记。**
2. **章节结果**：每章写 `chapters/_results/<chapterId>.chapter-result.json`（status/wordCount/summary/criterionRefs/figures/tables/issues）；特殊章节 `figures=[]`、`tables=[]`。
3. **图表源码**：mermaid `.mmd`、terminal `.txt` 写到 `resources/<type>/<anchorId>.*`；**不要运行 `bg_render.py`**——渲染统一由主 Agent 在整批合并后执行，避免并发写 `render-manifest.json`、避免包内 png 与临时目录残留。子智能体确需本地预览只能生成临时文件，交付前必须删除，不得留下 png/临时目录。特殊章节不产资源。
4. **沙箱**：写入因 workspace-write 权限失败（EPERM）→ 停止并提示用户切换 DSH 文件策略为 **danger-full-access** 或授权工作区后重试；不静默降级、不伪造 png。
5. 只写本批各章文件；不改 outline.json、不写其他批次、不删资源、不渲染 png。

## 口径（字数 / 表格 / summary）

- **字数 = 审查口径**：汉字逐个计数 + ASCII 连续词（如 `2026-11-21`、`PLC_01` 各记 1 个词），标点与空白不计；统计时去掉 Markdown 标题行与 `<!-- ... -->` 注释。`chapter-result.wordCount` 必须用这个口径；合并脚本 `bg_chapter_merge.py` 会再按章节 md 复核，并以该口径写 `outline.stats.writtenWordTotal`（导出/概览同口径）。
- **字数目标区间是 90%–120%**（低于 90% 需扩写，高于 120% 需精简，审查 A 层会拦）。**全包合计以 ±10% 为验收口径**：单章建议按 95%–110% 写，不要系统性顶到 118%–120%（16 章都顶上限会让全包 +16%，多花一轮优化）。**特殊章节例外**：按 handling 模板写 150–250 字即可，审查免字数检查，不为凑 200 字灌水。
- **合并后先看字数诊断**：`bg_chapter_merge.py` 输出 `wordCountDiagnostics`（`expectedTotal/actualTotal/deviationPct/chaptersOverTarget/chaptersUnderTarget/specialSkipped`）；全包超 ±10% 或超目标章节较多时，先按清单定点精简/扩写再进 T5；特殊章节在 `specialSkipped`，不参与逐章超欠目标。
- **summary 的 200–400 是字符长度 `len(str)`（含标点），不是字数**；两者不要混用。
- **表格注册**：`chapter-result.tables[]` 登记本章所有 Markdown 表；属于 figurePlan 的“计划表”必须带 `anchorId`（或与 figurePlan 的 `caption` 完全一致），合并脚本只在 `outline.supplementTables` 登记真正的补充表，不会把计划表重复登记成 `sup-*`。计划表不写 `BG:FIGURE`，误加会被 A 层审查拦截、导出也会跳过该标记并给 warning。

## 合并与 T5

- 每批结束调 `bg_chapter_merge.py`；检查各章 completion：done 继续，blocked 重试该章 ≤2 次，仍 blocked 交主 Agent。
- 合并脚本会重算 `outline.stats`：`writtenWordTotal` 按章节 md 的审查口径复算（同时记 `reportedWordTotal` 供对照）、清理 `supplementTables` 里的计划表重复登记；`wordCountWarnings` 提示“上报字数与实测偏差 >20%”的章节，`wordCountDiagnostics` 给出全包偏差与逐章超/欠目标清单；`staleResults` 列出“md 已改/字数不符但 chapter-result 未同步”的章节（优化阶段尤其要查）。
- 全包偏差 >±10% 时先按诊断清单定点调整，再进入 T5；避免“全包超额”被审查 A 层记为 major 后才返工。
- 全部 leaf 为 done/skipped、无 blocked → `bg_chapter_merge.py --verify-all` → `bg_state.py check --to review` → `transition --to review`（T5）。
- post-check 断言 `currentStage=="review"`；不成立报 `E_STATE_CORRUPT`。

## 硬约束

1. 只写技术正文；商务/资格/报价不进正文。
2. 不手写 outline.json/completion；章节结果只经合并脚本落盘。
3. 不编造事实；不承诺 package-meta 之外的人/设备/日期/品牌。
4. 不自引用、不写敏感信息。
5. 层级不越级；题注表上/图下、不带编号；table 不写 BG:FIGURE，mermaid/terminal 才写标记。
6. 图表按大纲规划生成（mermaid 按 `subtype`；terminal 可一章多张）；table/mermaid 配额是下限，terminal 是参考值，不为凑数新增图；**渲染统一由主 Agent 执行**，子智能体只交 `.mmd/.txt` 源文件并清理临时预览产物。特殊章节不产图。
7. 状态只由脚本写；T5 前置 `--verify-all` + check 通过。
8. 单批 60 分钟（单章约 20 分钟）上限；超出分批/次日继续，状态可恢复。

## 汇报

批次进度、完成/blocked 章节、特殊章节完成情况、字数与图表现状、T5 结果、未完成项与建议。

## 参考

- 特殊章节模板/禁用表述：加载 `bg-special-chapters` 技能
- chapter-result schema/批次示例/命令/异常：`references/schema.md`（按需读）
- 运行时路径见文末 Host 注入块。

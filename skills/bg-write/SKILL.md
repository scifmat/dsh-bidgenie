---
name: bg-write
description: BidGenie 技术方案正文撰写（F5-2）。当项目处于 draft 阶段，需要按 4–6 章一批派发子智能体写作、合并章节结果（chapter-result）、生成图表资源并完成 T5 迁移进入审查时使用；写正文、开始撰写章节、继续写方案、批量生成章节内容时触发。
---

# bg-write（F5-2 正文）

## 范围

- 只写技术正文（`chapters/**/*.md`、`resources/mermaid`、`resources/terminal`）；**不创建 `resources/network`、`resources/tables`**。
- 执行大纲：按章写、按批合并、按规则自检；不重新设计结构、不改 outline.json、不改 completion。
- 商务/资格/报价内容不进正文；文风规则加载 `bg-writing-style`。

## 批次（4–6 章一批）

1. 建图：节点=leaf，边=`dependsOn`；拓扑分层；层内按 priority→scoreWeight→seq 排序。
2. 每批 **4–6 个末级章节（约 6000–9000 字）**，批内不跨硬依赖；**一个批次交给一个子智能体，不要每章一个**。
3. 批间并发受 `maxParallelWriters`/DSH 并发上限限制；每批结束调 `bg_chapter_merge.py` 合并本批结果，再下一批。
4. 中断恢复：读 `pending`/`writing`；`writing` 超 30 分钟无更新视为失败，重置 pending。

## 子智能体交付（逐章）

1. **正文**：写 `filePath`；首行标题 `#`×level；正文标题只允许 level+1 及以内；表格用真实 Markdown，题注在表上方 `▼表<题名>`，**table 不写 `BG:FIGURE` 标记**；图/终端写 `<!-- BG:FIGURE:<anchorId> -->` + 下一行 `▲图<caption>`，不嵌图片。
2. **章节结果**：每章写 `chapters/_results/<chapterId>.chapter-result.json`（status/wordCount/summary/criterionRefs/figures/tables/issues）。
3. **图表**：mermaid `.mmd`、terminal `.txt` → `bg_render.py` 渲染 png；配额是最低值、上不封顶，多出允许不返工；失败保留源文件并记 issues。
4. **沙箱**：渲染/写入因 workspace-write 权限失败（EPERM）→ 停止并提示用户切换 DSH 文件策略为 **danger-full-access** 或授权工作区后重试；不静默降级、不伪造 png。
5. 只写本批各章文件；不改 outline.json、不写其他批次、不删资源。

## 合并与 T5

- 每批结束调 `bg_chapter_merge.py`；检查各章 completion：done 继续，blocked 重试该章 ≤2 次，仍 blocked 交主 Agent。
- 全部 leaf 为 done/skipped、无 blocked → `bg_chapter_merge.py --verify-all` → `bg_state.py check --to review` → `transition --to review`（T5）。
- post-check 断言 `currentStage=="review"`；不成立报 `E_STATE_CORRUPT`。

## 硬约束

1. 只写技术正文；商务/资格/报价不进正文。
2. 不手写 outline.json/completion；章节结果只经合并脚本落盘。
3. 不编造事实；不承诺 package-meta 之外的人/设备/日期/品牌。
4. 不自引用、不写敏感信息。
5. 层级不越级；题注表上/图下、不带编号；table 不写 BG:FIGURE，mermaid/terminal 才写标记。
6. 图表配额是下限、可多不可少；多出不返工；渲染失败保留源并记录。
7. 状态只由脚本写；T5 前置 `--verify-all` + check 通过。
8. 单批 60 分钟（单章约 20 分钟）上限；超出分批/次日继续，状态可恢复。

## 汇报

批次进度、完成/blocked 章节、字数与图表现状、T5 结果、未完成项与建议。

## 参考

- chapter-result schema/批次示例/命令/异常：`references/schema.md`（按需读）
- 运行时路径见文末 Host 注入块。

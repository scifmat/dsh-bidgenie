# 正文写作 schema 与命令参考（按需读）

## chapter-result.json（每章一个）

```json
{
  "chapterId": "1-3-1",
  "filePath": "chapters/1-需求分析及理解/1-3-运维重点分析/1-3-1-前端设备运维重点.md",
  "status": "done",
  "wordCount": 1800,
  "summary": "响应『前端设备运维』评审点；关键承诺/数据；与 1-3-2 衔接。",
  "criterionRefs": ["C-03"],
  "figures": [
    { "anchorId": "fig-1-3-1-01", "type": "mermaid", "caption": "前端设备运维流程", "status": "produced",
      "resourcePath": "resources/mermaid/fig-1-3-1-01.png", "sourcePath": "resources/mermaid/fig-1-3-1-01.mmd" }
  ],
  "tables": [ { "caption": "前端设备巡检项", "rows": 6 } ],
  "issues": [],
  "questions": [],
  "blockReason": ""
}
```

- `status`：done / partial / blocked；blocked 带 `blockReason` 与 `questions[]`（回传主 Agent，不直接问用户）。
- `wordCount`：**审查口径字数**（汉字逐个 + ASCII 连续词，标点/空白不计，去掉标题行与 HTML 注释）；合并脚本会按章节 md 复核并回写 `outline.stats.writtenWordTotal`。目标区间为 `wordTarget` 的 90%–120%。
- `summary`：200–400 **字符长度（`len(str)`，含标点）**，不是字数口径；两者不要混用。
- 补充表：`tables[]` 登记本章所有 Markdown 表。属于 figurePlan 的“计划表”须带 `anchorId`（或 `caption` 与 figurePlan 完全一致，可加 `"plannedFigure": true`）；合并脚本只把**真正补充表**写入 `outline.supplementTables`（按 `anchorId`/题注去重），计划表不会重复登记成 `sup-*`。
- 图/终端只写 `<!-- BG:FIGURE:<anchorId> -->` + 下一行 `▲图<caption>`；**table 不写 `BG:FIGURE` 标记**（按 `▼表<表名>` 题注定位）。

## 批次派发示例

```json
{
  "batch": 2,
  "chapters": [
    { "chapterId": "1-3-1", "title": "…", "level": 3, "filePath": "…", "criterionRefs": ["C-03"],
      "contentRequirements": ["…"], "wordTarget": 1800,
      "figurePlan": [ { "anchorId": "fig-1-3-1-01", "type": "mermaid", "caption": "…" } ],
      "context": { "dependencySummaries": {}, "projectFacts": "…", "userPlans": "…" } }
  ],
  "constraints": { "noSelfReference": true, "noSensitive": true, "noBusinessMaterial": true }
}
```

每批 4–6 章（约 6000–9000 字）；加载 `bg-writing-style` 后写作。

## 合并与渲染命令

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_chapter_merge.py" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_chapter_merge.py" --verify-all --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_render.py" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to review --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to review --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

渲染前源文件：mermaid `.mmd`、terminal `.txt`（写在 `resources/<type>/<anchorId>.*`）。

## 异常与时限

| 场景 | 处理 |
|---|---|
| 子智能体失败/超时 | 单批 60 分钟（单章约 20 分钟）终止，失败章 blocked；重试 ≤2 次 |
| 重试仍 blocked | 主 Agent 补上下文/问用户/确认 skipped（写 skipReason）；T5 前清零 |
| 依赖环/缺失 | 停止正文，回大纲修复 |
| 字数 <90% | 先重派并给扩写要点；仍不足如实记录留审查 |
| 标题越级 | 合并脚本报错 → 重写；连续 2 次记 blocker |
| 图表渲染失败 | 保留源文件、`figures[].status="failed"`、记 issues |
| 沙箱权限失败 | 提示切换 danger-full-access/授权工作区；不静默降级、不伪造 png |
| 并发写冲突 | 子智能体只写本批文件；合并脚本串行落盘 |
| 用户改字数/配额 | 走 R3 回 outline 重算 |
| 脚本未就绪 | 原样报错；不伪造输出 |

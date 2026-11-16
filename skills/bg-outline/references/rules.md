# 大纲字段/算法/异常参考（按需读）

## 字段与算法

- **章节树**：`id`（`1`/`1-1`/`1-1-1` 稳定）、`title`、`level`、`dirPath`、`filePath`、`seq`、`scoreWeight`、`priority`（P0/P1/P2）、`dependsOn`（只指向 seq 更小、不跨包无环）、`relatedChapters`。
- **H5 写作任务书**：`criterionRefs[]`（≥1）、`contentRequirements[]`（≥1、逐条可验收）、`wordTarget`、`figurePlan[]`。
- **字数分配**：`share=scoreWeight/ΣscoreWeight`；`raw=W×share`；floor=800，单章 clamp `[800, min(8000,W/2)]`；取整百，尾差给最高分章节；ΣwordTarget=W（±1%）；写 `wordTargetBasis{formula,share,weight,adjusted}`。
- **图表**：type=table/mermaid/terminal；每末级 ≤2 个；超额 `deferred`；`anchorId=fig-<chapterId>-<seq2>`；`caption` 纯题名；`status="planned"`；规划数 ≥ `figureQuota`（下限、多出允许）。
- **表格**：table 类型只写 Markdown 表格（正文 `▼表` 题注），不写 `BG:FIGURE`、不建 `resources/tables`；补充表登记 `outline.supplementTables`。
- 顶层：`schemaVersion/packageId/title/generatedAt/totalWordTarget/criterionIndex/chapters/figureIndex/stats/extensions`。

## 命令

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_validate.py" --strict --check-tree [--input "<outline.json>"] --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --materialize --workspace "<ws>" --project "<projectDir>" --package "package-<N>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --check-tree --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to draft --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to draft --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

## 异常

| 场景 | 处理 |
|---|---|
| 评分标准子项被合并 | 按语义拆末级，分值均分 |
| 层级超限 | 压缩/上提，避免单子父级 |
| 字数/图表不达标 | 修正后重校验；少规划不得放过；多出不返工 |
| 图表无法达到下限 | 回写 package-meta.figureQuota（用户确认）后重算 |
| 依赖环/跨包依赖 | 停止，修正 `dependsOn` |
| 校验 warning 多 | `--strict` 按失败处理；逐项修复 |
| 商务/资格章节混入 | 移除，只保留技术评审因素 |
| 文件树与 JSON 不一致 | `--materialize` 重建后 `--check-tree` |

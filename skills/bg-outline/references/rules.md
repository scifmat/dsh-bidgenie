# 大纲字段/算法/异常参考（按需读）

## 产出路径（M5.1 起唯一默认）

`<packageDir>/chapters/_outline/outline.draft.json`（agent 写）→ 同目录 `outline.json`（脚本写）。

- 旧项目把 `outline.draft.json` / `outline.json` 放在包根也能继续：`bg_outline_apply.py --materialize`
  会自动迁移到 `chapters/_outline/`（先备份、同盘移动原子、可重复执行），并在输出 `warnings` 里写明迁移与备份；
  不再报 `E_NOT_FOUND`。需要单独迁移时仍可跑 `bg_outline_apply.py --migrate-storage`。
- `--input` 显式指向包根旧文件时不自动搬运（避免搬走用户指定文件），仍提示默认路径。

## 字段与算法

- **章节树**：`id`（`1`/`1-1`/`1-1-1` 稳定）、`title`、`level`、`dirPath`、`filePath`、`seq`、`scoreWeight`、`priority`（P0/P1/P2）、`dependsOn`（只指向 seq 更小、不跨包无环）、`relatedChapters`。
- **H5 写作任务书**：`criterionRefs[]`（≥1）、`contentRequirements[]`（≥1、逐条可验收）、`wordTarget`、`figurePlan[]`。
- **特殊章节**：末级额外带 `handling{mode,materialType,requirements[],userAction}`；`mode="user_material"`；`materialType` 取 certificate/performance/personnel/test_report/product_literature/deviation_table/commitment/sample；`requirements[]` 每条 `{text,sourceRef}`；`wordTarget` 100–300（默认 200）；`figurePlan=[]`、`insertAnchors=[]`；`contentRequirements` 写“特殊处理：要求 + 处理方式 + 请用户补充”。普通章节不得带 handling，特殊章节不得规划图。
- **字数分配**：普通章节 `share=scoreWeight/ΣscoreWeight`；`raw=W×share`；floor=800，单章 clamp `[800, min(8000,W/2)]`；取整百，尾差给最高分章节；ΣwordTarget=W（±1%）；写 `wordTargetBasis{formula,share,weight,adjusted}`。特殊章节固定 200（100–300，脚本按 100 取整），普通章节分摊剩余字数，全包 Σ 不变。
- **图表**：type=table/mermaid/terminal；mermaid 必须带 `subtype`（flowchart/sequence/gantt/state/er/timeline/mindmap/block/architecture/quadrant/pie/xychart）；每末级 table ≤2、mermaid ≤2、terminal ≤4；`anchorId=fig-<chapterId>-<seq2>`；`caption` 纯题名；`status="planned"`；table/mermaid 规划数 ≥ `figureQuota`（下限、多出允许）；terminal 为**参考值**（0=不生成，>0 可少规划，不强制凑数，适合章节可多张）。
- **T4 口径**：`bg_state.py check/transition --to draft` 的 `figure_quota` guard：table/mermaid `planned >= quota`；terminal 仅 `quota=0 ⇒ planned=0`；失败 detail 给出 `deficits`/`overDisabledQuota`/`terminalReference`。
- **表格**：table 类型只写 Markdown 表格（正文 `▼表` 题注），不写 `BG:FIGURE`、不建 `resources/tables`；补充表登记 `outline.supplementTables`。
- 顶层：`schemaVersion/packageId/title/generatedAt/totalWordTarget/criterionIndex/chapters/figureIndex/stats/extensions`。

## 命令

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_validate.py" --strict --check-tree [--input "<outline.json>"] --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --init-draft [--force-draft] [--with-terminal] --workspace "<ws>" --project "<projectDir>" --package "package-<N>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --materialize --workspace "<ws>" --project "<projectDir>" --package "package-<N>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --check-tree --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --migrate-storage --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to draft --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to draft --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

`--materialize` 输出关注：`applied/backup/outlinePath/stats`、`autoMigrated`+`warnings`（旧布局自动迁移）、`mode`。

## 异常

| 场景 | 处理 |
|---|---|
| 评分标准子项被合并 | 按语义拆末级，分值均分 |
| 层级超限 | 压缩/上提，避免单子父级 |
| 字数/图表不达标 | 修正后重校验；table/mermaid 少规划不得放过；terminal 少于参考值仅提示，不为凑数硬塞 |
| table/mermaid 无法达到下限 | 回写 package-meta.figureQuota（用户确认）后重算；terminal 参考值可直接按适合章节规划 |
| 依赖环/跨包依赖 | 停止，修正 `dependsOn` |
| 校验 warning 多 | `--strict` 按失败处理；逐项修复 |
| 商务/资格章节混入 | 移除；技术文件/技术评分要求的证明材料改标特殊章节（加载 `bg-special-chapters`） |
| 特殊章节 wordTarget 不在 100–300 | 改为 200；特殊章节无图（figurePlan/insertAnchors 为空） |
| 特殊章节被规划了图 | 删除 figurePlan/insertAnchors；材料清单由 handling 生成 |
| 文件树与 JSON 不一致 | `--materialize` 重建后 `--check-tree` |
| 大纲写在包根旧位置 | 直接 `--materialize` 自动迁移（看 `warnings`）；或 `--migrate-storage` |

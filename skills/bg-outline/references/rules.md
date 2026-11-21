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
- **字数分配**：`share=scoreWeight/ΣscoreWeight`；`raw=W×share`；floor=800，单章 clamp `[800, min(8000,W/2)]`；取整百，尾差给最高分章节；ΣwordTarget=W（±1%）；写 `wordTargetBasis{formula,share,weight,adjusted}`。
- **图表**：type=table/mermaid/terminal；每末级 ≤2 个；超额 `deferred`；`anchorId=fig-<chapterId>-<seq2>`；`caption` 纯题名；`status="planned"`；规划数 ≥ `figureQuota`（下限、多出允许；超额不返工）；`figureQuota=0` 的类型规划数必须为 0（network 已移除）。
- **T4 口径**：`bg_state.py check/transition --to draft` 的 `figure_quota` guard = `planned >= quota`（与 `bg_outline_validate` 一致；quota=0 要求 planned=0），失败 detail 给出 `deficits`/`overDisabledQuota`。
- **表格**：table 类型只写 Markdown 表格（正文 `▼表` 题注），不写 `BG:FIGURE`、不建 `resources/tables`；补充表登记 `outline.supplementTables`。
- 顶层：`schemaVersion/packageId/title/generatedAt/totalWordTarget/criterionIndex/chapters/figureIndex/stats/extensions`。

## 命令

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_validate.py" --strict --check-tree [--input "<outline.json>"] --workspace "<ws>" --project "<projectDir>" --json
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
| 字数/图表不达标 | 修正后重校验；少规划不得放过；多出不返工 |
| 图表无法达到下限 | 回写 package-meta.figureQuota（用户确认）后重算 |
| 依赖环/跨包依赖 | 停止，修正 `dependsOn` |
| 校验 warning 多 | `--strict` 按失败处理；逐项修复 |
| 商务/资格章节混入 | 移除，只保留技术评审因素 |
| 文件树与 JSON 不一致 | `--materialize` 重建后 `--check-tree` |
| 大纲写在包根旧位置 | 直接 `--materialize` 自动迁移（看 `warnings`）；或 `--migrate-storage` |

# F7 定稿/导出命令参考（按需读）

## 定稿（T9）

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_finalize.py" --lock --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_finalize.py" --verify --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to export --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to export --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

解锁（R4）：`bg_finalize.py --unlock --force --reason "<原因>"`；锁定后禁止写正文。

## 导出与自检（顺序固定）

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_docx.py" --mode tech --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_verify.py" --mode tech --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --report --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_overview.py" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_verify.py" --mode overview --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --report --json
```

- overview 基于最新 tech 导出报告；任一自检失败停止后续（不导出 overview、不进 T10）。
- overview 自动统计特殊章节，并在“数据缺口”下生成“需用户提供的材料（特殊章节）”表；技术 docx 按大纲包含短章节，无需额外处理。
- keyinfo 按需：`bg_export_docx.py --mode keyinfo ...`；自检 `bg_export_verify.py --mode keyinfo [--docx <path>] --report`（缺省读导出报告里的 docxPath/对应报告）。
- verify 失败保留产物与报告，修复后重试一次。
- tech 导出默认插入 Word TOC 域（打开后按 F9/“更新域”生成页码），并在**目录后自动插入分页符**，正文从新页开始；`bg_export_verify` 返回 `tocFieldPresent=true`、`checks.pageBreakAfterToc=true`。招标文件明确不要目录时用 `bg_export_docx.py --mode tech --no-toc`。若用户拿到 docx 后未更新域，目录页显示占位提示，按 F9 即可。
- 多包 run：某包完成 T10 后**不要** `--finalize`；每包导出后只做普通记录（不带 `--start/--finalize`），等 `writingPackages` 全部完成、最后一个包 T10 后再 `--finalize`（中间包 finalize 会把未完成 run 提前归档）。

## run-manifest 生命周期

```text
# T1 后（开跑）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_run_manifest.py" --start --workspace "<ws>" --project "<projectDir>" --json
# 每包导出后（记录本次 docx 路径与 SHA256；脚本把 exports 写回 start 记录）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_run_manifest.py" --workspace "<ws>" --project "<projectDir>" --json
# T10 后（收尾结论）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_run_manifest.py" --finalize --workspace "<ws>" --project "<projectDir>" --json
```

- 归档：`--finalize` 按返回 `runId` 写 `<workspace>/dsh/run-manifests/<runId>.json` + `.md`；`--start` 写 `<runId>.start.json`；验收报告统一引用 start 的 runId。
- 中间记录（每包导出后不带 `--start/--finalize`）**复用同一 runId**：读最近的 `<runId>.start.json`，产物写成 `<runId>.snapshot-<ts>.json/.md`，不会新增第二个 runId；没有 start 记录时降级为新 runId 并返回 warning。大输出加 `--brief`：只回 runId/路径/摘要，不再打印 `plugin.files`/事件文件明细。
- `dsh/run-manifests/_latest.json` 为实体指针（`runId/kind/updatedAt/...`），随 start/记录/finalize 原子更新；历史遗留的 `_latest.json.lock`（无实体）会被清理。
- `--finalize` 返回 `versionDrift`/`envSnapshotAfterStart`/`envRefreshedAfterStart`（start 内）/`exitCode`；验收报告引用 runId/rootHash 与这些结论。
- 运行中刷新过 env-check → start 记录带 `envRefreshedAfterStart`，finalize 会判 envHash 漂移/环境快照晚于 start，不视为零漂移。
- **run 中途升级过插件/脚本**（start 与 finalize 的 pluginHash/scriptsHash 不同）→ `versionDrift=true`、finalize 退出码 4 属预期；验收以“start 后未升级”的干净运行为准，不要把它当成流程缺陷。

## T10 与 cleanup

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to completed --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to completed --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_finalize.py" --cleanup --keep 5 --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_outline_apply.py" --cleanup --keep 5 --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

- cleanup 覆盖 `export/.tmp`、旧 docx、`_frozen` 归档（保留最新 N）；outline cleanup 覆盖 `.bak`/`.lock`。
- 换样式重导不改阶段（completed 可重导），产物按时间戳，由 cleanup 管理份数。

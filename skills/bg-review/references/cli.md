# F6 审查命令与 issue schema（按需读）

## 三轮命令

```text
# A 层脚本审查（layer 默认 all）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_script.py" --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# B/C 层问题合并（layer coverage|graphic）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" merge --round <k> --input "<issues-bc.json>" --layer coverage --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 台账摘要 / 4.6 判据 / 关闭问题
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" status --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" verify --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" close --issue "RV-<round>-<seq4>" --status fixed --note "<说明>" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

- 顶层 `--check` 等价 `verify`；没有 `--summary`（用 `status` 子命令）。
- `close --status`：fixing / fixed / verified / wontfix；`--review` 由复审方写 verified；`--user-confirmed` 用于 wontfix。

## issues 结构（7.5）

顶层：`schemaVersion/packageId/round/generatedAt/layersRun/summary{blocker,major,minor,total,openCount,fixedCount,wontfixCount}/issues[]`。
issue：`issueId(RV-<round>-<seq4>)/layer(script|coverage|graphic)/type/severity/chapterId/filePath/anchorId/evidence/suggestion/status/userConfirmed/createdAt/updatedAt/resolvedAt/resolutionNote`。
状态：`open → fixing → fixed → verified`；允许 `open → wontfix`（需 userConfirmed）；verified 只能由下一轮审查/复审写入。

## 分层与短路

- A 脚本：结构、完整性、标题越级、字数、图表（table 只校题注/行列；table 出现 BG:FIGURE 记 blocker/major；mermaid/terminal 校标记↔figureIndex+png+源文件）、题注、引用、JSON、禁用内容、手工修改检测。
- A blocker → 短路 B/C；B 覆盖度语义（逐章 summary+criterionRefs，pass/weak/missing）；C 图文语义（脚本机检 + 语义判断）。
- C 无资源/无图表 → 直接通过。

## 通过判据（T7）

`blocker==0 ∧ major==0 ∧ (minor==0 ∨ 剩余 minor 全 wontfix 且 userConfirmed)`；覆盖率全部 pass/notApplicable；三层本轮已执行。到达 `reviewLoop.maxRounds` 未通过 → 停 review 输出未收敛报告，不自动通过。

## 异常

| 场景 | 处理 |
|---|---|
| 脚本误报 | wontfix（需 userConfirmed）或修脚本（版本化） |
| 审查子智能体超时 | 单章 ≤10 分钟；记 review_timeout（minor），不阻塞其他章 |
| issues 损坏 | 从 `rounds/round-<k>.issues.json` 恢复；否则重跑该轮 |
| 优化引入新问题 | 下一轮必须重覆盖；问题数上升报「质量回退」 |
| 用户要求关 blocker | 不允许；只能修复/按规则改级别 |
| 循环达上限 | 未收敛报告并停 review |
| 脚本未就绪/失败 | 原样报 error/message/hint；不手写 issues |

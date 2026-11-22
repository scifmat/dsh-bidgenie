# F6 审查命令与 issue schema（按需读）

## 三轮命令

```text
# A 层脚本审查（layer 默认 all；默认自动 carry-forward 上轮未 verified 条目）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_script.py" --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# B/C 层问题合并（layer coverage|graphic）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" merge --round <k> --input "<issues-bc.json>" --layer coverage --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 台账摘要 / 4.6 判据 / 关闭问题
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" status --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" verify --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" close --issue "RV-<round>-<seq4>" --status fixed --note "<说明>" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 复审写 verified（B/C 语义修复项；A 层条目不手工写，由下一轮脚本复检自动写）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" close --issue "RV-<round>-<seq4>" --status verified --review --note "<复检依据>" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 台账修复：上轮 fixed/未 verified 条目丢失时从 round 快照补回（幂等，可 --dry-run）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" carry-forward --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

- 顶层 `--check` 等价 `verify`；没有 `--summary`（用 `status` 子命令）。
- `bg_review_script.py` 的 `--no-carry-forward`：不读上一轮台账（纯本轮重生成，仅修复/重跑特殊场景使用）。
- `close --status`：fixing / fixed / verified / wontfix；`--review` 由复审方写 verified；`--user-confirmed` 用于 wontfix。

## 轮次台账与 carry-forward（P1-3）

- `reviews/issues.json` 是**单一台账**：带入条目保留原 `issueId`，不因新一轮重写而丢失。
- `reviews/rounds/round-<k>.issues.json` 是每轮快照；`reviews/rounds/round-<k>.optimization.json` 是本轮优化记录（close fixed/verified/wontfix 自动维护）。
- 新一轮开始时，`bg_review_script.py --round <k>` 读取上轮台账（`issues.json` 轮次 < k；同轮重跑读已 carry 条目；缺失时回退 `rounds/round-<k-1>.issues.json`），并按本轮 A 层复检结果处理：

| 上轮状态 | 层次 | 本轮复检 | 结果 |
|---|---|---|---|
| fixed/fixing | script | 未再检出 | `verified`（recheck.result=verified） |
| fixed/fixing | script | 再次检出 | `open`（recurred，复开等优化再修） |
| open | script | 再次检出 / 未检出 | 保持 open（still-open）/ 自动 `verified` |
| verified | script | 再次检出 | `open`（regressed，回归） |
| fixed | coverage/graphic（语义） | 脚本无法复检 | 保持 `fixed`（pending-semantic），复审 `close --status verified --review` |
| open/fixing | coverage/graphic | — | 保持原状态（carried-open） |
| wontfix（userConfirmed） | 任意 | 再次检出也不复开 | 保留 `wontfix`（wontfix-carried） |

- 本轮新检出与带入条目**同条件**（layer/type/chapterId/anchorId）时不再新增副本，`carryForward.suppressedCount` 记录去重数。
- **同一轮重跑**（如 review 轮内重跑脚本，或在 optimize 阶段复跑）：台账整表作为「同轮复检」数据源保留（`sameRound=true`，source 标「同轮复检」），不整体重写；A 层新检出按条件去重，已修复项复检通过写 `verified`，复现则复开 `open`。`carriedFrom` 取该 issue 的检出轮次（`RV-<round>-…`）。
- `carry-forward --round <k>` 用于修复：旧版本脚本把台账整体重写后，从 `rounds/round-<k-1>.issues.json` 补回丢失条目（`--dry-run` 只计算）；第二次运行 `changed=false`（幂等）。

## issues 结构（7.5）

顶层：`schemaVersion/packageId/round/generatedAt/layersRun/layerStatus/pendingLayers/summary{blocker,major,minor,total,openCount,fixedCount,verifiedCount,wontfixCount}/issues[]`；发生 carry-forward 时追加 `carryForward{round,previousRound,sameRound,carriedCount,detectedCount,suppressedCount,verifiedCount,reopenedCount,pendingSemanticCount,carriedOpenCount,wontfixKeptCount,keptVerifiedCount,byRecheck,source,at,verifiedIds,reopenedIds,pendingSemanticIds,...}`。
issue：`issueId(RV-<round>-<seq4>)/layer(script|coverage|graphic)/type/severity/chapterId/filePath/anchorId/evidence/suggestion/status/userConfirmed/createdAt/updatedAt/resolvedAt/resolutionNote`；
带入条目额外：`carriedFrom/lastRound/recheck{round,at,previousStatus,result,note,resolvedStatus?,resolvedAt?}`——`result` 是带入时点复检结论，`resolvedStatus` 是后续 `close` 写的复核结论（当前状态以 `status` 为准）。
状态：`open → fixing → fixed → verified`；允许 `open → wontfix`（需 userConfirmed）；verified 只能由下一轮审查/复审写入（A 层脚本复检自动写 / B/C 复审 `close --review` 写）。

## 字数口径（P2-10）

- 唯一实现 `_lib/text.py` 的 `count_body_words`（CJK 字符 + ASCII 词，剥离 HTML 注释与 Markdown 标题行）；审查/合并/导出/概览同口径。
- 章节字数目标 **90%–120%** 为 pass；summary 的 200–400 是**字符长度**（len，含标点），不是字数。

## 分层与短路

- A 脚本：结构、完整性、标题越级、字数、图表（table 只校题注/行列；**table 出现 BG:FIGURE 记 major `marker_format`（A 层拦截，与 bg-write 硬规则一致）**；mermaid/terminal 校标记↔figureIndex+png+源文件）、**图表机检读取 render-manifest 的 layout（字号/宽高/行数）**、题注、引用、JSON、禁用内容、手工修改检测。
- A blocker → 短路 B/C（只看仍未关闭的 blocker）；B 覆盖度语义（逐章 summary+criterionRefs，pass/weak/missing）；C 图文语义（脚本机检 + 语义判断）。
- C 无资源/无图表 → 直接通过。

## 通过判据（T7）

`blocker==0 ∧ major==0 ∧ (minor==0 ∨ 剩余 minor 全 wontfix 且 userConfirmed)`；major 必须 verified 或 wontfix+userConfirmed（`fixed` 不满足 T7）；覆盖率全部 pass/notApplicable；三层本轮已执行。到达 `reviewLoop.maxRounds` 未通过 → 停 review 输出未收敛报告，不自动通过。

## 异常

| 场景 | 处理 |
|---|---|
| 脚本误报 | wontfix（需 userConfirmed）或修脚本（版本化） |
| 审查子智能体超时 | 单章 ≤10 分钟；记 review_timeout（minor），不阻塞其他章 |
| issues 损坏 | 从 `rounds/round-<k>.issues.json` 恢复；否则重跑该轮 |
| 上轮 fixed/未 verified 条目在新一轮丢失 | `bg_review_issues.py carry-forward --round <k>` 补回（先 `--dry-run` 看清单） |
| 优化引入新问题 | 下一轮必须重覆盖；问题数上升报「质量回退」；带入项复开为 open（recurred） |
| 用户要求关 blocker | 不允许；只能修复/按规则改级别 |
| 循环达上限 | 未收敛报告并停 review |
| 脚本未就绪/失败 | 原样报 error/message/hint；不手写 issues |

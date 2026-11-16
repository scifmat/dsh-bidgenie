# F6 优化命令与异常参考（按需读）

## 命令

```text
# 问题台账（读）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" status --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 关闭/流转（optimizer 只写 fixing/fixed；verified 由审查写）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" close --issue "RV-<round>-<seq4>" --status fixed --note "<改了什么/涉及文件/前后字数>" --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# 4.6 判据（只读）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_review_issues.py" verify --round <k> --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
# T8 回到 review
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to review --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to review --package "package-<N>" --workspace "<ws>" --project "<projectDir>" --json
```

- 无 `--summary`/`--check`：摘要用 `status`，判据用 `verify`（顶层 `--check` 等价 verify）。
- `wontfix` 需用户确认（`--user-confirmed`）；blocker 不允许 wontfix；major 降级需用户确认。

## 批量优化

- 同一章节/相邻主题的问题合并给**一个**优化子智能体（每批约 5–10 个 issue 或 3–5 章）。
- 每批交付：修改后的章节 md/资源、每个 issue 的 `resolutionNote`、`reviews/rounds/round-<k>.optimization.json`（处理方式/文件/前后字数/是否需复审）。
- 统一口径（响应时限/自设指标/模板化开头）要记录统一前后与涉及章节。

## 异常

| 场景 | 处理 |
|---|---|
| 修复引入新问题 | 下一轮审查必须重覆盖；问题数上升报质量回退 |
| 无法修复 | wontfix（需用户确认）或升级结构性问题走 R1；blocker 不允许 wontfix |
| 文件锁定 | `E_LOCKED` → 停止，说明需先 R4 解锁 |
| 优化子智能体超时 | 按批限时（单章约 20 分钟）；保留已改内容，报告未完成问题 |
| issues 状态冲突/损坏 | 从本轮快照恢复；否则回上一轮重审 |
| 用户改字数/配额 | 走 R3（备份大纲 → 回 outline 重算） |
| 轮次达上限 | 停止并输出未收敛报告 |
| 脚本未就绪/失败 | 原样报 error/message/hint；不伪造 |

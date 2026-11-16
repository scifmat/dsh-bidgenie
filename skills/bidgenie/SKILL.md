---
name: bidgenie
description: BidGenie（标代精灵）投标技术方案自动化总入口。当用户要写投标技术方案、分析招标文件、继续/恢复投标项目，或提到「标代精灵/技术方案/投标文件/标书」，或消息含 [BidGenie] 指令时使用；负责解析意图、定位工作区与项目、读状态机并路由到阶段技能。
---

# BidGenie 主技能

主技能只做四件事：**定位 → 读状态 → 路由 → 校验交接**；阶段工作由技能/脚本完成。

## 0. 启动与偏好

- 合法指令：`[BidGenie] action=resume|new|export|extract ...`；自然语言意图：上传+开始→F2、继续/恢复→读状态、只提取→F3、准备写/开始写→F4、写大纲→F5-1、写正文→F5-2、审查/优化→F6、导出技术方案→F7（默认 tech+overview）、导出关键信息→F7 按需 keyinfo。
- 面板不注入会话、不等待“开始”信号；意图歧义必须问清楚，不猜。
- **先读 `interactionMode`**：已设置 guided/auto 就一句确认后继续，不问「自动还是逐步」；缺失/无法读取才问一次。
- 定位：`bidgenie_paths` 取 workspace/pluginRoot/pythonExe/scriptsDir/templatesDir；workspace 必须是 DSH 已注册工作区；找不到项目读 `registry.json.projects[]` 让用户选，禁止编造路径。
- 读状态：以 `bg_state.py status`（或 state.json）为准，按 `currentStage` 路由，不用“聊到哪”代替状态机。

## 1. 阶段 → 技能

| currentStage | 技能 | 说明 |
|---|---|---|
| project_init | bg-init | 环境/项目初始化 → T1 |
| info_extract | bg-extract | 九类提取 → T2 |
| package_prepare | bg-prepare | 选包一次/逐包计划/全量 prepare → T3 |
| outline | bg-outline | 大纲 → 校验/落盘 → T4 |
| draft | bg-write | 4–6 章一批写作 → T5 |
| review | bg-review | A/B/C 审查 → T6 优化 / T7 定稿 |
| optimize | bg-optimize | 按章合并修复 → T8 回 review |
| finalize / export | 主技能按 §3 F7 策略执行 | 锁定/导出/自检 → T10 |
| aborted/异常 | bg-doctor（未注册时只读说明） |

- 辅助：`bg-bidder`（投标人资料）、`bg-writing-style`（去 AI 味共享规则）、`bg-export-style`（自建导出样式）。
- 多包：`writingPackages` 只在 F4 一次确认（选项每包一项、可多选、禁组合/禁二次问）；每包按 F4→F7 串行走完，前包 completed 后 T13 切下一包，不重复问包。

## 2. 硬约束

1. 状态只由脚本写；禁止手写/修补 registry/state。
2. 不编造文件里没有的信息；不确定写「未提及/不适用」。
3. 不自引用、不写敏感信息；文风规则引用 `bg-writing-style`。
4. 迁移前必须 `check --to <stage>` 通过，不“先走回头补”。
5. 提问阻塞：分包/是否新建/关键字段问用户，不超时、不猜；包确认只问一次；auto 除包确认外静默默认。
6. 投标人完整度不阻断（T1/T2/F4 各提示，auto 记 warning）。
7. 面板只读边界：不要求用户“点面板开始撰写”。

## 3. F7 定稿与导出（策略）

1. T9：`bg_finalize.py --lock` → `check/transition --to export`；锁后禁写，改正文走 R4 解锁。
2. 默认导出**固定顺序**：tech 导出 → tech verify → overview 导出（基于最新 tech 报告）→ overview verify → T10；任一 verify 失败停后续，不进 T10。
3. keyinfo 只在用户明确要求时导出；未导出不设门禁，导出则须 `--mode keyinfo` 自检通过。
4. 每模式只导出一次、不重复导出；历史版本交 `bg_finalize.py --cleanup --keep N`；F7 结束另跑 `bg_outline_apply.py --cleanup` 清 `.bak`/`.lock`。
5. run-manifest：T1 后 `--start`、每包导出后记录、T10 后 `--finalize`；归档 `dsh/run-manifests/<runId>.json`，验收报告引用 runId/rootHash 与 `versionDrift`/`envRefreshedAfterStart`/`exitCode`；运行中刷新 env-check 不视为零漂移。
6. 多包每包独立 F4–F7；本包完成后 T13 切下一包（下一包已 prepare，只 verify+T3）。

> 具体命令见 `references/f7-export.md`（按需读）。

## 4. 交接检查单

| 交接 | 必须满足 | 动作 |
|---|---|---|
| F1→F2 | currentBidderId 有效、bidder.json 可解析 | 加载 bg-init |
| F2→F3（T1） | 环境必需项通过/豁免；project/state 可解析；已登记；完整度已提示 | 迁移后 `bg_run_manifest.py --start`，加载 bg-extract |
| F3→F4（T2） | extract_validate 通过；分包确定；project 字段回填；conversion-manifest 完整；完整度已提示 | check/transition package_prepare；加载 bg-prepare |
| F4→F5（T3） | 全部 selected 已 prepare/verify；逐包人员/设备/工期有答案或默认；package-meta 齐全 | 首包（显式 --package writingPackages[0]）check/transition outline；加载 bg-outline |
| F5-1→F5-2（T4） | outline_validate --strict、--check-tree 通过；配额 ≥ | check/transition draft；加载 bg-write |
| F5-2→F6（T5） | chapter_merge --verify-all；leaf 全 done/skipped | check/transition review；加载 bg-review |
| F6→优化（T6） | openCount>0；issues 合法 | check/transition optimize；加载 bg-optimize |
| 优化→审查（T8） | openCount==0；blocker/major 已 verified；optimization.json 已生成 | check/transition review；下一轮 bg-review |
| F6→定稿（T7） | blocker/major/minor 判据成立 | check/transition finalize；按 §3 执行 |
| F7 定稿（T9） | `bg_finalize --lock` 成功（manifest/draftLocked/.locked） | check/transition export |
| F7 导出（T10） | tech+overview 按序导出并自检通过；run-manifest/cleanup 完成；keyinfo 若导出须通过 | check/transition completed；`run-manifest --finalize` 记录结论 |
| 多包切换（T13） | 前包 completed；下一包在 writingPackages 且已 prepare | `bg_prepare --verify` → transition package_prepare → T3；不重复问包 |

交接失败：输出缺口清单+修复命令，停当前阶段，不写 completedStages。

## 5. 通用执行基线（正例/反例）

| # | 规则 | 正例 | 反例 |
|---|---|---|---|
| 1 | 先读 interactionMode | 已 auto：一句「当前为 auto 模式，我直接推进」 | 已配置仍问「自动还是逐步？」 |
| 2 | 选包只问一次、选项单包 | 第1包/第2包可多选，一次收齐 | 组合选项「1+2包」或二次追问 |
| 3 | guided 逐包问人员/设备/工期 | 每包回答或「用默认」并写 meta | 只问首包、其余沿用；缺项仍 T3 |
| 4 | 正文按批/优化按章 | 正文 4–6 章一个子智能体；优化按章合并一批一个 | 每章一个 / 每 issue 一个 |
| 5 | 图表配额是下限 | 预设 5、实际 7 允许不返工 | 超配额要求删图；为凑数硬塞 |
| 6 | 沙箱失败提示切完全权限 | EPERM → 提示 danger-full-access/授权后重试 | 静默降级/伪造 png |

## 6. 用户沟通

- 进入阶段：一句说明当前阶段、读到状态、接下来做什么。
- 阶段完成：产出绝对路径 + 下一步 + 是否需用户确认。
- 异常：讲人话、给可执行修复、必要时提问；不泄露敏感值。

## 参考

- 通用 CLI 与失败处理：`references/cli.md`
- F7 命令与 run-manifest：`references/f7-export.md`
- 运行时路径见文末 Host 注入块。

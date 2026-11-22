---
name: bg-prepare
description: BidGenie 撰写前准备（F4）。当项目处于 package_prepare 阶段，需要确认要撰写的包（只问一次）、按每个需撰写包各 7 项问题收集字数/人员设备/工期/图表配额（guided 逐包提问或 auto 静默填）、生成包目录与 package-meta、核对必存文件并完成 T3 迁移进入大纲阶段时使用；用户说「准备写方案/开始撰写/确认字数和图表数量」时触发。
---

# bg-prepare（F4 撰写前准备）

## 范围与边界

- 只关注技术范围/技术评分；商务问题（合同分包/中小企业/报价）不提问、不写入 package-meta。资格/证书要求只以 info 提示输出（供用户核对，不阻断、不要求补材料）。
- `package-meta.json` 只存包级决策/配置（7 项答案 + valueSource、字数、三类图配额、人员/设备/工期、样式、时间戳）；不复制提取内容。
- 7 项 = 字数/人员/设备/工期/表格/示意图（Mermaid）/终端图参考值；**导出样式不是提问项**，默认取 `styleProfiles.tech` → `styleProfile` → `official-tech` 写 `exportStyleProfile`。
- 工期字段语义分开（P2-9）：`schedulePlan.servicePeriod` = 合同履行期限/服务期（如「1 年」）；`schedulePlan.constructionPlan` = 施工/供货/交付工期；`startDate`/`deliveryDate` = 具体日期（YYYY-MM-DD，不编造）。auto 只在对应期限明确时填对应字段，`autoFill.autoBasis.schedulePlan` 记录映射来源；写作任务书不要引用 `constructionPlan` 当服务期。
- 字数默认**动态**：`user-settings.defaultWordCountTier`（缺省 50000），提问卡显示实际值，并与提取要求下限取大值，不写死。
- 图表配额：table/mermaid 是下限、上不封顶；terminal 是参考值（0=不生成，少规划不报错）；人员/设备/工期 guided 每包必须有答案或「用默认」。
- `qualificationGaps` 是 **资格/证书要求提示（商务部分，供用户核对）**，`blocking=false`：按 02/08/09 资格硬项比对投标人资料，**按“第N包/第1—3包/第4、5包”过滤当前包，跳过“不适用于本包/本包未要求”**；每包 ≤5 条、`missingTokens` 为整条短语；**不阻断 F4/T3，技术方案不据此判定、不要求 Agent 补材料**。

## 偏好与选包（只问一次）

1. 先读 `interactionMode`：guided/auto 已设置就一句确认，不问「自动还是逐步」；缺失才问一次。
2. 单包：`writingPackages=["package-1"]`，不提问。
3. 多包：只问「本项目分 {N} 包，您需要撰写哪些包的技术方案？」；选项**每包一项**（第1包/第2包…，可多选）；不支持多选就让用户输入「2、4」；**禁止组合选项与二次询问**。一次收齐并回填。

## guided 逐包提问

- 按包逐个展示该包人员/设备/工期要求并请用户回答（或明确「用默认」）；答案/默认写入该包 `personnelPlan`/`equipmentPlan`/`schedulePlan`（含 servicePeriod/constructionPlan）+ `valueSource`。
- 每包 7 项汇总后展示该包摘要（字数/人员/设备/工期/三类图/未用默认项/留空项），确认后再执行该包 prepare；不合并多包摘要。
- 人员/设备用单文本输入框，原样记录；不脱敏/不裁剪/不结构化/不编造。
- **任一包人员/设备/工期缺失且无默认记录 → 不得 T3**，停 `package_prepare` 补齐。

## auto 模式

- 仅「需撰写包确认」阻塞（仍只问一次）；其余 7 项不提问，`bg_prepare.py --auto` 静默填（autoValue/autoBasis），人员/设备用脚本合规描述；汇报列静默默认项与 autoFill 来源。
- 人员描述：岗位表逐行镜像数量；表内「合计」行不当作岗位，但会以「，合计N人」追加为总量口径（招标文件声明多少就镜像多少）。

## 执行顺序（多包）

1. 先对全部 `writingPackages` 执行 `bg_prepare.py --all-selected`（或循环 `--package`），逐包 `--verify`。
   - `--all-selected` 的中间包 `readyForT3=false` 是顺序中间态：结果带 `pendingReason="sequence-in-progress"`、汇总 `pendingPackages`/`summary.note`；**以汇总 `readyForT3`（最后一包）为准**，不要误判 F4 失败。批量准备只在首包切换一次 currentPackage（`summary.historyNote`），中间包不再写 set-package 事件，历史里不会被误读成来回切包。
   - `--verify` 与 prepare 输出同契约：`{ok,packageId,verified,readyForT3,packageTree,t3Check}`；单包时 `packageId` 为 `package-<N>`。
2. 全部通过、且 guided 逐包人员/设备/工期有答案或默认后，对 `writingPackages[0]` **显式** `transition --to outline --package "<writingPackages[0]>"`（T3，不依赖 currentPackage）。
3. T13 回来后下一包只 `--verify` + `set-package` + 显式 T3，不重复 prepare/提问。
4. T3 失败：全部 selected `--verify` 出缺包清单，补齐 prepare 后重试，不强行迁移。
5. post-check 断言 `currentStage=="outline"` 且 `currentScope=="<writingPackages[0]>"`；不成立报 `E_STATE_CORRUPT`。

## 校验与汇报

- 文件存在性：project.json/state/package-meta（7 项含逐包人员设备工期）/包目录（`resources/{mermaid,terminal}`、export）；不要求 network/tables。
- 汇报：要写的包列表、当前包字数/配额/日期（servicePeriod/constructionPlan 分开写）、人员/设备（用户原话或 auto 合规描述）、valueSource、T3 结果、迁移/资格证书提示（商务部分，info 不阻断）。
- 下一步加载 `bg-outline`。

## 硬约束

1. 不编造；无法推导留空或模糊表述（servicePeriod/constructionPlan 不互相顶替）。
2. 状态/元数据只由脚本写；不手写 package-meta/目录。`set-package`/`lock` 在 history 记为 `kind=event` 事件（不是阶段迁移）。
3. 不越过 package-meta；字数/配额/人员/设备/日期以后续唯一口径。
4. 商务边界；技术范围/评分点之外不提问。
5. 包确认只问一次；guided 逐包必问人员/设备/工期；auto 除包确认外静默默认。

## 参考

- 命令、answers 示例、异常：`references/cli.md`（按需读）
- 运行时路径见文末 Host 注入块。

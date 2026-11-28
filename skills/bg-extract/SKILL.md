---
name: bg-extract
description: BidGenie 招标文件关键信息提取（F3，人读模板版 v4）。当项目处于 info_extract 阶段，需要把已转换的招标文件按九大类模板提取为「可直接导出阅读」的提取稿（转换不可辨/矛盾照录）、回填项目字段、结构校验、按需导出关键信息时使用；用户说「提取关键信息/提取招标要求/识别评分标准/分包情况」时触发。
---

# bg-extract（F3 关键信息提取 · 人读模板版 v4）

## 0. 第一原则：输出是"给人读的成稿"，不是机器字段表

每个提取文件都必须是 **`# 模板标题` + 若干 `## 人性化章节` + 人读表格/段落**，风格严格模仿模板的「输出示例」。

**正例（01 基础信息，节选）：**

```markdown
# 基础信息

## 项目信息

| 项目属性 | 属性值 | 备注 |
|---|---|---|
| 项目编号 | WHZC-2025-00200-GK00136 | 来源：第一章 招标公告 |
| 项目名称 | XX市智慧城市综合管理平台建设项目 | 来源：第一章 招标公告 |
| 采购方式 | 公开招标 | |
```

**禁止（用户已明确反馈的错误形态）：**

- ❌ frontmatter 字段表（`--- templateId: "01" … ---`）——v2 提取文件**不写 frontmatter**；
- ❌ 机器表头：`字段键 / 字段值 / 字段名`、独立「证据（来源 · 章节/条款 · 页码）」列；
- ❌ 正文用英文机器键（`projectNo`、`budgetAmount`…）当展示名；
- ❌ 标题硬编码「一、二、」「1.」「2.1」等编号（编号由导出脚本自动生成）。

> 判断标准：把提取文件原样贴进 Word，用户应能**直接阅读**——看不到任何机器字段名/证据列/编号。

## 1. 进入条件

- 主技能路由而来：项目 `state.currentStage == info_extract`。
- 源文件就绪：`<projectDir>/sources/conversion-manifest.json` 存在，主招标文件已转换。
  **转换器已把原件复制到 `sources/raw/`**（见 `conversion-manifest.files[].rawCopyPath`，`--no-copy-raw` 可关闭）；**由 Agent 语义分类并复制 converted md** 为固定命名：
  `sources/01-bidding-documents.md`（主招标文件）、`02-technical-requirements.md`（技术要求）、
  `03-procurement-list.md`（采购清单）、`04-evaluation-criteria.md`（评审标准）、
  `05-construction-instructions.md`（施工说明）、`06-proposed-plan.md`（方案建议书）、
  `07-<内容描述>.md`（其他）；分类不确定时询问用户。完整规则见插件根 `CONTRACT.md §10.1`（随包发布）。
- 转换回退提示（大 PDF 必看）：manifest 的 `files[]`/`sources[]` 条目带
  `fallbackPageCount/fallbackPages/keyDataFromFallbackOnly/fallbackNote`，warnings 含
  `key_data_from_fallback_only:<n>`、`pdf_pages_fallback:<n>` 时表示**该页结构化内容（表格/版式）由纯文本回退补齐**；
  另有 `tableRecheck.mode=sampled`（>60 页大文件抽样复核）与 `pdf_table_recheck_large_pdf:` warning。
  涉及这些页的数字/条款，提取时须在备注标「关键数据仅来自回退文本（第 N 页）」，并在 F3 汇报中显式提示用户核对 PDF 原件（见 §5.1）。
- 模板就绪：`{{TEMPLATES}}/extraction/_template-manifest.json`（v2，`templateVersion 2.x`）+ 9 个模板正文；缺失 → 停止并如实告知「提取模板尚未随包提供」，不伪造。
- 模板读法：每个模板读「提取指引」「提取清单」「**输出示例**」三部分；**输出格式只以「输出示例」为准**，清单只用于字段覆盖自检。
- 05 实质性应标资料按包拆分，单包建议 1500–4000 字；多包人员/车辆/承诺必须逐包分行导致超长时，按包分节保留可读性，并在 F3 汇报说明（不为了字数压缩而丢信息）。

## 2. 模板与输出路径

- 读 manifest（v2）：`templates[]` 的 `id / docClass / fileName / title / expectedSections / path`。
- 输出路径（文件名取 manifest `fileName`，不自行改文件名/硬编码旧文件名）：

```text
<projectDir>/extraction/common/<fileName>              # 01–05 公共类
<projectDir>/extraction/packages/package-<N>/<fileName>  # 06–09 包类，每包各自生成
```

- 文件首行必须等于 `# <manifest.title>`（如 `# 基础信息`、`# 评审标准`）。
- `##` 章节 = 该模板 manifest `expectedSections`（人读标题、无编号）；`expectedSections` 为空的模板（如 09）按模板「输出示例」自然分节，不强行造章节。

## 3. 流程（按序执行）

1. **分包裁决（必须用户裁决）**：从主招标文件与方案建议书判定「是否分包 / 包数 / 各包名称」；不确定或矛盾 → `ask_user_question`（阻塞、不超时、不猜）。结论写入提取内容并在汇报中说明。
2. **公共信息 5 类**：以主招标文件为主、其他来源为辅，按各自模板「输出示例」产出 `01–05`。
3. **包信息 4 类（每包）**：对每个包产出 `06–09`；各包要求相同也**分别生成**（保证包目录自含，可在备注注明共用关系）。
4. **回填 project.json（脚本）**：整理字段 JSON 到临时文件，调用：

   ```text
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_project.py" fields --workspace "<ws>" --project "<projectDir>" --data "<临时字段.json>" --json
   ```

   白名单：`projectNo / projectName / isSubcontracted / packageCount / packagesToWrite / sourceFiles`（`sourceFiles` 条目需 `originalName/rawPath/convertedPath`，路径为**项目目录相对** POSIX 路径（如 `sources/raw/x.pdf`，不要带项目目录名））。脚本报错按 `hint` 修数据后重跑；成功后删除临时 JSON。**禁止手写 project.json/state.json**。
   - `packagesToWrite` 两种合法写法：①**不传** → 按 `packageCount` 自动生成 `package-1..N`；②显式传 `[]` 或 `packageCount` 内子集 → 表示「撰写包待 F4 确认」，脚本接受并返回 warning/`packagesToWriteNote`；**最终撰写包由 F4 选包（`bg_project.py select-packages`）覆盖**，F3 不必强行定死。
5. **结构校验（必须）**：

   ```text
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_extract_validate.py" --workspace "<ws>" --project "<projectDir>" --json
   ```

   校验维度（M3.2 v2）：9 文件存在、首 `#` 标题与 manifest title 一致、`##` expectedSections 齐全、**无机器表头/无 frontmatter**、07 评分表三列（评审因素/分值/评分标准）+ 合计、表格/正文非空。
   - `coverage.criteriaCount` 口径：`criteriaCountScope=all_packages_total` 时为**全包合计**（`criteriaCountNote` 已写明、`criteriaByPackage` 给分包明细）；单包 outline/审查不要按全包合计核对，按单包个数取。
   - `extractionCaveats`（写回 `project.extensions`）：按包合并展示、纯标题行（`**需用户确认：**` 之类）已丢弃、同包内相似重复项已合并（`duplicateCount/sources` 可见合并来源），汇报时用 `caveatsByPackage` 分片说明。
   有 blocker → 按报告修复（保持人读格式）后重跑，**≤3 轮**；仍失败 → 输出缺口清单 + 需用户补充项，停在 `info_extract`。
   - **回退页/表格/复选框阻塞确认**：若报告出现 `fallback_review_required`，必须把 `fallbackReview.risks`（回退页关键字段、跨页表格、复选框/符号不可辨）逐条交给用户核对（`ask_user_question`，不猜、不跳过）；用户确认后重跑并带确认说明：

     ```text
     "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_extract_validate.py" --workspace "<ws>" --project "<projectDir>" --confirm-fallback --note "<用户确认结论（含受影响字段/是否以提取稿为准）>" --json
     ```

     未确认时 T2 guard 也会因 `extractionFallbackReview.required=true` 失败。**`--note` 至少 8 字符，必须写清核对了哪些页码/字段/表格**（如“已核对第7页回退文本与采购清单跨页表，截止时间/工期按 PDF 原件确认”），空话会被脚本拒绝。**可与 F4 选包合并成一次提问**：若 F3 分包裁决与 F4 选包同源（同一份招标文件、用户本就要确认包号），把“回退风险确认 + 撰写包选择”放进同一个 `ask_user_question`，用户只被打断一次；两项结论分别落 `--confirm-fallback --note` 与 `bg_project.py select-packages`。
6. **关键信息导出（用户要求时）**：

   ```text
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_docx.py" --workspace "<ws>" --project "<projectDir>" --mode keyinfo --out "<abs.docx>" --json
   ```

   导出后自查：无封面/目录、标题由 Word 自动编号（不重复提取稿编号）、九章齐全、表格与题注保留、公开联系信息未脱敏。
7. **语义抽检（对照模板输出示例，2–3 节）**：至少抽检 `01 基础信息`、`07 评审标准`，再抽 1 个要求类（`05` 或 `08`）：
   - 首标题/章节与模板示例一致、无编号、无机器表头/无 frontmatter；
   - 表格是「项目属性/属性值/备注」等人读表头；
   - 关键数值有来源括注或备注；
   - 缺失值是「未提及/不适用」，不是空/无/TBD/-；
   - 07 三列齐全且分值可解析、合计与满分对得上；
   - 转换不可辨/疑似丢失的信息是否已按 §5.1 照录可见内容 + 标注 + 列入需用户确认（无猜选/静默丢失）；
   - 章节/前附表/附件矛盾是否按 §5.2 分别摘录并标「见冲突（位置A vs 位置B）」；
   - 公开联系人/电话/邮箱完整未脱敏。
   抽检不合格 → 重写对应章节 → 重跑第 5、6 步。
8. **汇报与后续**：9 类文件绝对路径、分包结论、校验结果、抽检结论、缺口/待用户补充、导出路径（如有）。
   - 独立提取模式（用户只要求解析/导出关键信息，未说要继续写方案）：只登记 `extractOnly`，**本阶段不自动迁移 T2**；用户明确说「继续/准备写方案」才走 T2。
   - 用户要「继续/进入准备包」→ 先 `bg_state.py check --to package_prepare` 预检并汇报 guard，迁移按主技能与里程碑约定执行。

## 4. 内容与格式规则

1. **章节与表格**：章节标题用模板输出示例中的中文标题；表格表头按内容人读化（如 `项目属性 / 属性值 / 备注`、`证书名称 / 是否必需 / 具体要求 / 备注`、`评审因素 / 分值 / 评分标准`）。
2. **来源**：写在「备注」列或值后括注，如 `（来源：第二章 投标人须知 · 2.1）`；关键数值/时间/分值必须可回查；无页码写「章节」即可，**禁止编造页码**；不要新增独立证据列。
3. **缺失值**：仅允许「未提及」（文件确实没有）或「不适用」（项目情形不涉及，备注写理由）；不得留空，不得写 `无 / 待定 / TBD / - / 略`。
4. **冲突**：同一信息多来源、或不同章节/前附表/附件之间不一致 → 按 **§5.2** 照实分别摘录并标位置，值写「见冲突（位置A vs 位置B）」，列入「需用户确认」，**不得自行裁决/择一**。
5. **公开联系信息必须提取、不脱敏**：采购单位、代理机构、联系人、公开电话、邮箱、地址、开标地点等按原文保留（用户核对与后续投标联系需要）。
6. **脱敏与信息边界（自包含）**：面板显示层负责脱敏（S14）；提取阶段按原文事实记录，公开联系信息（采购单位/代理机构/联系人/公开电话/邮箱）原文保留、不额外裁剪；技术方案正文中的模糊表达/敏感信息处理由 `bg-write`（M6）负责。
7. **只记录事实**：不写符合性判断、建议、风险、得分预测、方案语言；05/08 只写「要求什么」，不判断投标人是否满足。
8. **07 评审标准**：技术评分表列名固定三列（评审因素 / 分值 / 评分标准）；每行一个评审因素、分值必须可解析为阿拉伯数字（`15` 或 `15分` 均可）、保留原文 ①②③ 子项；**必须含 `## 分值合计` 章节**（技术/商务/报价/总分合计），技术分合计必须与招标文件声明的技术分满分一致（容差 0.5），差额写明细并入用户补充；评审因素 <3 条提示可能缺页但不补造。
9. **包类文件**：`06–09` 按包独立成文，不跨包混写；共用内容注明「与 package-1 共用」仍分别生成。

## 5. 转换不确定性与内部矛盾（用户新规则，必须遵守）

### 5.1 转换不确定性：照录可见内容 + 标注 + 列需用户确认

招标文件原件中的版式语义在转 Markdown 时可能丢失或不可辨，典型情况：

- 复选框/单选框勾选状态（`☑`/`□`、`[x]`/`[ ]`、`●`/`○`、`√`/`×`）；
- 特殊符号（`★`/`▲`/`◆`、`①②③`、箭头、勾叉）；
- 合并单元格（表头跨行/跨列、一格多值、行列错位）；
- 形状/文本框/艺术字/页眉页脚/批注中的文字（转换器可能整块丢弃）；
- 删除线、上下标（如 `H₂O`、`m²`、`x²`、`≤`、`≥`）。

**处理规则：**

1. **原文照录看到的内容**，不替原文补全；
2. 在值/备注中标注「转换不可辨」或「疑似丢失」，并列入「需用户确认」清单；
3. **禁止猜选**（不得因为"看起来像勾选"就替用户选一个）、**禁止静默丢弃**；
4. 关键字段（是否分包/包数/投标截止/评分分值/资格门槛）受此影响时，必须 `ask_user_question` 阻塞询问，不得猜值继续。

示例：前附表显示「`☑不允许` / `□允许`」，但转换后的 md 无法判断勾选状态 →

```text
值：见冲突：原文复选框状态不可辨，需用户确认（原文照录：☑不允许 / □允许）
备注：转换不可辨 · 位置：投标人须知前附表 · 已列入需用户确认
```

（不写"不允许"，也不写"允许"。）

**回退文本页（`keyDataFromFallbackOnly`）的额外处理：**

1. 识别：manifest 条目 `keyDataFromFallbackOnly=true` / `fallbackPages=[…]` / `fallbackDetail`（含 `placement=inline|appendix`），正文里对应「第 N 页（转换回退文本，仅缺失行）」（页标可定位时就地补录，否则文末「## 第 N 页（转换回退文本）」附录）；
2. 该页内容**只作线索**：表格结构、合并单元格、跨页对应关系可能已丢失，数字/单位/数量极易错位；
3. 提取稿备注必须写「关键数据仅来自回退文本（第 N 页），需核对 PDF 原件」，并列入需用户确认；
4. **F3 汇报必须显式提示**：列出回退页号与受影响的字段/数值（如各包设施数量、服务范围），提示用户核对原件后再进入写作；
5. 同名关键数值（如不同包的设施数量）跨页合并前，先确认是否来自同一页/同一表；无法确认时按「见冲突/需核对」记录，不自行合并；
6. `tables_maybe_lost_estimated` / `pdf_table_recheck_*` 时：07 评分表、05 人员/车辆表、06 采购内容表必须对照 PDF 复核跨页表头/合并单元格，不把转换稿的断行当独立行。
7. **阻塞式确认**：`bg_extract_validate.py` 会把上述回退页/跨页表格/复选框风险按类型合并（同类多文件命中只显示一条，`sources/fields/excerpts` 列全部来源）并报 `fallback_review_required` blocker；必须逐条 `ask_user_question` 核对，用户确认后才能用 `--confirm-fallback --note`（note ≥8 字符，写清页码/字段）继续；不允许只看 warning 就推进。

### 5.2 内部矛盾：照实分别摘录，不裁决

不同章节 / 前附表 / 正文 / 附件之间信息冲突时：

1. **不裁决、不择一、不按"更合理"取舍**；
2. 按原文**分别摘录**，并在值中标注位置差：值写「见冲突（位置A vs 位置B）」；
3. 在对应章节明细中并列列出「位置A：<章节/条款/表格> → 原文值」「位置B：…」；
4. 列入「需用户确认」清单，汇报时显式列出，继续提取其他不受影响的内容。

示例：前附表「投标有效期 90 天」vs 合同条款「投标有效期 120 天」→

```text
值：见冲突（投标人须知前附表 vs 合同条款）
备注：前附表：投标有效期 90 日历天（来源：投标人须知前附表）；合同条款：投标有效期 120 日历天（来源：合同条款第 3 条）· 需用户确认
```

### 5.3 说明：正文写作规则归 bg-write；商务问题不属于技术方案范围

提取阶段只按本技能要求**如实摘录事实与标注**（转换不可辨/内部矛盾/缺失值）；正文写作阶段的模糊表达、承诺用语、材料引用等规则由 `bg-write`（M6）负责，不在本技能中提前约束。

商务类条款（合同分包、专门面向中小企业等）在提取稿中**只作事实记录**；技术方案的提问/大纲/正文不处理商务问题，技术方案只关注技术范围与评分点（如 365 处）。

本技能产出的 `<projectDir>/extraction/**` 是业务/技术要求的**唯一事实源**：F4/F5 直接读取，不在 package-meta 复制内容或生成材料清单。

## 6. 常见错误 → 正确做法

| 错误（用户反馈根因） | 正确做法 |
|---|---|
| 输出成 `## 一、字段值` + `字段键/字段名/值/证据` 机器表 | 按模板「输出示例」写 `# 标题` + `## 人读章节` + 人读表格 |
| 输出带 frontmatter 字段表 | v2 提取文件不写 frontmatter |
| 标题硬编码「一、二、」「1.」「2.1」 | 只写标题文字，编号由导出脚本自动生成 |
| 来源单独做「证据」列 | 来源写备注/括注，不新增证据列 |
| 公开联系人/电话/邮箱被脱敏或漏提 | 原文保留、完整提取（公开信息） |
| 面板显示层/正文未脱敏 | 面板显示层脱敏（S14）；正文模糊表达归 `bg-write`（M6）；提取稿只记事实 |
| 转换不可辨（复选框/合并单元格/文本框/删除线/上下标/符号）时猜选或静默丢弃 | 原文照录可见内容 + 标「转换不可辨/疑似丢失」+ 列需用户确认；禁止猜选/静默丢（§5.1） |
| 章节/前附表/附件矛盾时自行裁决或只留一处 | 分别摘录并标「见冲突（位置A vs 位置B）」，交用户确认（§5.2） |
| 关键数值没有来源 | 备注/括注标来源，可回查 |
| 各包相同就只写一份 | 每包分别生成，共用关系用备注说明 |
| 07 只写评分表、漏「分值合计」 | 必须补 `## 分值合计`（技术/商务/报价/总分），技术分合计与声明满分对齐 |
| 校验不过就退回机器表 | 保持人读格式修复；若校验脚本仍按旧机器格式报错，记录「校验版本待同步」并上报，不降级输出 |

## 7. 常用 CLI 清单

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_convert.py"        --workspace "<ws>" --project "<projectDir>" --files "<abs1,abs2>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_project.py"        fields --workspace "<ws>" --project "<projectDir>" --data "<fields.json>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_extract_validate.py" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_extract_validate.py" --workspace "<ws>" --project "<projectDir>" --confirm-fallback --note "<用户确认结论>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_export_docx.py"    --workspace "<ws>" --project "<projectDir>" --mode keyinfo --out "<abs.docx>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py"          status --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py"          check --to package_prepare --workspace "<ws>" --project "<projectDir>" --json
```

- 一律用 `bidgenie_paths.pythonExe` 与绝对 `scriptsDir`；stdout 只解析单个 JSON；`ok:false` 原样汇报 `error/message/hint`。
- 脚本未随包提供 → 如实告知，不伪造输出。

## 8. 失败处理

| 场景 | 处理 |
|---|---|
| manifest/模板缺失或不可读 | 停止并如实告知；不凭经验自造模板 |
| 主文件未转换 / 转换质量差 | 先转换或请用户换文件；质量差在汇报中标注「可能不完整」 |
| 分包矛盾/无法判定 | 必须问用户，不猜不默认 |
| 转换不可辨/疑似丢失（复选框/合并单元格/文本框/删除线/上下标等） | 照录可见内容 + 标注「转换不可辨/疑似丢失」+ 列需用户确认；关键字段阻塞提问；不猜选、不静默丢（§5.1） |
| 大 PDF 回退页（`keyDataFromFallbackOnly` / `pdf_pages_fallback:`） | 该页数字/条款标「关键数据仅来自回退文本（第 N 页）」，F3 汇报显式提示核对 PDF 原件；不自行合并/裁决（§5.1） |
| 章节/前附表/附件内部矛盾 | 分别摘录 + 标「见冲突（位置A vs 位置B）」+ 交用户确认；不裁决（§5.2） |
| 校验 blocker | 保持人读格式修复，≤3 轮；仍失败 → 缺口清单 + 用户补充，停 `info_extract` |
| 校验脚本仍要求旧机器格式 | 不退回机器表；记录「校验脚本版本待同步」上报（M3.2 并行交付 v2） |
| 抽检不合格 | 重写相关章节，重跑校验与导出 |
| 脚本缺失/超时 | 报错 + 恢复建议；不静默重试超过 2 次 |

## 9. 资源

- 模板：`{{TEMPLATES}}/extraction/`（`_template-manifest.json` + 9 模板；输出格式只认模板「输出示例」）
- 脚本：`{{SCRIPTS}}/`（bg_convert / bg_project / bg_extract_validate / bg_export_docx / bg_state）
- 输入：`<projectDir>/sources/`；输出：`<projectDir>/extraction/`、`<projectDir>/exports/`
- 运行时绝对路径见文末 Host 注入块。

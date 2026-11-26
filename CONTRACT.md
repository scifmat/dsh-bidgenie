# BidGenie 插件 M1 接口契约（冻结稿 v1）

> 所有实现（Host / Client / Python）以本文件为准。改契约先改本文件并通知 Lead。
> 依据：PRD v1.0.2 第 6.6/7.7/8.5 节、开发文档第 3 章面板设计、决策 Q1–Q5。

## 1. 包与目录布局

```text
plugin/                              # 正式插件包根（M1 起）
├─ package.json                      # name: dsh-bidgenie; dsh.bundle.patch + dsh.client
├─ cordis.patch.yml                  # insert id: bidgenie, name: 'dsh-bidgenie'
├─ index.js                          # apply(ctx)：注册路由/工具/技能；import lib/*
├─ lib/
│  ├─ services.js                    # 模块函数（resolveWorkspace / bidder / script / settings / projects / envCheck / health）
│  ├─ routes.js                      # /bidgenie/* 路由（同一 handler 内分 method）
│  ├─ tools.js                       # bidgenie_paths / bidgenie_bidder（M1 至少 paths）
│  └─ python.js                      # 解析 bundled python、spawn 脚本、结构化返回
├─ client/
│  └─ client.js                      # 左栏面板（3 Tab：项目/投标人/设置）
├─ libexec/
│  ├─ _lib/{jsonio,schema,paths,logging,errors}.py
│  ├─ bg_workspace.py                # init：创建 dsh/ 目录树 + registry（Q1），不触发 Agent
│  └─ bg_bidder.py                   # 投标人 CRUD（create/update/delete/set-current/list/validate）
└─ tests/m1/                         # python 单测/自检脚本与 fixtures
```

## 2. 工作区解析（M1-00 结论前先用兜底方案）

- 面板在顶部显示「工作区」选择器；数据来自 `GET /bidgenie/workspaces`（Host 读 `workspaceRegistry.list()`）。
- 除 `GET /bidgenie/about`（插件级文案，不依赖工作区）外，所有 `/bidgenie/*` 请求必须带 `workspace`（GET 用 query，写请求放 body.workspace；绝对路径）。
- Host 用 `workspaceRegistry.resolveByPath(path)` 校验；未注册 → `E_NOT_FOUND`。不读 Host cwd/env。
- **M1-00 已验证（2026-11-08）**：`webServer.register` 的 `kind:'prefix'` 可用且按路径边界匹配；同一 handler 多 method 可用；框架不自动 404/405，需自行兜底；重复 (kind,path) 抛错；exact 优先于最长 prefix。`workspaceRegistry.list()/resolveByPath()` 可用；`useWorkspaces` 仍未验证。
- M1 采用：面板顶部工作区选择器 + `GET /bidgenie/workspaces` + localStorage；不依赖 `useWorkspaces`。

## 3. HTTP 路由（M1 必做）

统一响应 `{ok:true,...}` / `{ok:false,error,message,hint?}`；写请求要求同源 + `x-bidgenie-csrf: 1`；路径用 `kind:'prefix'` 自行解析 `:id`（M1-00 验证 prefix；若不可用则改用 query 参数 `?bidderId=`）。

| 方法 + 路径 | 请求 | 响应 |
|---|---|---|
| `GET /bidgenie/health` | `?workspace=` | `{ok,version,hostVersion,workspaceRoot?}` |
| `GET /bidgenie/about` | — | `{ok, about:{name,nameZh,tagline,sections[],footer}, version, source, warning?}`（读 `config/about.json`，不依赖工作区） |
| `GET /bidgenie/workspaces` | — | `{ok, workspaces:[{id,path,title}]}` |
| `POST /bidgenie/workspace/init` | body `{workspace}` | `{ok, created:[], registryPath}`（Q1；只建 dsh/ 目录树，不触发 Agent） |
| `GET /bidgenie/bidders` | `?workspace=` | `{ok,bidders:[{bidderId,displayName,bidderType,updatedAt,isCurrent,completeness}]}` |
| `POST /bidgenie/bidders` | body `{workspace, bidder, certificates?, performance?, awardsPenalties?}` | `{ok,bidderId,dirName}` |
| `PUT /bidgenie/bidders/:id` | body `{workspace, patch, certificates?, performance?, awardsPenalties?}` | `{ok,updatedAt}` |
| `DELETE /bidgenie/bidders/:id` | body `{workspace, confirm:true}` | `{ok,trashedTo}` |
| `POST /bidgenie/bidders/:id/current` | body `{workspace}` | `{ok,currentBidderId}` |
| `GET /bidgenie/settings` | `?workspace=` | `{ok,settings}` |
| `PUT /bidgenie/settings` | body `{workspace, settings}` | `{ok,updatedAt,changedKeys,skipped?}` |
| `GET /bidgenie/projects` | `?workspace=` | `{ok,projects:[{projectId,dirName,projectName,currentStage,currentPackage,updatedAt}]}` |
| `GET /bidgenie/env-check` | `?workspace=` | M1 返回 `{ok, status:'not-run', checklistVersion:null, items:[], requiredPassed:0, requiredTotal:8}`（M2 接真实脚本） |

错误码沿用 PRD 8.5（`E_SCHEMA_INVALID` / `E_NOT_FOUND` / `E_LOCK_TIMEOUT` / `E_USER_DECISION_REQUIRED` / `E_DEP_MISSING`）；M1 补通用 `E_IO`(5) 与 `E_INTERNAL`(500，宿主未分类异常，非业务码)。

## 4. Python 脚本 CLI（M1）

```text
python bg_workspace.py init --workspace <abs> [--json]
python bg_bidder.py list    --workspace <abs> [--json]
python bg_bidder.py create  --workspace <abs> --data <json-file> [--json]
python bg_bidder.py update  --workspace <abs> --bidder <id> --data <json-file> [--json]
python bg_bidder.py delete  --workspace <abs> --bidder <id> --confirm [--json]   # 软删除→.trash/
python bg_bidder.py set-current --workspace <abs> --bidder <id> [--json]
python bg_bidder.py validate --workspace <abs> [--bidder <id>] [--json]
```

- `--data` 文件结构：`{bidder:{...7.7.1}, certificates?:{items:[]}, performance?:{items:[]}, awardsPenalties?:{awards:[],penalties:[]}}`。
- 输出 `{ok:true,...}` 或 `{ok:false,error,message,hint}`；退出码 0/2/3/4/5/7。
- 命名：`dsh/bidders/<slug>-<ts14>-<rand4>/`；文件 `bidder.json`/`certificates.json`/`performance.json`/`awards-penalties.json`/`profile.md`/`meta.json`/`attachments/`。
- 写入：原子写 + 文件锁（`_lib/jsonio.py`）；更新 `dsh/registry.json` 与 `dsh/config/user-settings.json`；追加事件 `dsh/logs/events-<YYYYMMDD>.jsonl`；敏感字段默认脱敏只在 API 返回层做，文件中保留原值。事件为 UTF-8（无 BOM）JSONL；PowerShell 读取请用 `Get-Content -Encoding utf8`，不要依赖控制台默认编码。异常事件 detail 只存摘要/报告路径（完整清单在 `extraction/common/_extraction-report.json` 等落盘文件），避免 script.error 单条过大。
- 幂等：`create` 以 bidderId 去重；重复 `init` 不重建；删除为软删除（`.trash/<dir>-<ts>/`）。

## 5. 投标人数据（M1 最小字段）

- `bidder.json`：`schemaVersion:"1.0"`、`bidderId`、`bidderType`（legal_entity/natural_person）、`displayName`（必填）、`legalName`、`unifiedSocialCreditCode`、`legalRepresentative`、`registeredAddress`、`officeAddress`、`contact{name,title,phone,email}`、`bank{accountName,bankName,accountNo}`、`capital`、`establishedDate`、`businessScope`、`writable`、`sensitiveFields`、`tags`、`updatedAt`。
- `certificates.json`：`{items:[{certificateId,name,category,issuer,certificateNo,level,validFrom,validTo,status,holder,requiredFor,attachmentRef,notes}]}`
- `performance.json`：`{items:[{performanceId,projectName,clientName,contractAmount,period:{start,end},role,scope,attachmentRef,notes}]}`
- `awards-penalties.json`：`{awards:[{awardId,name,issuer,awardedAt,level,description,attachmentRef}], penalties:[{penaltyId,type,description,authority,occurredAt,status,attachmentRef}]}`
- `registry.json`：`{schemaVersion, currentBidderId, bidders:[{bidderId,dirName,displayName,bidderType,updatedAt}],projects:[],updatedAt}`（`currentBidderId` 为 M1 新增镜像字段，与 user-settings 同步）
- `user-settings.json`：至少 `{schemaVersion,currentBidderId,updatedAt}`；M1 保留其他 S1–S15 默认值。

## 6. 面板（M1）

- 3 Tab：项目 / 投标人 / 设置；顶部工作区选择器 + 刷新；默认项目页。
- 项目页 M1：空状态（无项目/未初始化两种）+ 项目列表只读（若已有项目数据）；未初始化时主导航「创建数据目录」调 `POST /bidgenie/workspace/init`（Q1），另有「复制启动指令」。
- 投标人页：列表（当前置顶、完整度、四组计数）、新建、详情/编辑（四组；M1 至少基本信息+证书列表可编辑，业绩/奖惩可 JSON 文本域或行编辑）、设为当前、软删除二次确认。
- 设置页：S1–S15 呈现（可按设计稿分组；保存即时或按钮保存均可，但需四态反馈）。
- 环境自检页：M1 显示"未运行"与说明；重跑按钮可先调 stub 路由（M2 接真脚本）。
- 严格无上传/无启动按钮/不注入会话（决策 9.19）；无技术词泄露；全部颜色用主题 token。
- **Client bundle 更新需重启 DSH 生效**（页面内普通刷新不可靠，M1 实测：旧 client 模块会留在页面内存，左栏仍显示旧面板）。
- Client 文件为纯 ESM 动态模块：`window.__ModuleLoader__.load({id:'dsh-bidgenie', factory(require){...return {inject:['slots'],apply(ctx){...}}}})`；注册 `sidebar.panellist`(id=bidgenie,order=30,label='标代精灵') 与 `main`(key=bidgenie)。

## 7. 安装与验证

```text
plugin_manager install_bundle target=D:\ProJects\bidgenie-flow\plugin
plugin_manager set_plugin target=include:bidgenie enabled=false/true   # 重载
curl "http://127.0.0.1:60715/bidgenie/health?workspace=<ws>"
```

M1 验收：PRD/开发文档 5.2 验收表 + 第 3 章 15 条面板验收清单 + `docs/acceptance/M1/` 证据。

---

## 8. M2 追加契约（2026-11-08，M2 实施期冻结）

### 8.1 Skills（M2-00）

```text
plugin/skills/bidgenie/SKILL.md        # 主技能：路由 + 硬约束（M2 最小版：F2 初始化、F3 提取入口）
plugin/skills/bg-bidder/SKILL.md       # 投标人资料（F1 Agent 侧）
plugin/skills/bg-init/SKILL.md         # 项目初始化（F2）
```
- frontmatter 仅 `name`（kebab-case）/`description`（含触发线索）；正文短，references 按需。
- Host 在 `apply()` 中扫描 `plugin/skills/*/SKILL.md`，解析 frontmatter 后 `ctx.get('skills').register({name,description,content})`；content 内注入绝对路径 `<pluginRoot>/skills/<name>/references/`（M0 已验证）。
- 仅注册 M2 三个技能；其余 bg-* 随里程碑补齐。

### 8.2 环境自检（M2）

- 基线：`plugin/libexec/checklist.json`（16 项、必需 8，按 PRD 附录 D；含 `itemVersion/remediation/degradeImpact`）。
- CLI：`bg_env_check.py --report --workspace <ws> [--json]`（只读，返回当前状态）；`--apply`（逐项检查、写 `dsh/config/env-check.json`、返回报告）；`--selftest`。
- `env-check.json`：`{schemaVersion,checklistVersion,hostVersion,checkedAt,items:{id:{itemVersion,required,status,evidence,checkedAt,detail}},waivers,obsoleteItems,degraded}`。
- `GET /bidgenie/env-check?workspace=` → `{ok,status:'not-run'|'ready',checklistVersion,items:[{id,label,required,status,evidence,remediation,degraded}],requiredPassed,requiredTotal}`。
- `POST /bidgenie/env-check/run` body `{workspace,itemIds?}` → 调 `--apply`（超时 10 分钟；不自动安装依赖），返回同上 + `checkedAt`。
- 版本化跳过：`checklistVersion`+`itemVersion`+`status=passed` → 跳过；结果可重复执行。

### 8.3 项目初始化与状态机（M2）

- `bg_workspace.py init-project --workspace <ws> [--bidder <bidderId>] [--project-name <name>] [--json]`：
  - 解析当前投标人（缺省读 `user-settings.currentBidderId`）；创建 `<ws>/<bidderSlug>-<ts14>-<rand4>/`；
  - `project.json`：键名齐全、值空（projectNo/projectName/isSubcontracted/packageCount/packagesToWrite/bidderRef/sourceFiles/keyInfoExport/workspaceRootFingerprint/createdAt/updatedAt/extensions）；
  - `state.json`：`{schemaVersion,projectId,currentScope:'project',currentPackage:null,currentStage:'project_init',completedStages:[],pendingInput:null,abort:null,locks:{draftLocked:false},reviewLoop:{round:0,maxRounds:5,closedRound:null},envCheckSummary:null,extensions:{},history:[],revision:1,createdAt,updatedAt,updatedBy}`；
  - 更新 `dsh/registry.json.projects[]`；追加事件 `project.initialized`；幂等（同 workspace 下已有同名 projectName 未完成时提示）。
- `bg_state.py`：`status|check|transition|set-package|abort|resume|projects|doctor`（参数见 PRD 4.4）；M2 至少实现 T1（project_init→info_extract）与 T2 的 guard/effects（后续里程碑按 PRD 4.3 全量）；非法迁移返回 `E_ILLEGAL_TRANSITION` + allowedNext。`projects` 列出工作区项目（status 的入口）；`doctor` 为只读续跑诊断（阶段/待做章节/stale chapter-result/open issues/建议命令）。
- Host 路由：`GET /bidgenie/projects` 改为读 registry 真实数据；`GET /bidgenie/projects/state?workspace=&project=` 返回 state 摘要；新增 `POST /bidgenie/projects/init`（body workspace, bidderId?, projectName?）。
- M2 面板：项目列表可显示刚创建的项目（只读）；环境自检页接真数据（Q2：面板可直接重跑，单项 120s/整体 10min）。

### 8.4 M2 验收要点

1. 技能 catalog 出现 `bidgenie`/`bg-bidder`/`bg-init`，`skill` 工具可加载正文；2. SP-3：用户在对话框输入 `[BidGenie] ...` 或自然语言，Agent 加载主技能；3. 环境检查 16 项/必需 8、二次运行跳过已通过项、生成 env-check.json；4. 项目初始化生成 project.json/state.json/registry 记录；5. state status/check/transition T1 正确、非法迁移被拒；6. 面板环境自检页真数据+重跑（重启后验证）；7. M2 开发日志与验收记录。


---

## 9. 导出样式配置（v2，2026-11-09：两套官方样式隔离）

### 9.1 设置字段

`dsh/config/user-settings.json`（`styleProfile` 仅旧数据兼容，不再写入）：

```json
"styleProfiles": { "tech": "official-tech", "keyInfo": "official-keyinfo" }
```

- `tech`：技术方案（F7 正文导出）专用；`keyInfo`：关键信息文档（F3.2 导出）专用。
- 两套样式**互不共用**；默认值分别为 `official-tech` / `official-keyinfo`。
- 旧值迁移（读取时内存映射）：`official-default` → tech=`official-tech`、keyInfo=`official-keyinfo`；`keyinfo` → keyInfo=`official-keyinfo`。

### 9.2 官方样式（随包，隔离目录）

```text
plugin/templates/styles/tech/official-tech.json         # 官方-技术方案导出样式
plugin/templates/styles/keyinfo/official-keyinfo.json   # 官方-关键信息导出样式
```

结构：`{schemaVersion:"1.0", id, name, label, docType:"tech"|"keyInfo", description, basedOn:null, params:{fontFamily, fontSizePt, lineSpacing, pageMarginMm, headingNumbering, captionPrefix...}, createdAt, updatedAt}`
后续新增官方样式只加文件，不改代码。

### 9.3 用户自定义样式（隔离目录）

```text
<workspace>/dsh/styles/tech/<styleId>.json
<workspace>/dsh/styles/keyinfo/<styleId>.json
```

- 只扫描对应子目录；`docType` 可省略（由目录推断），若填写必须与目录一致，否则进 warnings。
- 兼容旧版扁平文件 `<workspace>/dsh/styles/*.json`：按 docType 归类并给 warning「建议移动到 tech/keyinfo 子目录」。
- 文件必须为合法 JSON；缺失 id/name/schemaVersion 或非法 JSON → warnings（含文件名+原因），不阻塞。
- `bg_workspace.py init` 幂等创建 `dsh/styles/tech/`、`dsh/styles/keyinfo/`。

### 9.4 接口

`GET /bidgenie/styles?workspace=` → `{ok, builtin:{tech:[{id,label}], keyInfo:[{id,label}]}, custom:{tech:[{id,label,path}], keyInfo:[...]}, warnings:[{file,message}]}`
`PUT /bidgenie/settings`：**部分合并**——只应用请求里出现的键，未出现的键保持原值；无实际变化时 `skipped=true` 且不写盘。校验 `styleProfiles.tech` 属于 tech 可用集合、`styleProfiles.keyInfo` 属于 keyInfo 可用集合，否则 400 `E_SCHEMA_INVALID` + 对应可选值。旧 `styleProfile` 写入继续兼容（镜像到 styleProfiles.tech）。每次实际写入追加 `settings.updated` 事件（`actor=user`、`source=panel`、`detail.changedKeys`、`detail.changed{key:{before,after}}`），用于追溯字数档/配额等设置变化。
## 10. M3 转换与提取契约（2026-11-09）

### 10.1 `bg_convert.py`

```text
python bg_convert.py --workspace <ws> --project <projectDir> --files <abs>[,<abs>...] [--json]
```

- 输入：`.docx`（python-docx 解析标题/段落/表格→md）、`.doc`（Word COM 转 docx 再解析；无 Word/COM → `E_DEP_MISSING` 降级提示）、`.pdf`（pymupdf4llm 转 md）、`.md`/`.txt`（复制/直转）；其他格式 → `E_USAGE`。
- 输出：`<project>/sources/converted/<原文件名>.md`；`<project>/sources/conversion-manifest.json`：
  `{schemaVersion, generatedAt, converter, files:[{sourcePath, sourceName, sourceType, sizeBytes, convertedPath, mdChars, headings, tables, warnings, status}], sources:[...同 files 数据]}`, `sources[]` 为 PRD 7.6/T2 guard 兼容别名
- 依赖：`plugin/pylibs`（`_lib/deps.ensure_dependency_path()`）；不允许联网/改源文件。
- 幂等：同一源文件重复转换覆盖 md、manifest 去重更新；失败文件不写半成品（临时文件+替换）。
- **分类与固定命名由 Agent（bg-extract）完成**：`sources/converted/*.md` 是原始转换结果；Agent 按 PRD F3.2 判定主招标文件/技术要求/采购清单/评审标准/施工说明/方案建议/其他，复制（不修改原文件）为 `sources/01-bidding-documents.md`、`02-technical-requirements.md`、`03-procurement-list.md`、`04-evaluation-criteria.md`、`05-construction-instructions.md`、`06-proposed-plan.md`、`07-<内容描述>.md`…；原件保留在 `sources/raw/`（脚本不自动分类，避免误判）。

### 10.2 提取模板（v2，2026-11-10：Traecode 模板）

`plugin/templates/extraction/`：9 个模板直接采用 Traecode 模板（保留「提取指引 / 提取清单 / 输出示例」），并已适配：示例标题去硬编码编号、表头人性化、公开联系信息不脱敏、07 评分表三列+分值合计。

- `_template-manifest.json`（v2）：`{schemaVersion:"2.0", templateVersion:"3.0.0", templates:[{id, docClass, fileName, title, path, expectedSections:[...]}]}`；`expectedSections` 为输出示例中 `##` 章节标题（09 允许空数组）；v3 起 05/08 增「拟派人员要求/业绩与车辆要求/材料要求」等章节，人员/业绩/车辆只作要求项、不记个人明细。
- 提取稿格式（人读）：`# <title>` + `## <expectedSections>` + 人读表格/段落；禁止 frontmatter 字段表、`字段键/字段值/字段名/证据` 机器表头、英文机器键当展示名、硬编码标题编号。
- 公开信息（采购单位/代理机构/联系人/公开电话/邮箱/地址/开标地点）必须提取且不脱敏；证件号/银行账号/个人隐私不写。

### 10.3 `bg_extract_validate.py`（v2 结构校验）

```text
python bg_extract_validate.py --workspace <ws> --project <projectDir> [--json]
```

- 校验：9 文件存在、每个文件 H1 唯一且等于 manifest.title、`expectedSections` 齐全、禁止机器表头与 frontmatter 字段表、07 评分表三列+`## 分值合计`（合计与声明满分 ±0.5）、正文/表格非空；包数一致。
- 输出 `_extraction-report.json`（files 含 expected/found/missingSections/blockers/warnings）；blocker→exit4 `E_VALIDATE_FAILED`，warning→0。

### 10.4 关键信息导出（`bg_export_docx.py --mode keyinfo`，v2）

```text
python bg_export_docx.py --workspace <ws> --project <projectDir> --mode keyinfo --out <path> [--json]
```

- 合并 9 个 extraction md（01–05、06–09）；剥离源标题自带编号，H1–H6 使用 **Word 原生多级自动编号** 绑定 Heading 样式。
- 样式（读 `templates/styles/keyinfo/official-keyinfo.json`）：标题黑体（H1 16 / H2 14 / H3 12pt）、正文宋体 12pt（小四）、1.5 倍行距、首行缩进 2 字符；表头加粗+灰底；**无封面、无目录、无页眉页脚**；标题行（项目名/编号/日期）作为正文段落。
- 公开联系信息不脱敏（仅屏蔽身份证/银行卡等隐私）；原子落盘 + 回填 `project.json.keyInfoExport`。
### 10.5 M3 验收要点

1. `.docx`/`.pdf`/`.doc` 转换与 manifest；2. 9 个模板 + manifest；3. 样本提取 validate（含 blocker 负例）；4. keyinfo docx 可打开、无封面/目录、题注/表格保留；5. `templates_integrity` 环境项转 passed；6. 独立提取模式（extractOnly）登记，M3 只做标记不迁移 T2。


---

## 11. 配置分层与默认值优先级（2026-11-14）

- `dsh/config/user-settings.json`：工作区级**新项目默认偏好**（字数档/默认图表配额/交互模式/默认样式等）；随时可改，只影响之后新建或尚未准备的包。
- `package-meta.json`：**本包决策快照**（F4 写定；含 8 项答案 + `valueSource` + 导出样式等）；已准备/已推进的包以它为准，设置页变更不回溯。
- F4 解析优先级：**本次用户回答（user） > 提取事实（extracted） > 设置默认（agent-default） > 内置默认**；结果与来源写入 package-meta。
- 要修改已准备包：显式走 `bg_prepare.py --update-answers` / 重做准备（并备份），不自动同步设置。
- 材料/业务要求明细不复制进 package-meta；需要时用 `bg_prepare.py materials` 按需输出或独立 `materials-index.json`。
- `userInputTimeoutSec` 已废弃，不再读写；`extensions.allowNetworkImages` 不再写入 package-meta（联网配图状态以 agent 调用时参数/设置派生，降级原因写入 assumptions 即可）。


---

## 12. 大纲存储与 F5/F6 输入范围（2026-11-14）

- 大纲文件统一放 `<packageDir>/chapters/_outline/`：`outline.json`、`outline.draft.json`、`outline.json.bak-<ts>`、锁文件；`_outline/` 是元数据目录，不参与章节树校验。
- F5-1 大纲输入：`extraction/common/01-Basic-Information.md`、`extraction/packages/package-<N>/06-Procurement-Content.md`、`07-Evaluation-Criteria.md`、`09-Technical-Requirements.md` + `package-meta.json` 用户回答；不读 05/08 等商务/资格全文。
- F5-2 正文输入：本章 `contentRequirements`/`criterionRefs`/`figurePlan`、`package-meta.json` 用户回答、01/06/07/09 中与本章相关片段；不读商务/资格类全文，减少上下文。
- 以上输入范围是硬约束；技能/脚本不得自行扩大读取范围。


---

## 13. 去 AI 味共享规则（2026-11-14）

- 辅助技能 `bg-writing-style`：只提供规则，不写文件、不改状态；由 `bg-write` 写作子智能体加载，M7 `bg-review`/`bg-optimize` 复用同一技能，不复制规则。
- 规则要点：禁用 AI 高频词/套话/排比堆砌；开头直接进入内容，结尾自然收束，不总结/不升华/不固定句式；允许总-分但不模板化。
- **禁止自引用**：不得写「详见第 X 章/第 X 节」「见上图/如下表」「前文所述/后文将述」等；结构调整后引用会失效。
- 审查阶段只做语义判断，不新增脚本硬校验。


---

## 14. 官方导出样式（颜色/字体，2026-11-14）

- 三套官方样式：`official-tech`（技术方案）、`official-keyinfo`（关键信息）、`official-overview`（工作概览）。
- 颜色：所有文字（正文/标题/题注/表格/编号）使用 Word **自动**色（`w:color val="auto"`），不得写主题蓝/固定黑色。
- 字体（仅 Windows 常规自带）：标题 中文黑体 + 西文 Arial；正文 中文宋体 + 西文 Times New Roman；题注/表格 中文宋体 + 西文 Times New Roman；字号沿用各 profile。
- `bg_export_docx.py` 统一应用 `rFonts` 的 ascii/hAnsi/eastAsia/cs；`styleProfiles` 增加 overview，默认 `official-overview`。


---

## 15. 多包撰写范围与网络配图移除（2026-11-14）

- 包确认只问一次：`「本项目分 N 包，您需要撰写哪些包的技术方案？」`，支持多选/文本「2、4」；写入 `project.json.writingPackages`/`packageSelection`，事件 `package.confirmed`（actor=user）。
- 之后逐包串行：每包 F4→F7 完成并导出后 T13 切下一包；全部完成后 completed 终态，不重复询问。
- F4 只 prepare `writingPackages` 中的包；未选包拒绝；`packagesToWrite` 为所选子集。
- **网络配图功能移除**：`defaultFigureQuota.network=0`、`allowNetworkImages=false`；F4 问题 8→7；outline 不再规划 network；render/review/export/overview 不再要求 network；旧数据只读兼容。

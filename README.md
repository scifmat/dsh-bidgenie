<p align="center">
  <img src="assets/brand/readme-cover.webp" alt="BidGenie 标代精灵：DSH 投标技术方案自动化插件" width="100%">
</p>

<h1 align="center">BidGenie 标代精灵 </h1>

<p align="center"><strong>把复杂的技术方标留给我：解析、大纲、正文到定稿，步步为营</strong></p>


<p align="center">
  <a href="https://github.com/scifmat/dsh-bidgenie/blob/main/package.json"><img src="https://img.shields.io/github/package-json/v/scifmat/dsh-bidgenie?label=version&color=EC661A&style=flat-square" alt="version"></a>
  <img src="https://img.shields.io/badge/platform-Windows%2010%2B-0B1220?style=flat-square" alt="Windows 10+">
  <img src="https://img.shields.io/badge/host-DSH%200.2.x-4D6BFE?style=flat-square" alt="DSH 0.2.x">
  <img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-8A9AB8?style=flat-square" alt="PolyForm Noncommercial">
</p>

[BidGenie](https://github.com/scifmat/dsh-bidgenie) 是专为 [DSH（DeepSeek Harness）](https://github.com/deepseek-ai/deepseek-harness)打造的投标技术方案撰写插件。作为投标专员，我们深知写标书的痛苦，因此它被设计成一个**具备高智能、工程化、防漏项、可随时中断并恢复**的技术标撰写智能体：

- 📄 **不止于解析**：从 `.pdf` / `.docx` 中精准提取 9 类关键信息，按技术评审因素生成大纲。
- ✍️ **懂排版的写手**：按包生成大纲、正文，自动规划并生成表格、流程图。
- 🛡️ **严苛的质检员**：写完自动审查（评分覆盖、图文核对），揪出 blocker 级问题。
- 🔄 **随时打断，随时继续**：关掉电脑，明天回来说一句「继续这个项目」就能接着走。

> **内测版（Alpha）**：当前版本由 `package.json.version` 单一来源驱动；Alpha / Beta / RC 由版本后缀自动识别，面板与 README 徽章同步显示。

## 功能一览

- 📇 **投标人**：管理公司 / 自然人档案、资质证书、业绩、奖惩等信息。
- 📄 **文件解析**：支持 `.pdf`、`.docx`、`.doc`、`.md`，统一转成 Markdown；`.doc` 需要 Word 或 LibreOffice。
- 🔎 **信息提取**：按 9 类模板生成可直接阅读的提取稿（公共 5 类 + 每个包 4 类）。
- 🧩 **分包识别**：判断是否分包、分几个包；有歧义时让你裁决，不自行猜测。
- 📝 **撰写准备**：要写哪些包只问一次；再按包收字数、人员、设备、工期、表格 / 流程图 / 终端图配额。
- 🗂️ **大纲规划**：按技术评审因素生成章节树，校验评分覆盖率、层级、字数分配和图表规划。
- ✍️ **正文撰写**：每批 4–6 章交给一个子智能体；表格、mermaid图、终端模拟图按规划生成。
- 🔍 **审查优化**：脚本检查 → 评分覆盖核对 → 图文核对；问题分 blocker / major / minor，修完再复审（默认最多 5 轮）。
- 📦 **导出**：技术方案、工作概览默认导出；关键信息按需导出；导出后自动自检。
- 🎛️ **控制面板**：查看项目进度、管理投标人、跑环境检查、改设置。
- 🎨 **导出样式**：内置 tech / keyinfo / overview 三套官方样式，也可在工作区用 JSON 自定义字体、页边距、编号等。

**它不做什么**

- 不写商务标、报价、资格文件和合同分包内容，只处理技术方案范围。
- 不编造招标文件或投标人资料里没有的信息。
- 不自动提交投标，也不替代人工复核。

## 使用流程

```text
投标人 → 初始化 → 提取招标文件 → 撰写准备 → 大纲 → 正文 → 审查/优化 → 定稿 → 导出 Word
```

| 阶段 | 你可以这样说 | 主要产出 |
|---|---|---|
| 投标人 | 「帮我建一个投标人」 | `dsh/bidders/<投标人>/` 下的资料 |
| 初始化 | 「开始这个项目」 | `project.json`、`state.json` |
| 提取 | 「继续提取关键信息」 | `extraction/` 下 9 份提取稿 |
| 撰写准备 | 「准备写方案」 | 包目录、`package-meta.json` |
| 大纲 | 「继续生成大纲」 | `outline.json` 与章节文件树 |
| 正文 | 「继续写正文」 | `chapters/**/*.md`、图表 PNG |
| 审查/优化 | 「继续审查」/「继续优化」 | `reviews/issues.json`、`review-report.md` |
| 定稿 | 「可以定稿」 | 锁定当前包内容 |
| 导出 | 「导出 Word」 | `export/` 与 `exports/` 下的 DOCX |

多包项目只需在撰写准备阶段选一次包，之后按包串行走完；上一包完成后再进入下一包，不会重复问你要写哪些包。

## 运行环境

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | **Windows 10 或更高** | 当前只在 Windows 上验证，未支持 Linux / macOS |
| 宿主 | **DSH Desktop / Web，0.2.x 基线** | 版本兼容项会读取宿主版本；不匹配时提示「兼容性未验证」 |
| Python | 3.10+ | 优先使用 DSH 自带运行时（含安装目录内的运行时载荷），无需单独安装 |
| 磁盘 | 剩余 ≥ 500 MB | 环境检查会确认 |
| 可选 | Chrome / Edge、Word 或 LibreOffice、Windows 中文字体 | 分别用于流程图渲染、`.doc` 转换、Word 导出字体 |

PDF、Word、图像相关依赖不再随仓库分发（`pylibs/` 已从仓库移除，避免仓库体积过大），首次安装后需运行一次依赖修复工具（见下节）；缺失项会在环境检查里指出。首次使用前建议先跑一遍**环境检查**（左栏「标代精灵」→ 环境检查），也可让Agent帮忙初始化工作区。

> **全新电脑不需要预装 Python / Node.js**：DSH Desktop 安装包已自带 Node.js、Python 与 pnpm 运行时（安装目录下的 `resources\runtime\primary-runtime`），插件会直接使用。只有插件自有依赖（PDF 组件、pywin32，约 100 MB）不随安装包分发，首次在环境检查里点「修复环境」联网安装即可。

环境检查发现可修项时，页面会显示 **「修复环境」** 按钮：先给出自动/需确认/只能手动/需升级插件的分组计划，确认后只把锁定版本的依赖装到用户数据目录，并自动复检；手动项会给出可复制指引，不会自动安装系统软件或提权。

## 安装

### 方式 A：DSH 插件面板（推荐）

打开 **Plugins / 插件 → Add plugin / 添加插件**，输入仓库地址：

```text
https://github.com/scifmat/dsh-bidgenie
```

安装后重启 DSH（或按面板提示加载），再打开一个工作区。

### 方式 B：让 Agent 从 GitHub 安装

在 DSH 会话里让 Agent 调用 `plugin_manager`：

```text
action = install_bundle
target = "https://github.com/scifmat/dsh-bidgenie"
```

### 方式 C：本地目录安装

```powershell
git clone https://github.com/scifmat/dsh-bidgenie.git
```

然后在插件面板选择这个本地目录。**本地目录通常是链接安装，装好后不要删除或移动该目录。**

> 不建议只用 `dsh plugin --profile <profile> add <pkg>` 安装：它会把包加入 profile 的依赖，但不会完成 bundle 注册和选中。请优先使用面板或 `plugin_manager install_bundle`。

### 首次安装依赖（重要：pylibs 不在仓库）

本仓库**不包含** Python 依赖（`pylibs/` 已从历史移除）。首次安装或更新插件后，先运行一次修复工具把依赖装到本机：

```powershell
# 在插件根目录执行（Windows）
tools\repair-env.cmd -DryRun     # 先看计划，不做改动
tools\repair-env.cmd             # 安装依赖（约 100+ MB，首次较慢）
```

也可以直接调用 PowerShell 脚本：

```powershell
powershell -ExecutionPolicy Bypass -File tools\repair-bidgenie-pylibs.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File tools\repair-bidgenie-pylibs.ps1
```

**国内源与换源**（默认走清华镜像）：

```powershell
tools\repair-env.cmd -Mirror https://mirrors.aliyun.com/pypi/simple   # 换其他镜像
tools\repair-env.cmd -Mirror http://pypi.internal/simple               # http 源自动加 --trusted-host
tools\repair-env.cmd -NoMirror                                         # 不走镜像，用 pip 默认源
tools\repair-env.cmd -NoOfficialFallback                               # 镜像失败时禁止回退官方源
$env:BIDGENIE_PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple" # 环境变量指定镜像
```

- **Python 选择**：`-Python <路径>` → `BIDGENIE_PYTHON` / `DSH_PYTHON` → **DSH 自带运行时**（优先，避免装到系统 Python）→ PATH 里的 `python` → 本地 Python 3.10+。
- **镜像与回退**：默认清华 `https://pypi.tuna.tsinghua.edu.cn/simple`；pip 自动加 `--retries 3 --timeout 60`，http 源自动加 `--trusted-host`；镜像失败会打印 warning 并自动回退官方 `https://pypi.org/simple`（`-NoOfficialFallback` 可关闭）。
- **失败提示**：若所有源都失败，按提示换源重试（`-Mirror` / `-NoMirror` / `BIDGENIE_PIP_INDEX_URL`）或检查网络/代理。
- 依赖安装到 `%USERPROFILE%\.dsh\data\bidgenie\pylibs`（设置 `DSH_HOME` 时用 `%DSH_HOME%\data\bidgenie\pylibs`），并在插件目录建立 `pylibs` 链接；升级/重装插件后重复运行即可，不用重新下载。
- 安装完成后在面板跑一次「环境检查」；`-Rollback` 可回滚到最近一次安装前的版本，`-Force` 可强制重装。
- 本地开发目录可保留自己的 `plugin/pylibs`，不会被本工具删除（会先移动到 `data/bidgenie/plugin-pylibs.legacy-*`）。

## 快速开始

1. 在 DSH 里打开（或新建）一个工作区。BidGenie 的数据会写到这个工作区的 `dsh/` 下。
2. 左栏打开「标代精灵」→ **投标人** → 新建一份公司或个人资料，并设为**当前投标人**。
3. 打开 **环境检查**，确认必需项通过。首次检查会久一点。
4. 在对话框上传招标文件，说「**帮我写技术方案**」。
5. 按助手提示确认分包、要写的包、字数和人员 / 设备 / 工期。中途可以随时停下，下次说「继续这个项目」。
6. 内容确认后说「**可以定稿**」，定稿后说「**导出 Word**」。到项目目录的 `packages/package-<N>-<时间戳>/export/` 取文件。

## 产物与数据位置

所有文件都在你自己的 DSH 工作区里，安全高效。

```text
<工作区>/
├── dsh/                              # 插件数据
│   ├── config/                       # user-settings.json、env-check.json
│   ├── bidders/<投标人>/             # 投标人资料、证书、业绩、奖惩
│   ├── styles/                       # 自定义导出样式
│   ├── run-manifests/                # 每次运行的记录
│   ├── backups/  logs/  locks/  tmp/
│   └── registry.json
└── <项目目录>/
    ├── project.json  state.json
    ├── sources/                      # 原始文件、转换后的 Markdown、转换清单
    ├── extraction/common/            # 公共提取稿 01–05
    ├── extraction/packages/package-N/ # 包提取稿 06–09
    ├── packages/package-N-<时间戳>/
    │   ├── package-meta.json
    │   ├── chapters/                 # 大纲与正文
    │   ├── reviews/                  # 审查问题与报告
    │   ├── resources/                # 流程图、终端图源文件与 PNG
    │   └── export/                   # 技术方案、工作概览 DOCX
    └── exports/                      # 按需导出的关键信息 DOCX
```

| 产物 | 位置 |
|---|---|
| 技术方案 DOCX | `<项目>/packages/package-N-<时间戳>/export/` |
| 工作概览 MD + DOCX | 同上 |
| 关键信息 DOCX | `<项目>/exports/` |
| 提取稿 | `<项目>/extraction/` |
| 投标人资料 | `<工作区>/dsh/bidders/` |

导出的 Word **不含封面和目录**；技术方案与关键信息文档使用 Word 自动标题编号。如招标文件要求封面 / 目录，请在 Word 里手动添加。

## 仓库结构

```text
.
├── index.js              # Host 入口：注册工具、面板路由、运行时技能
├── cordis.patch.yml      # bundle 装载声明
├── package.json          # 插件元数据、导出入口、dsh.client 配置
├── client/
│   └── client.js         # 左栏面板（Web）
├── lib/                  # Host 侧：路由、模型工具、Python 桥、业务服务
├── libexec/              # 确定性 Python 脚本（转换、校验、渲染、导出）
├── config/               # 关于页等内容配置（about.json，改完刷新面板生效）
├── skills/               # 11 个运行时技能
├── templates/            # 9 类提取模板 + 3 套官方导出样式
├── assets/               # 品牌素材、README 封面、Mermaid 渲染器
├── tools/                # 依赖修复工具（repair-env，安装 pylibs）
├── LICENSE
└── README.md
```

`pylibs/` 不在仓库中：首次安装后由 `tools/repair-env.cmd` 安装到 `%USERPROFILE%\.dsh\data\bidgenie\pylibs`；开发目录本地的 `plugin/pylibs` 保留、不提交。

**开发与调试入口**

- 无需构建步骤：DSH 直接加载 `index.js`（Host）与 `client/client.js`（面板）。
- 关于页文案（简介、隐私、协议、团队联系方式）在 `config/about.json` 中维护，保存后刷新面板即可生效；联系方式会自动生成「复制」按钮。
- 所有 `libexec/bg_*.py` 支持 `--json`，可用 DSH 自带 Python 单独执行排障。例如单独跑一遍环境检查：

  ```powershell
  & "<pythonExe>" -X utf8 libexec\bg_env_check.py --apply --workspace "<工作区绝对路径>" --json
  ```

  这会重新跑一遍环境检查，并写入 `dsh/config/env-check.json`。

- Python 解释器按 `BIDGENIE_PYTHON` / `DSH_PYTHON` → DSH 运行时 → `PATH` 的顺序查找；宿主版本可用 `BIDGENIE_HOST_VERSION` 或 `DSH_HOST_VERSION` 覆盖自动探测。
- 业务状态只由脚本写入，不要手工修改 `project.json`、`state.json` 或 `registry.json`。

## 常见问题

**面板里为什么没有「开始撰写」按钮？**  
面板负责资料、设置和进度展示，撰写由对话框里的助手推进。复制面板给出的启动指令，粘贴到对话框发送即可。

**在全新电脑上点「创建数据目录」失败 / 提示缺少组件？**  
插件本身不需要你安装 Python：它会依次查找 `BIDGENIE_PYTHON`、DSH 运行时、DSH 安装目录里的运行时载荷、用户目录和 PATH，并实际执行一次版本探测。如果仍然失败，按面板提示设置 `BIDGENIE_PYTHON` 指向 `python.exe`，或运行 `tools\repair-env.cmd`。环境检查里的「修复环境」会自动安装插件自有依赖（PDF 组件、pywin32 等），只写入用户数据目录，不需要管理员权限。

**新电脑需要先装 Python 或 Node.js 吗？**  
不需要。DSH Desktop 自带 Node.js 与 Python 运行时，装好 DSH 和插件即可；插件自有依赖（PDF 组件、pywin32，约 100 MB）首次需要在环境检查里点「修复环境」联网下载。

**环境检查里的「插件脚本完整性」未通过，是不是插件坏了？**  
先看该行的失败原因。PDF 组件属于可选依赖；即使未安装，脚本自检也会跳过 PDF 环节，不再把它误报成插件缺陷。直接点「修复环境」把可选依赖补齐即可。

**支持扫描件 PDF 吗？**  
插件不含 OCR。纯图片扫描件可能提取不到文字，请先用其他工具转成可选中文字的 PDF 或 Word。

**`.doc` 转换失败怎么办？**  
安装 Word 或 LibreOffice 后重试，或者把文件另存为 `.docx` 再上传。

**流程图 / 终端图没有生成？**  
确认本机有 Chrome 或 Edge。若提示 `EPERM` 等权限错误，请把 DSH 的文件策略切到 `danger-full-access`，或授权当前工作区后重试。

**能写商务标、报价或资格文件吗？**  
不能。BidGenie 只处理技术方案范围；商务、报价、资格和合同分包内容会明确排除。

**投标人资料会被写进方案正文吗？**  
只有你登记过的证书和业绩才可能被引用。证件号、银行账号等默认打码，也不会进入正文和对话摘要。

**换电脑或换工作区后项目还在吗？**  
项目数据保存在原工作区，插件不会跨工作区同步。请继续在原工作区打开该项目；如果只是换电脑，把整个工作区目录一起拷贝过去即可。

**内测版遇到问题怎么反馈？**  
到 [Issues](https://github.com/scifmat/dsh-bidgenie/issues) 提交，附上插件版本、DSH 宿主版本和复现步骤；不要粘贴证件号、账号等敏感信息。

## 开发与发布同步

开发以 `D:\ProJects\bidgenie-flow\plugin` 为唯一源；本仓库只保留运行时代码、技能、模板、资产、README/LICENSE/tools，不包含 `pylibs/`、`tests/`、开发文档或验收产物。

同步运行时代码到本仓库：

```powershell
# 只检查差异
powershell -ExecutionPolicy Bypass -File tools\sync-from-dev.ps1 -Check

# 同步 + 提交 + 推送
powershell -ExecutionPolicy Bypass -File tools\sync-from-dev.ps1 `
  -Commit -Message "fix: ..." -Push
```

同步脚本会保留本仓库独有的 `README.md`、`LICENSE`、`package.json`、`tools/` 和封面图；不要在本仓库里直接改运行时代码，否则会与开发源产生双份维护。

## 许可

- 许可：**PolyForm Noncommercial 1.0.0**，仅限非商业使用；商业授权需另行联系版权方。全文见 [LICENSE](LICENSE)。

- 官网：[www.bidgenie.cn](https://www.bidgenie.cn)

- 联系：如需**商业授权**，请联系：[bidgenie@163.com](mailto:bidgenie@163.com)。

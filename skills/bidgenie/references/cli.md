# 通用 CLI 与错误参考（按需读）

## 调用形式

```text
"<pythonExe>" -X utf8 "<scriptsDir>/<script>.py" <子命令/参数> --workspace "<workspaceRoot>" --project "<projectDir>" --json
```

- Python 用 `bidgenie_paths.pythonExe`；stdout 只解析单个 JSON；`ok:false` 原样报 `error/message/hint/detail`。
- 退出码：2 用法 / 3 前置 / 4 校验 / 5 权限IO / 6 超时 / 7 锁 / 8 依赖 / 9 需用户决策。

## 控制台编码（P3，VM 冒烟）

脚本 stdout 已强制 UTF-8；Windows PowerShell 5.1 的管道/`ConvertFrom-Json` 仍可能按 ANSI 解码出现乱码（落盘 JSON 正常）。在命令前加一行：

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

或改用 Python 读取落盘 JSON（`json.loads(Path(...).read_text(encoding="utf-8"))`）；不要用 `chcp` 之外的“猜测编码”反复重试。

## 常用脚本

| 阶段 | 脚本 |
|---|---|
| F2 | `bg_workspace.py init|init-project`、`bg_env_check.py --report|--apply`、`bg_bidder.py list|create|update|delete|set-current` |
| F3 | `bg_convert.py`、`bg_extract_validate.py` |
| F4 | `bg_prepare.py`（requirements/--auto/--all-selected/--verify/--compact/--update-answers/record-override） |
| F5 | `bg_outline_validate.py`、`bg_outline_apply.py`（--materialize/--check-tree/--cleanup）、`bg_chapter_merge.py`、`bg_render.py` |
| F6 | `bg_review_script.py`、`bg_review_issues.py`（merge/close/verify/status） |
| F7 | `bg_finalize.py`、`bg_export_docx.py`、`bg_export_overview.py`、`bg_export_verify.py`、`bg_style_merge.py`、`bg_run_manifest.py` |
| 状态 | `bg_state.py status|check|transition|set-package` |

## 常见失败

| 场景 | 处理 |
|---|---|
| 脚本缺失/未就绪 | 如实告知「该功能未开放」，不伪造输出 |
| `ok:false` | 原样汇报 error/message/hint；不吞错、不重试超过 2 次 |
| guard 失败 | 输出缺口清单与修复命令，停在当前阶段，不强行迁移 |
| 状态不一致 | `bg_state.py status` 核对；`E_STATE_CORRUPT` 停止，不手写 state |
| 锁定态写正文 | `E_LOCKED` → 说明需 R4 解锁 |
| 沙箱权限失败 | 见下节「沙箱权限」：先请求用户切「完全权限」，不要重试/装依赖 |

## 沙箱权限（BUG-01，VM 冒烟）

`workspace-write` 会话下，插件依赖目录 `%USERPROFILE%\.dsh\data\bidgenie\pylibs`（fitz/pdfplumber/PIL/pywin32）
会被 DSH 沙箱拒绝读取（`WinError 5 / 拒绝访问`）。这是**环境限制，不是插件缺陷**，也不要尝试重装依赖。

识别信号（任一命中即可判断）：

- `bg_env_check.py` 的 `rootCause.code == "sandbox_permission"` 或 `sandboxHint` 非空，或 item 的 `detail.cause == "sandbox_permission"`；
- 脚本报 `E_IO` 且 `hint/detail` 含 `WinError 5 / 拒绝访问 / PermissionError`；
- 命令 traceback 指向 `_lib/deps.py` 或 `pylibs` 路径。

处理（**只做一次，不要反复重试**）：

1. 用 `ask_user_question` 请用户把本会话文件策略切到 **「完全权限」（danger-full-access）**，说明原因一句话：
   「插件依赖目录被沙箱拒绝读取，需要切完全权限才能转换 PDF / 导出 Word；这是 DSH 会话设置，切完继续即可。」
   选项建议：`我已切换，继续（推荐）` / `不切换，逐条命令授权（会明显变慢）`。
2. 用户确认后重跑一次 `bg_env_check.py --apply --workspace <ws> --json`；必需项通过再继续。
3. 用户拒绝切权限 → 如实停在当前阶段并说明受限能力（PDF 转换/导出等不可用），**不要**伪造结果或改用临时脚本绕过。

> 不要用 `--sandbox_permissions` 逐条升级来“绕”；用户切一次会话策略最省事。
> 旧提示里的 `BIDGENIE_TEMP_DIR` 只解决「临时目录不可写」，不解决 pylibs 不可读。

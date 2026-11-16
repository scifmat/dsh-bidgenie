# 通用 CLI 与错误参考（按需读）

## 调用形式

```text
"<pythonExe>" -X utf8 "<scriptsDir>/<script>.py" <子命令/参数> --workspace "<workspaceRoot>" --project "<projectDir>" --json
```

- Python 用 `bidgenie_paths.pythonExe`；stdout 只解析单个 JSON；`ok:false` 原样报 `error/message/hint/detail`。
- 退出码：2 用法 / 3 前置 / 4 校验 / 5 权限IO / 6 超时 / 7 锁 / 8 依赖 / 9 需用户决策。

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
| 沙箱权限失败 | 提示切换 DSH 文件策略为 danger-full-access/授权工作区 |

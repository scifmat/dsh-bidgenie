# F2 命令与异常参考（按需读）

## 命令

```text
# 环境自检
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_env_check.py" --report --workspace "<ws>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_env_check.py" --apply [--item <id>] --workspace "<ws>" --json
# 初始化 dsh/ 与项目
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_workspace.py" init --workspace "<ws>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_workspace.py" init-project --workspace "<ws>" [--bidder <bidderId>] [--project-name "<名称>"] --json
# 投标人
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" list|create|update|set-current|validate --workspace "<ws>" [...] --json
# T1
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to info_extract --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to info_extract --workspace "<ws>" --project "<projectDir>" --json
```

## 异常

| 场景 | 处理 |
|---|---|
| 环境必需项失败 | 给修复指引并终止；非必需项记录降级/豁免 |
| 需要装依赖 | 展示命令与影响，等用户确认，不自动安装 |
| `dsh/` 已存在但损坏 | 先 `bg_workspace.py init` 补齐；仍失败从 `dsh/backups/` 恢复 |
| 同名项目冲突 | 提示「继续 / 换名 / 新建」 |
| init-project 未就绪 | 如实说明，禁止手建 project.json/state.json |
| 投标人缺失/不完整 | 完整度提示不阻断；缺少当前投标人则先创建/选择 |
| 脚本缺失/未就绪 | 如实说明，不伪造成功 |

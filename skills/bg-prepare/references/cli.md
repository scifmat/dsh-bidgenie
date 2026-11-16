# F4 命令与异常参考（按需读）

## 基础命令

```text
# 提取要求（人员/设备/工期/字数来源）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" requirements --workspace "<ws>" --project "<projectDir>" [--package "package-1"] --json
# 逐包/全部选中包 prepare（先全部 prepare，再首包 T3）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" [--package "package-1"] --answers "<answers.json>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" --all-selected --json
# auto（不提问；读取 interactionMode=auto）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" --auto --json
# 校验
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" --verify --json
# 旧 meta 瘦身 / 答案更正
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" --compact --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" --workspace "<ws>" --project "<projectDir>" --update-answers "<answers.json>" --json
# 单字段人工改写（留痕）
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_prepare.py" record-override --field personnelPlan --value "<新值>" --note "<原因>" --workspace "<ws>" --project "<projectDir>" --package "package-1" --json
```

## answers JSON（guided 每包一份）

```json
{
  "expectedWordCount": 50000,
  "personnelPlan": "项目经理1名、驻场运维约8人（具体以进场安排为准）",
  "equipmentPlan": "高空作业车1台、运维检测终端2台",
  "schedulePlan": { "startDate": "", "deliveryDate": "", "constructionPlan": "合同签订后30日内开工" },
  "figureQuota": { "table": 5, "mermaid": 5, "terminal": 5 },
  "valueSource": { "expectedWordCount": "agent-default", "personnelPlan": "user", "equipmentPlan": "user", "schedulePlan": "agent-default", "table": "user", "mermaid": "user", "terminal": "user" },
  "exportStyleProfile": "official-tech"
}
```

- 7 项 = 字数、人员、设备、工期、表格/流程图/终端图配额；样式不作为提问项。
- 字数默认**动态**：`user-settings.defaultWordCountTier`（缺省 50000），提问卡显示实际值；与提取稿要求下限取大值，不写死 5 万
- 样式默认：`styleProfiles.tech` → 兼容 `styleProfile` → `official-tech`（写 `exportStyleProfile`）。
- 配额是下限、上不封顶；人员/设备/工期 guided 每包必须有答案或「用默认」。

## T3 前置与迁移

```text
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" set-package --package "<package-N>" --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" check --to outline --workspace "<ws>" --project "<projectDir>" --json
"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_state.py" transition --to outline --package "<writingPackages[0]>" --workspace "<ws>" --project "<projectDir>" --json
```

T3 必须显式 `--package "<writingPackages[0]>"`；失败先 `--verify` 出缺包清单补齐。

## 异常

| 场景 | 处理 |
|---|---|
| 脚本未就绪 | 停止并告知；禁止手建包目录/package-meta |
| requirements 未就绪 | 从 05/06/07/08/09 摘要求片段（只取要求、标注来源）；auto 仍静默 |
| 多包/关键问题未答 | 阻塞；`pendingInput` 记录，恢复后重问 |
| 非法值 | 脚本拒绝 → 重问；`table=0` 允许但确认 |
| 目录/meta 已存在 | 幂等复用；重建先备份 |
| 旧字段（mustSaveMaterials 等） | `--compact` 迁移后重跑 `--verify` |
| T3 guard 失败 | 全部 selected `--verify` 补 prepare 后重试，不强行迁移 |
| T3 post-check 失败 | `E_STATE_CORRUPT` 停止，不手写 state |
| 用户问商务问题 | 说明不在技术方案范围 |
| 磁盘不足/超时 | 终止并提示；不静默重试超过 2 次 |

---
name: bg-bidder
description: BidGenie 投标人资料管理（F1 Agent 侧）。当需要创建/选择/补全投标人，或用户提到「投标人/公司资料/资质证书/业绩/奖惩/当前公司」，以及 F2 创建项目前需要校验当前投标人时使用。
---

# bg-bidder（F1 投标人资料）

## 何时使用

- 工作区还没有任何投标人，或没有设置「当前投标人」（F2 的前置条件）。
- 用户要新建/修改公司（或自然人）资料、证书、业绩、奖惩。
- F2 创建项目前校验当前投标人；后续撰写阶段需要按需读取素材（M5+）。

## 数据位置（只读理解；写必须经脚本/面板）

```text
<workspaceRoot>/dsh/config/user-settings.json          # currentBidderId
<workspaceRoot>/dsh/registry.json                      # bidders[] 索引
<workspaceRoot>/dsh/bidders/<dirName>/
  bidder.json / certificates.json / performance.json / awards-penalties.json / profile.md / meta.json
```

## 操作方式（优先级从高到低）

1. **面板（推荐引导用户）**：左栏「标代精灵 → 投标人」新建/编辑/设为当前/软删除。面板不注入会话，Agent 只做引导与结果说明。
2. **模型工具（Agent 调）**：`bidgenie_bidder`，`workspace` 必填，`action` = `list` / `create` / `update` / `delete` / `set-current`；写操作由 Host 经 `bg_bidder.py` 落盘。
3. **脚本直调（批量/排障）**：

   ```text
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" list    --workspace <ws> --json
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" create  --workspace <ws> --data <json-file> --json
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" update  --workspace <ws> --bidder <id> --data <json-file> --json
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" set-current --workspace <ws> --bidder <id> --json
   "<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_bidder.py" delete  --workspace <ws> --bidder <id> --confirm --json
   ```

   `--data` 结构：`{bidder:{...}, certificates?:{items:[]}, performance?:{items:[]}, awardsPenalties?:{awards:[],penalties:[]}}`。

## 硬约束

1. **只用已有事实**：证书名称/等级/有效期、业绩名称、奖惩情况只能来自 `bidder.json`/`certificates.json`/`performance.json`/`awards-penalties.json` 的已有条目；**禁止编造**。
2. **敏感信息**：证件号、银行账号、证书编号、联系电话等不写入方案正文/对话摘要/日志；面板默认脱敏（S14）。
3. **writable=false** 的投标人资料禁止在正文引用（用户隐私设置）。
4. 证书有效性：`validTo` 早于项目投标截止时间、或状态 `expired/unknown` 的证书，不得当作有效资质引用；需回避并提示用户。
5. **当前投标人**：只能在用户明确选择后变更；多个候选时用 `ask_user_question` 让用户选（不猜、不超时）。
6. **删除**：软删除到 `dsh/bidders/.trash/`，必须二次确认；被项目引用时提示「历史项目保留快照」。
7. 完整度低（缺证书/业绩/简介）**不阻塞**项目创建，只提示可在撰写前补充。

## 交接

- 出口（→F2）：`user-settings.currentBidderId` 存在且对应 `bidder.json` 可解析；否则不得创建项目目录。
- 若用户中途切换投标人：已创建项目保持不变（项目 `bidderRef` 为创建时快照），记录提示即可。

## 资源

- 脚本：`{{SCRIPTS}}/bg_bidder.py`
- 面板：DSH 左栏「标代精灵」

---
name: bg-init
description: BidGenie 项目初始化（F2）。当用户上传招标文件后要开始写技术方案、说「初始化项目/开始投标/新建投标项目」，或工作区尚未初始化需要创建 dsh/ 目录与项目元数据时使用；包含环境自检与 T1 迁移。
---

# bg-init（F2 项目初始化）

1. **先读偏好**：`dsh/config/user-settings.json.interactionMode`；已设置 guided/auto 就一句确认，**不问「自动还是逐步」**；缺失才问一次。
2. **环境自检**：`bg_env_check.py --report`；必需项失败终止，非必需项记录降级/豁免；需要装依赖时只给命令与影响，等用户确认。
   - **沙箱权限（BUG-01）**：若 `rootCause.code=sandbox_permission` / item `detail.cause=sandbox_permission`，或脚本报 `WinError 5/拒绝访问`（常见于读 `%USERPROFILE%\.dsh\data\bidgenie\pylibs`），**不要重试、不要点/触发修复环境**；用 `ask_user_question` 请用户把会话文件策略切到**「完全权限」（danger-full-access）**，说明「切完才能转换 PDF/导出 Word」，用户确认后重跑一次 `bg_env_check.py --apply`。用户拒绝则停在当前阶段并说明受限能力。详见 `bidgenie/references/cli.md` 的「沙箱权限」。
3. **初始化工作区**：`bg_workspace.py init`（幂等，只补缺失，不重建）。
4. **创建项目**：`bg_workspace.py init-project`（脚本生成目录/project.json/state.json/registry 与事件；幂等；用户说继续已有项目时不新建）。**用户明确要“重新跑一次/新建一个”时加 `--new` 强制新建**（旧项目保留，可 T11 abort 或 resume）；不加 `--new` 时若复用未完成项目，按 warning 提示用户可 `--new` 或先 T11。
5. **一致性自检**：创建项目后跑一次 `bg_state.py doctor --project <项目>`（只读；输出当前阶段、包目录、建议命令）；不可用则标注跳过。
6. **投标人完整度提示（T1 前）**：读当前投标人 completeness/资格资料缺口；低完整度或缺证书/业绩/简介 → guided 提示确认（可稍后补，不阻断），auto 记 warning；不因完整度阻止迁移。
7. **T1**：`bg_state.py check --to info_extract` 通过后 `transition --to info_extract --package package-1`；成功才算 F2 完成，随后加载 `bg-extract`；`bg-extract` 未注册时如实停在 `info_extract`。

## 硬约束

1. 目录/JSON/状态/注册表只由脚本写；禁止手写 project.json/state.json/registry.json。
2. 不覆盖已有项目、不删用户数据；取消时保留半成品可重试。
3. 必要提问阻塞、不超时、不猜；只对非必需环境项降级。
4. 不向用户展示技术词与敏感值；修复建议可执行。

## 参考

- 命令与异常：`references/cli.md`（按需读）
- 运行时路径见文末 Host 注入块。

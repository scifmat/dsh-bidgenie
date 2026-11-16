---
name: bg-export-style
description: BidGenie 导出样式配置辅助。指导用户在 dsh/styles/{tech,keyinfo,overview}/ 下自建或修改导出样式 JSON；用户说「自定义导出样式/改导出字体/改页边距/改表格样式/新建样式」时加载。只给方法与规格，不写业务状态。
---

# bg-export-style（导出样式配置辅助）

## 用途

- 指导用户或 Agent 在 `<workspace>/dsh/styles/{tech,keyinfo,overview}/` 下创建/修改导出样式 JSON。
- 只提供样式规格与操作步骤；不写项目状态、不改正文、不调用导出脚本。
- 完整字段规格见 `references/style-spec.md`；最小示例见 `references/example-custom-style.json`。

## 快速步骤

1. **选类型**：技术方案用 `tech`（目录 `dsh/styles/tech/`）、关键信息用 `keyInfo`（目录 `dsh/styles/keyinfo/`）、工作概览用 `overview`（目录 `dsh/styles/overview/`）。
2. **从官方复制**：读 `templates/styles/<对应目录>/official-*.json`，复制到 `dsh/styles/<对应目录>/<自定义文件名>.json`；不要改插件自带的官方文件。
3. **改标识**：改 `id`（全小写字母/数字/连字符，不与官方 id 冲突）、`name`、`label`；`docType` 必须与目录一致（`tech`/`keyInfo`/`overview`）；`schemaVersion` 保持 `"1.0"`；`basedOn` 可保留官方 id 作说明。
4. **改参数**：按 `references/style-spec.md` 调 `params`（字体/字号/行距/页边距/标题编号/表格/题注等）。颜色字段一律 `"auto"`，字体只用 Windows 自带字体。
5. **验证**：`bg_style_merge.py --validate --profile <id>`（M8 脚本；未就绪时先用下面的接口初检）。
6. **刷新与选用**：保存后在面板设置页点刷新（或调 `GET /bidgenie/styles?kind=<类型>`）查看 custom 列表与 warnings；在设置页把该样式选为对应文档类型的默认样式（保存为 `styleProfiles.<类型>=<id>`）。

## 三类 docType

| docType | 目录 | 用途 |
|---|---|---|
| `tech` | `dsh/styles/tech/` | 技术方案正文导出 |
| `keyInfo` | `dsh/styles/keyinfo/` | 关键信息文档导出（无封面/目录/页眉页脚） |
| `overview` | `dsh/styles/overview/` | 工作概览导出 |

## 硬规则

1. **颜色必须 `"auto"`**：`params.title.color`、`params.table.color`、`params.captions.color` 以及 `textColor`/`headingColor`/`captionColor`/`tableColor` 全部写 `"auto"`，不写 `#RRGGBB` 或颜色名；表头底纹 `headerShading` 可写六位十六进制（如 `D9D9D9`）。
2. **字体白名单（Windows 自带）**：中文 `宋体`/`黑体`/`仿宋`/`楷体`/`微软雅黑`；西文 `Times New Roman`/`Arial`/`Calibri`。不写第三方字体。
3. **数值单位固定**：字号 `pt`（数字）、行距倍数（如 `1.5`）、页边距 `mm`（数字）、首行缩进字符数（整数）。
4. **标题编号**：`headingNumbering` 布尔；`headingNumberingScheme` 只支持 `%1`–`%6` 占位（如 `{"h1":"第%1章","h2":"%1.%2"}`）。
5. **不配置封面/目录/水印**：M8 导出不生成封面与目录；样式文件不要写这些内容。
6. **文件位置正确**：`dsh/styles/<类型>/xxx.json`；放在 `dsh/styles/` 根目录会被当作旧版扁平样式并给迁移 warning。
7. **JSON 合法**：无注释、无尾逗号、UTF-8；`schemaVersion`/`id`/`name`/`docType` 必填。

## 验证与刷新

- 样式 JSON 是否可用：`GET /bidgenie/styles?kind=<类型>&workspace=<工作区>` 看 `custom` 列表与 `warnings`（非法 JSON、缺字段、docType 与目录不一致都会进 warnings，接口本身不报错）。
- 正式校验（脚本就绪后）：`"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_style_merge.py" --validate --profile "<id>" --json`。
- 选中样式：`PUT /bidgenie/settings` 提交 `{"settings":{"styleProfiles":{"<类型>":"<id>"}}}`；或面板设置页选择并保存。
- 刷新：面板设置页刷新按钮；无面板时重新 `GET /bidgenie/styles` 即可看到新文件。

## 常见错误

| 现象 | 原因/处理 |
|---|---|
| custom 里没有新样式 | 文件不在 `dsh/styles/<类型>/`、扩展名不是 `.json`、JSON 解析失败（看 warnings） |
| 样式被忽略并提示「id 与官方样式冲突」 | 自定义 id 与官方 id 相同；换一个自定义 id |
| 样式被忽略并提示「docType 与目录不一致」 | `docType` 与所在目录不匹配；改成对应值或移动文件 |
| 旧版扁平样式 warning | 文件放在 `dsh/styles/` 根目录；移动到 `tech/`、`keyinfo/` 或 `overview/` |
| 设置保存 400「样式不可用」 | id 不存在或类型不匹配；先确认 `GET /bidgenie/styles?kind=...` 的 custom 列表 |
| 字体/颜色在导出后不对 | 用了非白名单字体或非 `"auto"` 颜色；按 §硬规则 修正 |
| 面板看不到变化 | 未刷新；重开设置页或重新调用 styles 接口 |

## 资源

- 字段规格：`references/style-spec.md`
- 最小示例：`references/example-custom-style.json`
- 官方样式：`{{TEMPLATES}}/styles/{tech,keyinfo,overview}/official-*.json`
- 运行时绝对路径见文末 Host 注入块。

# 导出样式字段规格（v1.0）

样式文件放 `<workspace>/dsh/styles/{tech,keyinfo,overview}/<文件名>.json`；文件名任意（`.json`），**id 才是样式标识**。官方样式在 `plugin/templates/styles/{tech,keyinfo,overview}/official-*.json`，建议复制后改。

## 1. 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemaVersion` | string | 是 | 固定 `"1.0"` |
| `id` | string | 是 | 全小写字母/数字/连字符；不能与官方 id（`official-tech`/`official-keyinfo`/`official-overview`）相同 |
| `name` | string | 是 | 样式中文名（人读） |
| `label` | string | 否 | 设置页展示名，缺省用 `name` |
| `docType` | string | 是 | `tech` / `keyInfo` / `overview`，必须与所在目录一致 |
| `description` | string | 否 | 一行说明 |
| `basedOn` | string \| null | 否 | 参考的官方样式 id（仅说明，不自动继承） |
| `params` | object | 是 | 样式参数，见 §2 |
| `createdAt` / `updatedAt` | string | 否 | ISO 时间，可省略 |

## 2. `params` 字段

### 2.1 页面与字体

| 字段 | 类型 | 取值范围/说明 | 官方 tech 示例 |
|---|---|---|---|
| `fontFamily` | string | 中文正文字体，白名单：宋体/黑体/仿宋/楷体/微软雅黑 | `"宋体"` |
| `headingFontFamily` | string | 中文标题字体，同上白名单 | `"黑体"` |
| `fontLatin` | string | 西文字体，白名单：Times New Roman/Arial/Calibri | `"Times New Roman"` |
| `fontSizePt` | number | 正文字号 9–16（pt） | `12` |
| `lineSpacing` | number | 行距倍数 1.0–2.0 | `1.5` |
| `pageMarginMm` | object | `{top,bottom,left,right}`，10–40（mm） | `{top:20,bottom:20,left:19,right:19}` |
| `firstLineIndentChars` | integer | 首行缩进字符数 0–4 | `2` |
| `bodyAlignment` | string | `left`/`center`/`right`/`justify` | `"justify"` |

### 2.2 标题

| 字段 | 类型 | 说明 |
|---|---|---|
| `headingSizesPt` | object | `{h1..h6}` 字号（pt）；h1 14–26，其余 12–22，逐级不增大 |
| `headingNumbering` | boolean | 是否 Word 原生多级编号 |
| `headingNumberingScheme` | object | `{h1..h6}` 编号格式，占位 `%1`–`%6`；h1 可用「第%1章」 |
| `title` | object | 文档大标题：`{fontFamily,fontSizePt(12–30),bold,align(center/left/right),spaceAfterPt,fontLatin,color:"auto"}` |

### 2.3 表格

| 字段 | 类型 | 说明 |
|---|---|---|
| `table.autoFit` | boolean | 按内容自适应列宽 |
| `table.cellAlign` | string | 单元格水平对齐 `left`/`center`/`right` |
| `table.cellVerticalAlign` | string | `top`/`center`/`bottom` |
| `table.headerBold` | boolean | 表头加粗 |
| `table.headerShading` | string | 表头底纹，六位十六进制（如 `D9D9D9`） |
| `table.fontSizePt` | number | 表格字号 8–14 |
| `table.style` | string | Word 内置表样式名，如 `"Table Grid"` |
| `table.minColumnWidthCm` | number | 最小列宽 0.5–5（cm） |
| `table.maxColumnWeight` | number | 单列最大权重 10–80 |
| `table.fontFamily` / `table.fontLatin` | string | 表格中/西文字体（白名单同 §2.1） |
| `table.color` | string | 固定 `"auto"` |

### 2.4 题注与颜色

| 字段 | 类型 | 说明 |
|---|---|---|
| `captions.tablePrefix` / `captions.figurePrefix` | string | 题注前缀：tech/overview 用 `▼表`/`▲图`；keyInfo 用 `表`/`图` |
| `captionPrefix` | object | `{table,figure}` 与上面一致 |
| `captions.fontSizePt` | number | 题注字号 8–14 |
| `captions.align` | string | `center`/`left`/`right` |
| `captions.spaceBeforePt` / `spaceAfterPt` | number | 段前/段后（pt） |
| `captions.fontFamily` / `captions.fontLatin` | string | 题注中/西文字体（白名单） |
| `captions.color` | string | 固定 `"auto"` |
| `textColor` / `headingColor` / `captionColor` / `tableColor` | string | **全部固定 `"auto"`**（Word 自动色） |

### 2.5 兼容别名（可省略）

官方样式同时写了平铺别名：`tableFontSizePt`、`tableHeaderFill`、`tableHeaderBold`、`tableStyle`、`headingFontLatin`、`captionFontFamily`、`captionFontLatin`、`tableFontFamily`、`tableFontLatin`。它们与嵌套字段等价，保留可读性更好；自定义样式只写嵌套字段也能用。

## 3. 约束

- **颜色一律 `"auto"`**：`title.color`、`table.color`、`captions.color`、`textColor`、`headingColor`、`captionColor`、`tableColor` 必须为 `"auto"`，不写十六进制/颜色名（表头底纹 `headerShading` 例外）。
- **字体白名单**：只用 Windows 常规自带字体（中文：宋体/黑体/仿宋/楷体/微软雅黑；西文：Times New Roman/Arial/Calibri）。
- **单位固定**：字号 pt、行距倍数、页边距 mm、缩进字符数（整数）。
- **编号**：`headingNumberingScheme` 只支持 `%1`–`%6` 占位；不写其他编号语法。
- **不配置封面/目录/水印**：M8 不生成封面与目录，样式里不要写这些内容。
- **目录与 docType 一致**：`tech`→`tech/`、`keyInfo`→`keyinfo/`、`overview`→`overview/`；放在 `dsh/styles/` 根目录会按旧版扁平样式处理并给迁移 warning。
- **未知字段忽略**：Host 只校验 `schemaVersion`/`id`/`name`/`docType`（及目录一致性）；params 的深度校验在导出/合并阶段（`bg_style_merge.py --validate`）。

## 4. 三种类型的最小骨架

tech（技术方案）：

```json
{"schemaVersion":"1.0","id":"custom-tech-1","name":"我的技术方案样式","docType":"tech","basedOn":"official-tech",
 "params":{"fontFamily":"宋体","headingFontFamily":"黑体","fontLatin":"Times New Roman","fontSizePt":12,"lineSpacing":1.5,
  "pageMarginMm":{"top":20,"bottom":20,"left":19,"right":19},
  "title":{"fontFamily":"黑体","fontSizePt":22,"bold":true,"align":"center","fontLatin":"Arial","color":"auto"},
  "table":{"autoFit":true,"cellAlign":"center","fontFamily":"宋体","fontLatin":"Times New Roman","color":"auto"},
  "captions":{"tablePrefix":"▼表","figurePrefix":"▲图","fontFamily":"宋体","fontLatin":"Times New Roman","color":"auto"},
  "textColor":"auto","headingColor":"auto","captionColor":"auto","tableColor":"auto"}}
```

keyInfo（关键信息）：`docType` 改 `"keyInfo"`、放 `dsh/styles/keyinfo/`；题注前缀改为 `{"tablePrefix":"表","figurePrefix":"图"}`，其余参数可沿用。

overview（工作概览）：`docType` 改 `"overview"`、放 `dsh/styles/overview/`；题注前缀用 `▼表`/`▲图`。

## 5. 验证

- 轻量检查：`GET /bidgenie/styles?kind=<tech|keyInfo|overview>&workspace=<工作区>` 看 `custom` 与 `warnings`。
- 正式校验（M8 脚本就绪后）：`"<pythonExe>" -X utf8 "{{SCRIPTS}}/bg_style_merge.py" --validate --profile "<id>" --json`。

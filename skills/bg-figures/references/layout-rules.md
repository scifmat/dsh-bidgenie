# 字号/布局/机检规则

## mermaid

- 默认 `flowchart TB`；纵向线性链 ≥8 节点改 `LR`（或拆图），单图节点 ≤12，标签尽量 ≤10 汉字。
- 长链/多分支优先拆成两张图，不靠缩字号。
- 渲染后机检字段（`render-manifest.json` 的 `layout`）：
  - `viewBoxWidth`：图固有宽度；目标 ≤950px（A4 正文约 8pt 所需），>1200px 优先拆图，>2000px 必须拆图；
  - `fontPtAtDisplay`：按 A4 正文宽度换算的有效字号；<8pt 记 minor，<6.5pt 记 major；
  - `aspectTall`（高/宽）>3.0 或 `aspectWide`（宽/高）>4.0 记 minor，提示改布局/合并；
  - `diagramType`：应与大纲 `subtype` 一致。
- 自动布局：`flowchart LR/RL` 且图过宽时，渲染器会在临时副本里改 `TB` 再渲染；记录 `layout.autoLayout`。仍不达标则必须拆图。
- C 层审图仍要检查：图文契合、图型选对、文字可辨、无遮挡/空泛。

## terminal

- 16px 字号、42 列上限（中文 2 列）、行高 24px。
- 超长行自动折行，不缩字号；一张图 8–18 行，总行数 >22 提示删减/拆图。
- 行数/列宽/折行/占位词由 A 层机检；C 层仍审内容真实性、与章节契合度、是否正确使用终端图。

## 规划数量

- `table ≤2/章`、`mermaid ≤2/章`、`terminal ≤4/章`。
- `terminal` 配额是参考值（0=不生成）；实际数量按适合章节定，不平均分配、不硬凑。
- 特殊章节不规划图。

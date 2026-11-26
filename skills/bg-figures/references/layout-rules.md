# 字号/布局/机检规则

## mermaid

- 默认 `flowchart TB`；节点 6–7、判定 ≤3、回边 ≤1、标签 ≤12 字；超过 7 节点先拆图。
- **TB 近线性链**（节点 ≥7、判定 ≤1、基本一条链）容易超高：真实案例 7 节点/1 判定/0 回边 → 高宽比 3.94、字号 13.7pt。优先改 `flowchart LR`（7–11 节点）或合并到 ≤6 节点/拆 2 张；不要只靠缩字号。
- 有并列分支（同一层 2 个节点）会横向撑宽，7 节点也可能安全（真实案例 7 节点/1 判定/1 回边 → 2.15）；以预判结果为准。
- 长链/多分支优先拆成两张图，不靠缩字号。
- 渲染前 `bg_render.py` 会给 `layoutPreflight/estimatedAspectTall/rewriteSuggestion`（只看预判，不改图）；渲染后 `layout.warnings` 才是机检结论。
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

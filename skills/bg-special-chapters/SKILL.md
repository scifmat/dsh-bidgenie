---
name: bg-special-chapters
description: BidGenie 特殊章节处理（技术标中的证书/检测报告/偏离表/承诺函/样品等）。当 F5-1 识别到技术文件或技术评分要求投标人提供证明材料、参数响应表、第三方报告、承诺/声明、样品/演示时加载；用于判定特殊章节、写 handling、按模板写正文、审查只审四项、优化只改文字。
---

# bg-special-chapters（特殊章节）

## 触发与范围

- 只在**技术文件/技术评分**里识别；商务/报价/资格文件本身不写成技术章节。
- 命中下列任一要求时标记为特殊章节：证书/证明、第三方材料、技术参数响应/偏离表、承诺函/声明函、样品/演示/现场考察。
- 普通技术方案、管理措施、人员组织、进度计划、培训方案不标记。

| materialType | 典型内容 |
|---|---|
| certificate | 资质、认证、营业执照、许可证、人员证书、身份证/劳动合同、业绩合同、验收报告 |
| test_report | 检测/检验报告、3C/入网许可 |
| deviation_table | 技术参数响应表、技术偏离表、逐条参数对照 |
| product_literature | 彩页、软著、专利、原厂授权/售后承诺 |
| performance | 业绩合同、验收报告、用户证明 |
| personnel | 人员证书、身份证、劳动合同、社保 |
| commitment | 承诺函、声明函（技术标内要求时才处理） |
| sample | 样品、演示、现场考察（只写流程说明，不代做） |

## F5-1：写 handling

大纲末级增加：

```json
{
  "id": "2-3",
  "title": "技术、服务偏离表",
  "criterionRefs": [{ "criterionId": "CR-02" }],
  "contentRequirements": ["特殊处理：要求 + 处理方式 + 请用户补充"],
  "wordTarget": 200,
  "figurePlan": [],
  "insertAnchors": [],
  "handling": {
    "mode": "user_material",
    "materialType": "deviation_table",
    "requirements": [
      {
        "text": "按招标文件第六章格式填写《技术、服务偏离表》",
        "sourceRef": "extraction/packages/package-1/04-Compilation-Requirements.md#响应文件格式"
      }
    ],
    "userAction": "请用户按第六章格式填写偏离表，并确认偏离项"
  }
}
```

- `mode` 固定 `user_material`；`materialType` 用上表枚举。
- `requirements` 每条带原文 `text` 与 `sourceRef`；`userAction` 写清请用户补什么/确认什么。
- `criterionRefs` 仍必填：有技术评分就引对应 factor；只有格式要求时引最相关技术因素。
- `wordTarget` 默认 200，允许 100–300；脚本按 100 取整（100/200/300），普通章节分摊剩余字数。
- 特殊章节**不规划图**：`figurePlan=[]`、`insertAnchors=[]`。

## F5-2：写正文

正文约 150–250 字，固定三段：

```markdown
### 特殊处理说明

本项为招标文件要求的《技术、服务偏离表》（来源：第…章）。按招标要求需填写…，具体偏离内容需用户确认。

**招标要求**

- …（原文 1–3 条，带来源）

**处理方式**

原因：本项所需的证明材料/具体参数需由用户提供并核对，方案不代为承诺、不编造。
请用户按招标文件要求补充；本方案只保留要求说明与配合方式。

**请用户补充**

- 偏离表：…
- 确认事项：…
```

- 不写“我方已具备/已提供”等无法核实的承诺。
- 不写证书编号、检测结论、业绩金额、人员姓名。
- 不用“不响应”表述；写“不在方案中代填，请用户补充”。
- 章节结果照常 `status=done`、`wordCount` 实际字数、`summary` 200–400 字符。
- 不写图/表，不写 `BG:FIGURE`，不创建资源文件。

## F6：审查

- **A 层**：特殊章节跳过 90%–120% 字数检查与图表检查；只要求文件存在、非空、章节结果 done；报告标注“特殊章节（免字数检查）”。
- **B/C 层**：先读 `handling`，知道该章为什么短；只审四件事：
  1. 要求摘录是否准确、来源是否对得上；
  2. 处理方式是否合理（不代填、不承诺、不编造）；
  3. 请用户补充是否明确；
  4. 是否出现编造/绝对化承诺。
- 不因字数少、无证明材料、无图判问题。

## F6.4：优化

- 只改文字表达；不扩写、不补材料、不新增图、不改 handling。

## 检查清单

- [ ] 特殊章节只出现在技术文件/技术评分要求处。
- [ ] `handling` 五要素齐全：mode/materialType/requirements/userAction/来源。
- [ ] `wordTarget` 200（或 100–300），`figurePlan=[]`。
- [ ] 正文三段齐全，含“请用户补充”，无编造承诺。
- [ ] 审查只审四项，不按普通章节字数/图表判问题。

## 参考

- 字段与枚举正反例：`references/outline-handling.md`
- 写作模板与禁用表述：`references/writing-template.md`

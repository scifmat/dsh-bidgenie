# 特殊章节 handling 字段与判定

## 判定顺序

1. 要求是否出现在技术文件/技术评分中？否 → 不标记。
2. 是否要求投标人提供“现成材料/第三方材料/参数响应/承诺/样品”？是 → 标记特殊章节。
3. 是否只是普通技术方案、管理措施、人员组织、进度、培训？是 → 普通章节。
4. 商务资格要求只在 F4 的“资格/证书要求提示（商务部分）”里提示，不写成技术章节。

## 枚举

| materialType | 中文 | 例子 |
|---|---|---|
| certificate | 证书/证明 | 资质证书、认证、营业执照、许可证 |
| performance | 业绩/验收材料 | 合同、验收报告、用户证明 |
| personnel | 人员证书/劳动关系 | 人员证书、身份证、劳动合同、社保 |
| test_report | 检测/检验报告 | 第三方检测、3C、入网许可 |
| product_literature | 彩页/软著/专利/授权 | 产品彩页、软著、专利、原厂授权 |
| deviation_table | 偏离表/参数响应 | 技术偏离表、逐条参数对照 |
| commitment | 承诺函/声明函 | 技术承诺、服务承诺、声明 |
| sample | 样品/演示/现场 | 样品、演示、现场考察 |

## 正例

招标要求：“投标人须提供所投产品由第三方检测机构出具的检测报告，并提供技术偏离表。”

```json
{
  "id": "2-3",
  "title": "第三方检测报告与技术偏离表",
  "wordTarget": 200,
  "figurePlan": [],
  "handling": {
    "mode": "user_material",
    "materialType": "test_report",
    "requirements": [
      { "text": "提供所投产品由第三方检测机构出具的检测报告", "sourceRef": "extraction/packages/package-1/09-Technical-Requirements.md#检测报告" },
      { "text": "按第六章格式填写技术偏离表", "sourceRef": "extraction/packages/package-1/04-Compilation-Requirements.md#响应文件格式" }
    ],
    "userAction": "请用户提供检测报告，并按第六章格式填写技术偏离表、确认偏离项"
  }
}
```

## 反例

- 把“资格要求：投标人须具备安全生产许可证”写成技术章节 → 商务资格，交 F4 提示，不写技术标。
- 把“项目实施方案”写成特殊章节 → 普通技术章节。
- `materialType` 写成 `report`/`cert` 等非枚举值 → 校验 blocker。
- `requirements` 只写“检测报告”没有原文和来源 → 校验 blocker。
- 特殊章节带 `figurePlan` 或 `insertAnchors` → 校验 blocker。
- `wordTarget=800` → 超出 100–300，校验 blocker。

## 常见错误

| 现象 | 处理 |
|---|---|
| 大纲校验报 `handling_material_type_invalid` | 换成上表枚举 |
| 报 `handling_requirements_invalid` | 补至少一条 `{text, sourceRef}` |
| 报 `handling_user_action_missing` | 写清请用户补充/确认什么 |
| 报 `special_figure_plan_forbidden` | 删除 figurePlan/insertAnchors |
| 报 `special_word_target_invalid` | 改 100–300（建议 200） |
| 报 `handling_on_parent` | 把 handling 移到末级章节，或改为普通父级 |

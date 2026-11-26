# mermaid 类型与最小语法骨架

## flowchart

```mermaid
flowchart TB
  A[接警] --> B{能否现场修复}
  B -->|能| C[现场修复]
  B -->|不能| D[返厂/更换]
```

- 方向默认 TB；节点 6–7、判定 ≤3、回边 ≤1；超过 7 节点先拆图，不要只靠改 LR 硬塞。
- 节点 ≤12；标签 ≤10 汉字。

## sequenceDiagram

```mermaid
sequenceDiagram
  participant 值守 as 运维值守
  participant 平台 as 运维平台
  participant 现场 as 现场人员
  值守->>平台: 接警登记
  平台->>现场: 派单
  现场-->>平台: 到场/修复反馈
```

- 参与者用正文中的真实角色/系统名；不要超过 6 个参与者。

## gantt

```mermaid
gantt
  title 年度运维排期
  dateFormat YYYY-MM-DD
  section 巡查
  第一季度全面巡查 :a1, 2026-01-01, 90d
  第二季度全面巡查 :a2, after a1, 90d
```

- 日期必须来自工期计划/正文；不得虚构。

## stateDiagram-v2

```mermaid
stateDiagram-v2
  [*] --> 正常
  正常 --> 故障: 告警
  故障 --> 维修中: 派单
  维修中 --> 正常: 修复验收
```

## erDiagram

```mermaid
erDiagram
  DEVICE ||--o{ WORK_ORDER : has
  DEVICE {
    string device_id
    string type
    string status
  }
```

## timeline

```mermaid
timeline
  title 年度运维节奏
  第一季度 : 全面巡查
  第二季度 : 专项治理
  第三季度 : 迎峰保障
  第四季度 : 年度总结
```

## mindmap

```mermaid
mindmap
  root((运维重点))
    前端设施
      信号灯
      电子警察
    平台系统
      数据接入
      运维管理
```

## block-beta

```mermaid
block-beta
  columns 3
  展示层["展示层"]:3
  应用层["应用层"]:3
  数据层["数据层"]:3
```

## architecture-beta

```mermaid
architecture-beta
  group cloud(cloud)[中心平台]
  service api(server)[接口服务] in cloud
  service db(database)[数据库] in cloud
```

## quadrantChart

```mermaid
quadrantChart
  title 风险分级
  x-axis 低影响 --> 高影响
  y-axis 低概率 --> 高概率
  点位故障: [0.2, 0.8]
  平台中断: [0.9, 0.4]
```

## pie / xychart-beta

```mermaid
pie title 设施类型占比
  "视频监控" : 2334
  "电子警察" : 1936
  "交通卡口" : 1691
```

```mermaid
xychart-beta
  title 月度工单量
  x-axis [1月, 2月, 3月]
  y-axis "工单数" 0 --> 500
  bar [320, 280, 350]
```

- pie/xychart 只能使用有来源的数据；没有数据改用 flowchart/table。

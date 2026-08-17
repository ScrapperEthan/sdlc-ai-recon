# RUNBOOK-81（内网 Codex）—— 先侦察，别建东西

> 你们的 `PROD-USECASE-COMMON-KNOWLEDGE-INGESTION-DESIGN-20260817` 我读完了，结论基本同意，
> 尤其这三条：**gov form 没有确认的连接键就不许模糊 join**、**PROD 缺 ext 时要诚实降级不许跨环境补齐**、
> **Common Knowledge 不能整篇进提示词**。设计层面我只有四点不同意，写在
> `docs/specs/mdc-knowledge-layer-zh.md` §10，不在这份里重复。
>
> **这一轮不要写任何构建代码。** 只用手上已经有的 `Use_Case_Metadata.xlsx` 和五张 PROD CSV
> **读一遍、数一遍**，回答下面 6 个问题。
>
> **为什么先做这个**：Q1~Q3 是三张挂了一个多月的"等业主"单子。答案有可能就在你们已经拿到的
> 那份 workbook 附件里 —— 如果是，我们等于零成本解掉三个阻塞项；如果不是，"文档也没定义"
> 本身就是可以交给业主的结论，比"我们没查过"强得多。
>
> 预计 40~60 分钟。**只读，不写 `index/`，不动 active。**

---

## 一、Q1~Q3：workbook 能不能解掉我们三个硬编码的语义缺口

我们代码里现在写死了一批业务语义，来源是**业主转述的照片**，有三处是空的：

| 缺口 | 现状 |
|---|---|
| `send_mode = 0` | 903 行挂 `pending`，含义未知 |
| `business_category` 33 / 37 | 数据字典（停在 7）和 Java 枚举**都没有**，但生产数据里 status=Y |
| `delivery_path` 的数字 | 我们连 code→name 都没有，答案里只能原样回显数字 |

请在 `Use_Case_Metadata.xlsx` 的 `reference` 页 + `Reference_Data` 页里查这三个字段。

**每个字段请按这个形状回报**（缺就写缺，不要补全）：

```
字段：send_mode
在哪个 sheet / 哪一行：
semantic 列原文：
options 列原文（**逐字抄，不要归纳**）：
B/D/M/S：
derive rule / based-on 列原文：
0 这个取值是否出现在 options 里：是 / 否 / options 列为空
```

**三条纪律**（都是我们已经犯过的错，不是客套话）：

1. **`options` 列请逐字抄原文，不要替我们翻译或归纳。** 上一轮我给你们的示例 JSON 里编了值，
   照抄就会造出业务语义 —— 同一个坑不踩第二次。
2. **空 / `TBC` / 占位符不算答案。** 写"该单元格为空"，不要写成"未定义"（那是个结论）。
3. **不要因为 workbook 说了什么就去改 `config/*.json` 里业主已经确认过的值。** 这轮只回报。

---

## 二、Q4：8 条"体检项"在这批 PROD 数据里到底可不可判

Common Knowledge 里有一批陈述句，而你们手上的 PROD 表里正好有对应的列。我们想把它们变成
**"文档这么说、数据里 N 条不符合"** 的清单交给业主（不是判对错，是标矛盾）。

先确认**列在不在、值判不判得了**。请对下面每一条回一行：

| # | 检查 | 需要的列 | 请回报 |
|---|---|---|---|
| 1 | 高风险但没开双厂商 | `high_risk_flag`, `support_dual_vendor` | 列在否 / 取值域 / 命中行数 |
| 2 | 高风险但没填客户旅程 | `high_risk_flag`, `customer_journeys_num` | 同上 |
| 3 | 声明双渠道但只有一条渠道规则 | `is_dual_channel` + channel_rule 行数 | 同上 |
| 4 | 非 SMS 用例带 China flag | `Send_to_China_flag`, `channel` | 同上 |
| 5 | SMS/Email 缺 `compose_template_mode` | 两列 | 同上 |
| 6 | `obsolete_message` 与 status 互相矛盾 | 两列 | 同上 |
| 7 | 开了 bounce-back 转下一渠道但只有一个渠道 | `bounce_back_next_channel` + 渠道数 | 同上 |
| 8 | 同一用例 `traffic_percentage` 合计≠100 | `traffic_percentage`, 分组键 | 同上 |

**"取值域"请给实际出现的 distinct 值和各自行数**（例如 `Y=131 / N=402 / 空=59`），
不要只回"有这一列"。**行数是这轮最有用的输出** —— 它直接决定哪几条值得做。

> ⚠️ 只要低基数的枚举列。自由文本列（`remarks`、`template_description`、任何人名）**不要抽样、
> 不要回报内容**，只回"该列存在、为自由文本"。

---

## 三、Q5：`use_case_type` 的 `C`

Common Knowledge 第 4 节给了一张 use_case_type 码表：
`M=Event Trigger`、`A=Adhoc`、`E=Defined by eAlert`、`K=Marketing`、`T=Auto Test`、
`I=HSBC Insurance`、`B=HSBC BM`、**`C` 写的是"digits-only"** —— 这句话不像一个业务类型，
更像是提取时把某个说明抄串了。

请回报：
- workbook 里 `use_case_type` 的 options 原文；
- PROD `tbl_use_case` 里 `use_case_type` 实际出现的 distinct 值 + 各自行数；
- `C` 在生产数据里有没有出现、有多少行。

**这张表如果确认得下来，我们就能把答案里的 `use_case_type=M` 直接显示成
"M = Event Trigger（系统触发）—— 来源：metadata 文档，非生产验证"** 。这不是猜语义，
是带出处和权威标签地引用一份文档。

---

## 四、Q6：两处硬编码的 UAT 文案（你们设计文档 §5.6 已经定位）

确认一下我这边理解的位置对不对，以及是不是只有这两处：

- `retriever/usecase_catalog.py` 里，caveat 文案会把**任意** dataset 描述成
  `<env> snapshot — indicative, NOT production`；
- builder 那边的 mixed-export warning 含 `indicative UAT evidence only`。

请回报：
1. 全仓 grep `indicative` / `UAT` 的**完整命中清单**（文件 + 行号，不用贴内容）；
2. 其中哪些是**环境无关**的措辞（可以留），哪些是**写死 UAT**的（必须改）。

正确口径是：`environment=PROD` 说明数据来自生产配置导出，`production_verified=false` 说明它不是
实时运行结果 —— **两者同时出现才完整**，不能因为后者是 false 就改口叫 UAT。

---

## 五、回报格式

一个文件，`index/reports/` 下面，或者直接贴。**不要回传原始数据行。**

```
Q1 send_mode=0        : 解得开 / 解不开（+ 上面那张表）
Q2 business_category  : 33= / 37= / 都没有
Q3 delivery_path      : 有 code→name / 没有
Q4 体检项             : 逐条 列在否 + distinct 取值 + 命中行数
Q5 use_case_type C    : options 原文 + 生产 distinct + C 的行数
Q6 UAT 文案           : 命中清单 + 哪些必须改
另外：这轮有没有发现我在 spec 里写错的东西（有的话请直接说，前几轮都是你们抓出来的）
```

---

## 六、这轮**不要**做的事

- 不要建 `index/mdc-knowledge/`（那是 Q1~Q5 有结论之后的事）
- 不要跑 PROD candidate build
- 不要动 `active`
- 不要根据 workbook 去改任何**业主已确认**的 `config/*.json`
- 不要为了"补全"去手工拼一张同名 CSV（你们设计文档 §18.2 已经写了这条，我完全同意）

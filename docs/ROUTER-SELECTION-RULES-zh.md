# 人工路由选择规则(转述) —— 解释性资料,**不是权威配置**

> **来源:** 消息团队同事 2026-08-06 在 Teams 里写下的操作指引,由项目负责人转述。
>
> ## ⚠️ 这份资料的地位,先说清楚
>
> 这是**一个人写的操作指引**,不是从数据库导出的配置表。它的正确用法只有一种:
> **解释我们在数据里观察到的现象**。
>
> **绝对不能**用它去推断任何一条具体用例的厂商。原因很简单 —— 它写的是"配的时候应该怎么选",
> 而实际配成什么样只有 `tbl_use_case_router` 知道,两者可能不一致,而不一致恰恰是我们要发现的东西。
> 拿规则去填数据,等于把"应该"当成"实际",我们会失去发现问题的能力。
>
> 所以:**这份文档里的厂商名一个都没有进代码,也没有进 `vendor_display_aliases` 配置。**
> 厂商别名映射依然是空的,依然等业主正式签认(见 `retriever/usecase_router.py` 模块注释)。

---

## 原文规则

### PUSH —— 看 app name

| app name | 要建几个 router | 具体 |
| --- | --- | --- |
| `DaaSC` | **3 个** | IOS + AWS-SG(100% − traffic percentage)<br>AOS + AWS-SG(100% − traffic percentage)<br>AOS + AURORA(100% − traffic percentage) |
| `InvestEx` 或 `DBB` | **1 个** | AWS-SG |

**明确排除:** `would NOT choose ICCM related router`。

### SMS —— 看 telecom / delivery mode

**明确排除:** `can ignore the ICCM related routers, and HUTCHISON_GW related router`。

| 情况 | 选什么 |
| --- | --- |
| 非高风险 | 优先 **HTCL**,按 delivery mode 选 **RT** router |
| **高风险** | **双厂商 HTCL + CSL,主 100% HTCL & CSL 0%**;按 delivery mode(是否 OTP)选 **TC** 或 **RT** router |
| 发往中国大陆 | **LX(尚未就绪)** 和 **CM**,主 **100% CM & LX 0%** |

### Email

| 情况 | 选什么 |
| --- | --- |
| new & external | `PFP_EU` |
| new & internal | `INT` |

### Letter

| 情况 | 选什么 |
| --- | --- |
| auto trigger | `OTX_BATCH_LETTER` |
| manual trigger | `OTX_BAT_HTML_LETTER` |

### 2WAYSMS

固定选 `HTCL_2WAY_SMS`。

> 原文末尾还有一句待办提问:`whether this use case can send SMS to HK & CN?` —— 这是他们内部的
> 待确认项,不是给我们的。

---

## 这份资料回答了我们三个悬而未决的问题

### ① `traffic_percentage = 0` 到底是什么 —— 🔴 这条推翻了我之前的实现

**0% 不是"关掉了",而是「双厂商里的备用方」。**

> `if message is high-risk, choose dual vendor with HTCL and CSL,`
> **`primary 100% HTCL & 0% for CSL`**

> `if need to send to CN, choose LX (not yet ready) and CM routers,`
> **`primary 100% CM & 0% for LX`**

CSL 挂着 0%,**正是 HTCL 挂掉时接管的那一家**。我上一轮把 0% 写成
"configured but NOT sending, must not be counted as live" —— **在故障场景下这是危险的错误**:
问"HTCL 挂了谁接管",我会把唯一的答案删掉。

**已修正**(见 `retriever/traffic.py` 模块注释):

- 0% 的语义改为「**已配置,当前不承载流量**」,并明确它有**三种无法区分**的可能:
  双厂商备用(CSL)、已登记但**未就绪**(LX,原文明说 `not yet ready`)、真正停用。
- 新增 `standby` 字段,和 `sends` 分开。`sends` 只回答"现在有没有在发",
  **不得**被提升成"这条渠道不相关"。
- 报告里对 0% 渠道的措辞改成:**故障问题必须包含它们**,只有"现在谁在发"这类问题才排除。
- 新增 `has_standby`:**一条正在发的渠道背后也可能挂着 0% 的备用**(100% HTCL + 0% CSL 是
  同一个渠道),只看渠道级结论会把备用完全藏起来。

> 这和 `rule_text` 的 `>` fallback 是**同一个形状**:平时不发、故障时正是它接管。
> 同一个原则第二次出现,现在两处的注释互相指认。

### ② 为什么权威厂商覆盖率只有 27.32%

因为**整族 router 是被刻意跳过的** —— `would NOT choose ICCM related router`、
`can ignore the ICCM related routers, and HUTCHISON_GW related router`。

而且有些渠道的厂商**根本不由这一列决定**:PUSH 看 `app name`,SMS 看 `telecom / delivery mode`,
Letter 看是 auto 还是 manual trigger。

**所以"匹配上了 router 行但 vendor 是空"是一种预期形状,不是数据缺陷。**
业主 2026-08-06 明确决定:那 244 行**不报成数据质量异常**。已照此实现 ——
只报计数,不报严重级,措辞里去掉了 "unexplained"。

### ③ TC / RT router 是什么

原文里 `choose TC or RT router` 按 delivery mode(是否 OTP)决定。这和我们已经解出来的
`delivery_path` 分级(Time Critical / Real Time Express / Real Time Standard / Batch)**看起来
是同一套东西**,TC=Time Critical、RT=Real Time。

**但这是我的推测,没有落进代码。** `delivery_path` 的解析依然只认
`DeliveryPathEnum.java` 那份代码证据。这条记在这里只是备查。

---

## 🔴 一个必须防住的命名陷阱:`HTCL` ≠ `HUTCHISON_GW`

原文在同一段里把这两个当成**不同的东西**:

> `can ignore the ICCM related routers, and` **`HUTCHISON_GW`** `related router`
> `- if message is non-highrisk, priority to choose` **`HTCL`**

一边要忽略 `HUTCHISON_GW`,一边要优先选 `HTCL`。如果它们是同一家,这句话自相矛盾。

`HTCL` 看起来像 "Hutchison Telecom" 的缩写 —— **但看起来像不等于是**。这正是 RUNBOOK-49/51
反复踩过的坑(`htcl` → `3hk` 那次别名合并),也正是 `usecase_router` 至今**不发放任何默认厂商
别名**的原因。

**结论:一个字都不动。** 不建立 `HTCL` ↔ `HUTCHISON_GW` 的任何关联,不把任何一个折叠到
`3hk`。等业主正式签认厂商别名表。

---

## 出现过的名字(仅登记,不解释、不映射)

`AWS-SG` · `AURORA` · `ICCM` · `HTCL` · `CSL` · `LX` · `CM` · `PFP_EU` · `INT` ·
`OTX_BATCH_LETTER` · `OTX_BAT_HTML_LETTER` · `HTCL_2WAY_SMS` · `HUTCHISON_GW`

app name:`DaaSC` · `InvestEx` · `DBB`

**这些名字没有进任何配置文件。** 登记在这里,是为了将来业主签认厂商别名表时,我们知道
需要问哪些名字 —— 而不是为了现在拿来用。

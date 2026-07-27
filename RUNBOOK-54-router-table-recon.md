# RUNBOOK-54 —— `tbl_use_case_router` 侦察(建代码之前先摸清 7 件事)

> **为什么先侦察不直接建:** 这张表的表头信息量很大,但**它怎么和现有三张表对上号,只能从数据里看**,
> 猜错会造成静默错连(Round A 就踩过一次跨环境错连,那是当时最严重的 P0)。
> 本 runbook 只读不写,回答 7 个问题,然后我按答案建摄取代码。
>
> **谁做都行**:表在 Excel 里的话,项目负责人自己筛几下就能回答大半;在盒子上则由内网 Codex 跑。
> **回报的是取值和计数,不是整表** —— 不要贴数据行,不要贴人名(`created_by`/`last_modified_by` 一律不用回报)。

已知表头:

```
id / channel / route / router / vendor / message_process_sla / message_delivery_sla /
delivery_path / created_by / created_time / last_modified_by / last_modified_time / business_category
```

---

## 问题 1 ⭐最关键 —— 这张表怎么和用例对上号?

现有的 `tbl_use_case_channel_rule` **也有 `route` 和 `router` 两列**。所以最可能的连法是:
`channel_rule.route`(或 `.router`)的值 = `tbl_use_case_router.id` 的值。**但必须验证。**

**要回报:**

| 项 | 怎么看 |
| --- | --- |
| `router.id` 的样子 | 前 5 个不同取值(是数字?字符串?形如 `R001`?) |
| `channel_rule.route` 的样子 | 前 5 个不同取值 |
| `channel_rule.router` 的样子 | 前 5 个不同取值 |
| **匹配率** | `channel_rule.route` 的取值有多少 % 能在 `router.id` 里找到?`channel_rule.router` 呢? |
| `router.route` / `router.router` | 这两列的取值样子(可能是名称而不是外键) |

**判断标准**:哪一列的匹配率接近 100%,哪一列就是外键。两列都高 → 告诉我,可能是复合键。
两列都低 → 说明连法不是这个,把你看到的取值贴回来,我重新设计。

## 问题 2 ⭐ —— `vendor` 列的取值域(**这是我们等了很久的一列**)

MDC 仓库清单表里**没有厂商列**,所以"某厂商挂了影响谁"一直只能靠仓库命名反推。这一列是权威来源。

**要回报:** `vendor` 列去重后的**全部取值 + 每个的行数**。

拿到后我会和代码里现有的厂商白名单对照:

```
csl, sinch, 3hk, cm, lx, aurora, awssg, awshk, pfp, pps, sfmc, iccm, otx, haro, sns, apns, fcm
```

**特别注意**:表里出现但白名单里没有的厂商 —— 那就是我们一直漏掉的;
白名单有但表里没有的 —— 可能是已下线或者命名不同。两个方向都要。

## 问题 3 —— `delivery_path` 长什么样?

**要回报:** 去重后前 10 个取值 + 总共多少种。

我要判断它是:消息 topic 名?仓库名?接口路径?还是一串分隔符拼的路径?
**这一列很可能就是链路图缺的那一跳**,所以取值样子很重要。

## 问题 4 ⭐ —— `business_category` 在这张表里是什么情况?

这条直接关系到之前挂起的"33/37 是什么"的问题。

**要回报:**
1. 这一列去重后的**全部取值 + 每个的行数**
2. **33 和 37 在不在里面?**
3. 取值是**数字编号**还是**文字名称**?(如果是文字名称,那 33/37 的答案可能就在这张表里,不用去问人了)
4. 同一个用例,这张表的 `business_category` 和 `tbl_use_case` 那一列**是否一致**?不一致的有多少条?

> ⚠️ 现在两张表都有这一列,**必须搞清楚以哪张为准** —— 这和 rule_text vs priority 是同一类问题,
> 不能猜。如果不一致的条数不为零,这会变成下一个要问业务方的问题。

## 问题 5 —— `channel` 取值域

**要回报:** 去重取值 + 计数。对照代码里的渠道词表:

```
SMS, EMAIL, PUSH, LETTER, WHATSAPP, WECHAT, MMS, TWOWAYSMS, INAPP, PUSH_INBOX / PUSH+INBOX
```

出现新渠道要单独指出来。

## 问题 6 —— 两个 SLA 列的单位

**要回报:** `message_process_sla` / `message_delivery_sla` 各自的取值范围(最小/最大/几个样例)。

我要判断单位是毫秒、秒还是分钟 —— 单位猜错会让"时效性"类问答给出错误答案。

## 问题 7 —— 基本体检

**要回报:** 总行数;`id` 是否唯一(有无重复);各列的空值率(哪几列大面积为空)。

---

## 回报格式

```
1 连接方式 : router.id 样例=___ ; channel_rule.route 样例=___ 匹配率=__%
             channel_rule.router 样例=___ 匹配率=__%
2 vendor   : 取值+计数(全部)
3 delivery_path : 共__种,前10个取值
4 business_category : 取值+计数;33/37 在否=__;数字还是名称=__;与 tbl_use_case 不一致__条
5 channel  : 取值+计数;新渠道=___
6 SLA      : process 范围___ ; delivery 范围___
7 体检     : 行数___ ; id 唯一=是/否 ; 大面积为空的列=___
```

**不要回报:** 数据行本身、`created_by`/`last_modified_by` 的人名、任何自由文本备注。

---

## 拿到答案之后我会做什么(让你知道这次侦察换来什么)

1. **建 `tbl_use_case_router` 摄取** —— 列映射放进 `config/usecase_columns.json`,以后表改了由内网 Codex 改配置即可(按 `AGENTS.md` 的分工)。
2. **补上链路图缺的那一跳** —— 用例 → 渠道 → **route/router → vendor → delivery_path** → 投递仓库 → 客户。这是 Demo 档 2 的主角。
3. **厂商口径升级** —— 从"靠仓库名反推"变成"权威表直读",`outage_report`(某厂商挂了影响谁)的准确度会明显提高。
4. **33/37 的问题**要么就地解决(如果这张表带名称),要么变成一个更精确的问题去问业务方。
5. 如果两张表的 `business_category` 不一致 → 按 rule_text 的先例,**报告矛盾、标出待确认,绝不自己选边**。

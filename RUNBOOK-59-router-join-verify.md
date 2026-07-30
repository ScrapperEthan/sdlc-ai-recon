# RUNBOOK-59 —— 验证 `tbl_use_case_router` 四列连接(外部实现 vs 内网 SQLite,两边对数)

> **为什么值得单独跑一次:** 内网用 SQLite 做过一次这个连接,得到 **2967/5959 = 49.79%**。
> 外部现在用 Python 独立实现了**同一个连接**(`retriever/usecase_router.py`)。
> 两个独立实现算同一个数 —— **对不上就说明有一边错了**,这是这份 runbook 唯一真正重要的检查。
> 连接错了不会报错,只会静默给出别人家厂商的答案(RB-54 当初就是为了防这个)。
>
> **只读。** 不改数据、不改配置、不跑 `refresh.py`。
> **回报的是计数和分布,不是数据行。** 不要贴 use_case_id 清单,不要贴人名。

先 `git pull`(需要 `usecase_router.py`,提交在 `50deee3` 之后)。

---

## 检查 1 ⭐⭐ 两边对数 —— 唯一的关键项

```bash
python -c "
from retriever import usecase_master as um, usecase_router as ur
idx = ur.index_by_natural_key()
print('router rows      :', idx['row_count'])
print('key fields       :', idx['key_fields'])
print('unbound key cols :', idx['unbound_key_fields'])
print('rows w/ 不完整key :', idx['rows_with_incomplete_key'])
rules = um.rules_by_use_case_id()
masters = {}
for uc in rules:
    m = um.master_for(uc)
    masters[uc] = (m or {}).get('business_category_code') or ''
tot = matched = vendor_present = incomplete = nomatch = 0
for uc, rows in rules.items():
    for r in rows:
        tot += 1
        got = ur.router_for_rule(r, masters[uc], index=idx)
        if got['matched']:
            matched += 1
            if got['vendor']['present']: vendor_present += 1
        elif got.get('missing_key_fields'): incomplete += 1
        else: nomatch += 1
print('child rows       :', tot)
print('matched          :', matched, '=> %.2f%%' % (100.0*matched/max(1,tot)))
print('  of which vendor非空:', vendor_present, '=> %.2f%%' % (100.0*vendor_present/max(1,matched)))
print('miss: 不完整key   :', incomplete)
print('miss: 键无对应行  :', nomatch)
"
```

**要回报这 9 个数字。** 判定标准:

| 项 | 期望 | 对不上说明什么 |
| --- | --- | --- |
| `router rows` | **247** | 数据集不是那份快照 |
| `key fields` | `business_category, channel, route, router` | `config/usecase_columns.json` 的 `router_natural_key` 和口头不一致 |
| `unbound key cols` | **空** | 列名绑定失败 → 列映射要补 alias |
| `matched` | **2967**(49.79%) | ⚠️**两个实现不一致,必须查清**。差一点点 → 大小写/空格处理不同;差很多 → 连接语义不同 |
| `vendor非空` | **1628**(54.87%) | 同上 |
| `child rows` | **5959** | 分母不同 → 内网那 5959 不是"channel_rule 全量",要说明它到底是什么 |

> `child rows` 如果不是 5959,**这一条本身就是发现**:请说明 `tbl_use_case_channel_rule` 是 6217 行,
> 而 5959 是怎么来的(是不是过滤了 `status`?还是四列都非空的行数?)。

## 检查 2 —— 方法分档的真实分布(这决定我们对外怎么说话)

```bash
python -c "
from retriever import usecase_master as um
import impact_report, collections
c = collections.Counter(); ch = collections.Counter()
ids = list(um.rules_by_use_case_id())[:400]
for uc in ids:
    try: rep = impact_report.build_report('use-case:'+uc)
    except Exception: continue
    for item in (rep.get('delivery_chain') or {}).get('by_channel') or []:
        c[item['vendor_selection']['method']] += 1
        ch[item['channel']] += 1
print('sample use cases :', len(ids))
print('methods:', dict(c))
print('channels:', dict(ch))
"
```

**要回报:** `methods` 和 `channels` 两个字典。

预期(我的推算,**可能错**):`router_table` = **0**(厂商别名还没确认,所以一条都不该升到这档);
`router_table_unconfirmed_alias` 只出现在 push / sms;email/letter/whatsapp/wechat **全部**落在
`channel_upper_bound` 或 `route_hint`。

⚠️**如果 `router_table` 不是 0** —— 说明盒子上的 `config/usecase_columns.json` 里已经有
`vendor_display_aliases`,或者代码在别处猜了别名。**请立刻回报**,这是我最担心的一种错。

## 检查 3 —— 抽 3 条人工核对(防"连上了但连错了")

从检查 2 里挑 **3 个 `router_table_unconfirmed_alias` 的用例**,对每个回报:

```
use_case_id / channel / route / router / business_category
→ 命中的 router.id / router.vendor 原文
```

然后**在原始 router CSV 里用这四列手工查一遍**,确认命中的是同一行。
这一步是在验"代码连的那一行,和人眼连的那一行是同一行"。

## 检查 4 —— 陈述的诚实性(不需要跑数据)

在真实聊天里问一句:**`M2050 的完整配置与渠道`**,以及一个**你知道有 vendor 的 push 用例**。
回报答案里是否出现了这几件事:

- [ ] 没有把 `HTCL` 说成 `3hk`,也没有把 `AWS HK SNS` 说成 `sns`(**这是硬性红线**)
- [ ] `HTCL OLD` 没有被当成 `HTCL`
- [ ] SLA 数字没有被写成"5 秒 / 5 分钟"之类带单位的说法
- [ ] `delivery_path` 只报数字,没有编出文字路径名
- [ ] 提到了"四张表不是同一时刻导出"或跨时间连接的风险
- [ ] email/letter 类问题老实说了是渠道级上界

任何一条打不上勾 —— 回报原话,那是 prompt/工具描述要改,不是数据问题。

---

## 顺便,一个只有你能答的问题

`tbl_use_case_channel_rule` 有没有自己的 `business_category` 列?

```bash
python -c "
from retriever import usecase_master as um
r = next(iter(um.rules_by_use_case_id().values()))[0]
print(sorted(r))
"
```

有的话,连接就是**两张表**的事,`tbl_use_case` 不必在场 —— 那么**没有主数据行的用例(比如 M2050)
也能拿到权威厂商**,覆盖率会比现在高。没有就维持现状。这一条直接影响要不要改代码。

---

## 回报格式

```
1 对数   : rows=___ key=___ unbound=___ incomplete_key_rows=___
           child=___ matched=___(__%) vendor非空=___(__%) 不完整key=___ 键无对应=___
2 分布   : methods={...} channels={...}
3 抽查   : 3 条,逐条列 四列值 → router.id / vendor原文 / 手工核对是否同一行
4 诚实性 : 6 个勾选项,打不上勾的贴原话
5 附加   : channel_rule 有没有 business_category 列?列名清单
```

**不要回报:** use_case_id 清单、vendor 之外的业务字段、任何人名。

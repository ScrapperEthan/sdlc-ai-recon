# RUNBOOK-76 (INTERNAL Codex) — verify the four fixes your RUNBOOK-75 run produced

> **只读**,预计 10 分钟。这一轮**没有新功能**,只验证你们上一轮回报直接促成的四个改动。
>
> 你们上一轮的回报里有四条实测发现,每一条都改了代码:
> 1. 我的任务 C 脚本漏传 master `business_category` → 0 匹配(引擎是对的,脚本是错的)
> 2. 244 行「有流量、无厂商」需要在报告摘要里能看见
> 3. `send_mode` 的 0/4/5 没有定义,而 0 有 903 行
> 4. `repos_with_any_meaning` 是饱和指标,量不出进展
>
> 结论汇总在 `docs/RUNBOOK-75-FINDINGS-zh.md`。
>
> **回报纪律不变:** 聚合计数,不贴数据行,不贴人名。

---

## 先决条件

```bash
git pull && python -m pytest tests -q
```

期望 **1458 passed**。

---

## ⭐ A0 —— 最重要的一条:0% 是**备用厂商**,不是"关掉了"

> 你们上一轮回报之后,业主又给了两张图,其中一张是消息团队写的路由选择规则。里面这两句
> **推翻了我上一轮的实现**:
>
> > `if message is high-risk, choose dual vendor with HTCL and CSL,`
> > **`primary 100% HTCL & 0% for CSL`**
> > `if need to send to CN, choose LX (not yet ready) and CM routers,`
> > **`primary 100% CM & 0% for LX`**
>
> **0% 是刻意配置的第二家厂商 —— 正是主厂商挂掉时接管的那一家。**
> 我上一轮写成 "must not be counted as live",等于问「HTCL 挂了谁接管」时**把唯一的答案删掉**。
> 已修正,但**这个错只在真实故障时才暴露**,所以要在真机上确认一遍。

### A0-1 语义已改

```bash
python -c "
from retriever import traffic
r = traffic.read('0')
print('sends:', r['sends'], '| standby:', r['standby'])
print('note has takeover:', 'takes over' in r['note'])
print('note refuses to pick a cause:', 'cannot tell the three apart' in r['note'])
"
```

**期望:** `sends: False | standby: True`,后两行都是 `True`。

### A0-2 ⭐ 双厂商:正在发的渠道背后也挂着备用

这是最容易被漏掉的一种 —— `100% HTCL + 0% CSL` 是**同一个渠道**,渠道级结论是"在发",
如果只看那一层,**备用完全看不见**。

```bash
python -c "
from retriever import traffic
rows = [{'traffic_percentage':'100'}, {'traffic_percentage':'0'}]
e = traffic.verdict(rows, 'SMS')
print('sends:', e['sends'], '| has_standby:', e['has_standby'], '| standby_rules:', e['standby_rules'])
"
```

**期望:** `sends: True | has_standby: True | standby_rules: 1`。

### A0-3 真实数据:到底有多少双厂商对

```bash
python -c "
from retriever import usecase_catalog as uc, traffic
pairs = live_only = standby_only = 0
for ucid, rules in uc.rules_by_use_case_id().items():
    for e in traffic.summarise(rules):
        if e['sends'] is True and e['has_standby']: pairs += 1
        elif e['sends'] is True: live_only += 1
        elif e['sends'] is False: standby_only += 1
print('dual-vendor pairs (live + a 0% standby behind it):', pairs)
print('live with no standby:', live_only, '| standby-only channels:', standby_only)
"
```

**回报:** 三个数字。

**怎么读:** `dual-vendor pairs` 就是**以前会被完全藏起来的备用路由数量** —— 上一轮我只统计了
"整条渠道都是 0%"(37 个),这一类根本没进统计。如果这个数字明显大于 37,
说明这次修正救回来的东西比我原先以为的多。

### A0-4 报告措辞

挑一条含 0% 路由的用例:

```bash
python impact_report.py use-case:<挑一条> --out ""
```

**期望**在 delivery chain 段看到 `**standby**` 或 `**dual-vendor**` 开头的行,并且里面明确说
**故障问题要包含它们**。**不应该**再出现 "do not count these as live" 这类措辞。

**回报:** 那一两行原样。

---

## A —— 三表连接的回归测试(你们抓到的那个)

```bash
python -m pytest tests/test_traffic_and_enums.py::ThreeTableJoinTest -q
```

期望 **4 passed**。这四条钉住的是:子行没有自带 category 时**必须**有 master category 才能匹配;
以及**绝不能**靠丢掉空列来"修好"匹配。

按你们的要求,**没有任何快照数字进测试** —— 2,967 这类数字下次导出就会变,写进测试就是定时炸弹。

**回报:** passed 数。

---

## B —— 修正后的任务 C 脚本现在能跑出正确结果

重跑 `RUNBOOK-75` 任务 C 那段(已按你们的诊断修好,现在会先建
`use_case_id → business_category` 映射并打印 `master categories loaded`)。

**回报:** `master categories loaded` 和 `matched child rows` 两个数。
只需确认它**不再是 0**、并且和你们上一轮报的 **2,967** 一致(数字本身我已经记在 findings 里了)。

---

## C —— 244 那批现在在报告摘要里看得见

挑一条有权威 router 行、且 vendor 为空的用例:

```bash
python impact_report.py use-case:<挑一条> --out ""
```

在「delivery chain」那一段应该能看到一行形如:

```
- blank-vendor rows checked against traffic_percentage: N carrying traffic with no carrier recorded / M standby (0%) / K undecidable — **not a data-quality exception**
```

**回报:** 这一行的原样输出(用哪条用例你们自己挑,不用告诉我 id)。

**⚠️ 措辞已按业主 2026-08-06 的决定改过,请顺便确认两件事:**

1. **不应该**再出现 `unexplained` 这个词 —— 业主明确说这批**不是数据质量异常**
   (路由规则里整族 router 是刻意跳过的,有些渠道的厂商根本不由那一列决定)。
2. caveat 里**仍然必须**有「**NOT evidence of which carrier it is**」——
   无论这批算不算异常,「空 vendor 不构成推断出是哪家的证据」这条禁止项都不变。

> 这两条不矛盾:**降级的是严重性,不是那条推断禁令。**

---

## D —— `send_mode`:4/5 已解决,只剩 0

> **变了什么:** 完整字典页到手,**4 = Send by separately、5 = Mixed mode**
> —— 第一份节选被截断在 3,所以它俩当时看起来"没定义"。现在只有 **0** 还不知道。
>
> 而且 **0 不再报 warning**。一个 903 行都在用的值,叫它"数据契约漂移"是过度指控 ——
> 它现在是**第三态 `pending`**(合法、在用、含义未提供),报 **info**。
> 只有真正意料之外的编号才报 warning。

```bash
python -c "
from retriever import usecase_catalog as uc, usecase_consistency as ucc
for c in ('0','1','2','3','4','5','99'):
    r = uc.resolve_send_mode(c)
    print(c, '-> known:', r['known'], '| pending:', r['pending'], '|', repr(r['label']))
q = ucc.quality_findings(limit=0)
if q.get('available'):
    for f in q['findings']:
        if f['check'] in ('send_mode_pending_meaning', 'undefined_send_mode'):
            print(f['check'], '|', f['severity'], '|', f['message'][:200])
"
```

**期望:**

- `1`–`5` 全部 `known: True`(4 = `Send by separately`,5 = `Mixed mode`)
- `0` → `known: False, pending: True`
- `99` → 两个都 False(真正的意料之外)
- findings 里出现 **`send_mode_pending_meaning` / info**,列出 `0 (N row(s))`
- **不应该**再有 `undefined_send_mode` 提到 4 或 5

**回报:** 全部输出。如果行数和你们上一轮报的分布对不上,**先怀疑我的统计口径**,把两边都贴回来。

### D2 ⭐ 顺带:5 = Mixed mode 解锁了一件事

以前混合表达式(`(SMS > EMAIL) & PUSH` 这种)**根本没法交叉验证** —— 没有哪个编号叫"混合",
所以既不能说它一致也不能说它不一致,只能跳过。现在 5 就是"Mixed mode",**第一次能正面比对**。

```bash
python -c "
from retriever import usecase_consistency as ucc
q = ucc.quality_findings(limit=0)
if not q.get('available'):
    print('dataset absent'); raise SystemExit
n = [f for f in q['findings'] if f['check'] == 'send_mode_vs_rule_text']
print('send_mode vs rule_text disagreements:', len(n))
mixed = [f for f in n if 'MIXED' in f['message']]
print('of which involve a MIXED expression (newly checkable):', len(mixed))
"
```

**回报:** 两个数字。上一轮是 30 条分歧,这一轮**可能会变多** —— 变多是**好事**,
说明以前被跳过的混合表达式现在被检查到了。如果暴涨到几百条,那才是我的映射写错了,把示例贴回来。

---

## E —— 词典指标现在会动了

```bash
python -c "from retriever import glossary; r=glossary.write_coverage('index'); print('slots', r['token_slots_decoded'], '/', r['token_slots'], '| fully', r['repos_fully_decoded'], '/', r['repos_measured'], '| any', r['repos_with_any_meaning'])"
```

**回报:** 这一行。

现在应该能看到:`any` 还是 459/460(饱和,意料之中),但 `slots` 和 `fully` 是**有区分度的**。
`GLOSSARY_COVERAGE.md` 的表头也换成了以这两个为主,并明确标注 `any` 不要读作进展。

**可选(有余力再做):** 再填 10 个 token,看 `slots` 和 `fully` 动了多少。这是为了确认新指标
**真的**能反映工作量 —— 如果它们也几乎不动,那说明我又选错了,请直接说。

---

## F —— 你们能自己写枚举答案了,不用等我

业主一旦回答 `send_mode` 的 0/4/5 是什么,**不需要等我推送**:

```bash
# 在盒子上创建(gitignored,自动优先于被跟踪的那份)
config/business_enums.local.json
```

内容只需要写你们要改的那一节,例如:

```json
{
  "send_mode": {
    "data_dictionary": { "0": "<业主给的名称>", "4": "...", "5": "..." },
    "rule_text_equivalent": { "0": "parallel_all" }
  }
}
```

**这是替换不是合并** —— 但**每一节各自独立回退到代码里的默认值**,所以一个只写 `send_mode` 的
本地文件**不会**把 `business_category` 清空。请顺手验证这一点:

```bash
python -c "
from retriever import usecase_catalog as uc, config
print('config in effect:', config.BUSINESS_ENUMS_JSON)
print('bc 6 ->', uc.resolve_business_category('6')['source'], uc.resolve_business_category('6')['label'])
print('send_mode 0 ->', repr(uc.resolve_send_mode('0')['label']))
"
```

**回报:** 这三行(创建 local 文件前后各一次更好)。

> 之所以做这个出口:你们**不能 push**,在被跟踪的文件上就地编辑会让下一次 `git pull` 被拒 ——
> 一个配置文件挡住同一次 pull 里所有不相关的修复。和 `db_queries.local.json` 是同一套安排。

---

## 回报模板

```
先决条件 pytest:            [ N passed ]
A0-1 sends/standby:         [ ... ]
A0-2 dual-vendor verdict:   [ ... ]
A0-3 pairs / live / standby: [ ... ]
A0-4 报告措辞:               [ 原样 ]
A 三表连接回归:              [ N passed ]
B master loaded / matched:  [ ... / ... ]
C 摘要行:                    [ 原样 ]  caveat 含 "NOT evidence": [ 是/否 ]
D undefined_send_mode:      [ 原样 ]
E slots / fully / any:      [ ... ]
E 可选 再填 10 个后:         [ slots 从 X 到 Y, fully 从 X 到 Y ]
F local 出口验证:            [ 三行 ×2 ]
```

---

## 业主已答复的(本 runbook 已按新答案写)

- ✅ **244 行不报成数据质量异常** —— 路由规则里整族 router 是刻意跳过的,有些渠道的厂商根本不由
  那一列决定。已改成只报计数、无严重级。
- ✅ **`send_mode` 4 = Send by separately,5 = Mixed mode**(第一份字典节选被截断了)。
  **5 尤其有用** —— 它就是 `rule_text` 的 MIXED,混合表达式第一次能正面比对。

## 唯一还在等的

🔴 **`send_mode` = 0 是什么意思**(903 行)。现在记为「**已知的未知**」,报 info 不报 warning。

拿到答案后**你们可以自己写**,不用等我推送 —— 见任务 F 的 `config/business_enums.local.json`。

顺带那 **30 条 `send_mode`/`rule_text` 分歧**是历史漂移还是缺陷,也还没答;
在答复之前按 warning 报告并注明 rule_text 为准。

详见 `docs/RUNBOOK-75-FINDINGS-zh.md` 和 `docs/ROUTER-SELECTION-RULES-zh.md`。

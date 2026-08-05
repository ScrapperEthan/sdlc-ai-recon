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

期望 **1445 passed**。

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
- blank-vendor rows checked against traffic_percentage: **N unexplained** (live traffic, no carrier) / M explained (0% route) / K undecidable
```

**回报:** 这一行的原样输出(用哪条用例你们自己挑,不用告诉我 id)。

**同时确认一件事:** 当 `N > 0` 时,caveat 里必须出现「**NOT evidence that a carrier can be
inferred**」这句。这是你们上一轮特别提醒的那条 —— 解释不成立可能是真缺口,**也可能只是导出时刻
不同**,两种都不构成推断厂商的理由。

---

## D —— 978 行未定义的 `send_mode` 不再无声无息

```bash
python -c "
from retriever import usecase_consistency as ucc
q = ucc.quality_findings(limit=0)
if not q.get('available'):
    print('dataset absent'); raise SystemExit
for f in q['findings']:
    if f['check'] == 'undefined_send_mode':
        print(f['severity'], '|', f['message'])
"
```

**期望:** 一条 warning,列出 `0 (903 row(s)), 4 (3 row(s)), 5 (72 row(s))` 这样的编号和行数。

**回报:** 这一行。如果行数和你们上一轮报的分布对不上,**先怀疑我的统计口径**,把两边都贴回来。

> 交叉验证**仍然跳过**这些未定义编号 —— 不理解的比较比不比较更糟。这条发现的作用就是让被跳过的
> 那部分留下痕迹,而不是消失。

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
A 三表连接回归:              [ N passed ]
B master loaded / matched:  [ ... / ... ]
C 摘要行:                    [ 原样 ]  caveat 含 "NOT evidence": [ 是/否 ]
D undefined_send_mode:      [ 原样 ]
E slots / fully / any:      [ ... ]
E 可选 再填 10 个后:         [ slots 从 X 到 Y, fully 从 X 到 Y ]
F local 出口验证:            [ 三行 ×2 ]
```

---

## 还在等业主的两件事(不影响本 runbook)

1. **那 244 行要不要默认报成数据质量异常**,还是等四张表同时刻导出之后再说。
   目前实现:**显示数字,不升级成 exception** —— 等同时刻快照到手再定。
2. **`send_mode` 的 0、4、5 是什么**(0 有 903 行),以及那 **30 条 `send_mode`/`rule_text`
   分歧**是历史漂移还是缺陷。

两条都在 `docs/RUNBOOK-75-FINDINGS-zh.md` 第六节,可以直接拿去问。

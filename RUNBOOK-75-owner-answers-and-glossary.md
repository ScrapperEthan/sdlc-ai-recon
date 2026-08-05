# RUNBOOK-75 (INTERNAL Codex) — verify the three 2026-08-05 owner answers, and fill the glossary

> **只读。** 不写任何生产数据,不改被 git 跟踪的文件(唯一的例外是 `index/glossary.json`,
> 它本来就是盒子本地的、被 gitignore 的手写文件)。
>
> **背景:** 业主 2026-08-05 回答了三件事,代码已按答案改完并推送(1425 tests 本地全绿)。
> 这份 runbook 只做一件事:**在真实数据上验证这三条实现是对的**,以及把词典填起来。
>
> **回报纪律(和前几轮一样):** 回报**取值和计数**,不要贴数据行,不要贴人名
> (`created_by` / `last_modified_by` 一律不用回报)。

---

## 先决条件

```bash
git pull && python -m pytest tests -q
```

期望 **1425 passed**。如果这一步就不绿,**先停,把失败贴回来** —— 后面的验证都建立在它之上。

---

## 任务 A —— 词典:先看现状,再填

### A1. 现在盒子上那份 `index/glossary.json` 到底是什么状态

图里看到的是 `mc=TBC ? ???????`。需要先分清是**占位符**还是**存盘时中文被吃掉了**,
因为补救方式不同。

```bash
python -c "import json;d=json.load(open('index/glossary.json',encoding='utf-8-sig'));print([(k,repr(d.get(k))) for k in ('mc','api','common','hase','hk')])"
```

**回报:** 这一行的原样输出。

- 如果看到的是 `'TBC ? ???????'` 这类**字面问号** → 是存盘时丢了中文(用了非 Unicode 代码页)。
  以后编辑这个文件**必须存成 UTF-8**,不要经过 cmd 的 `>` 重定向、不要用记事本的 ANSI。
- 如果这一行直接**报 UnicodeDecodeError** → 文件本身不是 UTF-8。这在旧版本里会让**整个影响报告
  崩掉**;新版本已改成优雅降级(词典当作不存在),但仍然要把文件重存成 UTF-8。

### A2. 确认那串 `?` 已经不会再显示出来

```bash
python -c "from retriever import glossary; print(glossary.expand('mc-hk-hase-api-common'))"
```

**期望:** 输出里**一个 `?` 都没有**。没填的 token 直接不出现,填了的照常显示。

> 这是这次改动的核心:**没填 ≠ 可以假装填了**。宁可少显示,不能显示 `TBC ? ???????` —— 那和
> "关键词没命中却回文件尾部"是同一类问题,读的人分辨不出来。

### A3. 生成填空清单

```bash
python -c "from retriever import glossary; r=glossary.write_coverage('index'); print(r['totals'], r['repos_measured'], r['repos_with_any_meaning'])"
```

然后打开 `index/reports/GLOSSARY_COVERAGE.md`。它按**这个 token 出现在多少个真实仓库名里**排序,
只列没填的。

**回报:**
1. 上面那行的输出(三个状态的计数 + 测量了多少个仓库名 + 有多少个仓库名至少能解出一个词)。
2. `GLOSSARY_COVERAGE.md` 里**前 20 行**的 token 和出现次数(不需要贴含义)。

### A4. 填前 20 个

按清单从上往下填 `index/glossary.json`,**存成 UTF-8**。不用一次填完,前 20 个就能覆盖绝大多数
仓库名。填完重跑 A2 和 A3,回报 `repos_with_any_meaning` 的变化。

> 值可以是中文。只要确认存盘是 UTF-8,中文不会再变成 `?`。

---

## 任务 B —— `>` 的右边:左边失败了才发

### B1. 配置确实生效

```bash
python -c "from retriever import rule_text as rt; print(rt.stage_transition()); print(rt.interpret(rt.parse('LETTER > (EMAIL & SMS)'))['stages_are_live'])"
```

**期望:** `fallback_on_failure` 和 `False`。

`stages_are_live=False` 是这次改动里**比故障场景更重要**的那一半:`LETTER` 正常的时候,
`EMAIL`/`SMS` **根本没在发**。

### B2. 报告里说人话

挑一条真实的、`rule_text` 带 `>` 的用例(例如 `I0141`):

```bash
python impact_report.py use-case:I0141 --out ""
```

**期望**在输出里看到类似:

```
- semantics: **owner-confirmed** (config/rule_text_semantics.json)
  - sends first: LETTER
  - sent together: EMAIL, SMS
  - later stages are **fallback only** — they send when the earlier stage FAILS ...
    - EMAIL sends only if LETTER fails
    - SMS sends only if LETTER fails
```

**回报:** 这一段的原样输出。

### B3. ⭐ 这一条最值得看 —— 影响面数字变了吗

在真实数据上统计:**有多少条用例的 `rule_text` 含 `>`**。这些用例以前会把表达式里的**所有**渠道
都算成活跃渠道,现在不会了。

```bash
python -c "
from retriever import usecase_catalog as uc, rule_text as rt
ext = uc.ext_by_use_case_id()
n = fb = 0
for k, v in ext.items():
    ast = rt.parse(v.get('rule_text') or '')
    if not ast.get('operator_tree'): continue
    n += 1
    if rt.interpret(ast).get('fallback_edges'): fb += 1
print('parseable:', n, 'with fallback stages:', fb)
"
```

**回报:** 两个数字。第二个数字就是"**影响面口径发生变化的用例数**"。

---

## 任务 C —— `vendor` 空值 × `traffic_percentage`

业主的原话是"vendor 是空**基本上**也是因为 percentage 为 0"。代码**没有**把这句话写成规则,
而是**逐行复核**。这一任务就是量出"基本上"到底是多少。

```bash
python -c "
from retriever import usecase_catalog as uc, usecase_router as ur
idx = ur.index_by_natural_key()
print('router index available:', idx['available'], 'rows:', idx['row_count'])
master = {}
holds = {True: 0, False: 0, None: 0}
matched = blank = 0
for ucid, rules in uc.rules_by_use_case_id().items():
    m = None
    for rule in rules:
        r = ur.router_for_rule(rule, (m or {}).get('business_category_code',''), index=idx)
        if not r.get('matched'): continue
        matched += 1
        if 'vendor_blank_explained' in r:
            blank += 1
            holds[r['vendor_blank_explained']['holds']] += 1
print('matched child rows:', matched, 'of which blank vendor:', blank)
print('explanation holds (0% route):', holds[True])
print('explanation FAILS (live traffic, no carrier):', holds[False])
print('undecidable (percentage unreadable):', holds[None])
"
```

**回报:** 全部五个数字。

**怎么读:**

- `holds True` 占绝大多数 → 业主的解释成立,空 vendor 大多是"这条路由本来就不发"。
- 🔴 **`holds False` 才是要给人看的那一批** —— 有真实流量、却没有权威厂商记录。
  这批的数量决定了要不要单独提一个数据问题。**请单独回报这个数字**,哪怕是 0
  (是 0 也是有价值的结论:说明业主的解释是完全成立的,不是"基本上")。
- `None` 多 → 说明 `traffic_percentage` 这一列本身缺失率高,那是另一个数据质量问题。

### C2. 有多少渠道其实没在发

```bash
python -c "
from retriever import usecase_catalog as uc, traffic
idle = live = unknown = 0
for ucid, rules in uc.rules_by_use_case_id().items():
    for e in traffic.summarise(rules):
        if e['sends'] is False: idle += 1
        elif e['sends'] is True: live += 1
        else: unknown += 1
print('channel-slots  live:', live, ' idle(0%):', idle, ' unknown:', unknown)
"
```

**回报:** 三个数字。`idle` 就是"**以前被算成活跃、现在不算了**"的渠道数。

---

## 任务 D —— `business_category` 与 `send_mode`

### D1. 数据字典枚举已生效,且分清了出处

```bash
python -c "
from retriever import usecase_catalog as uc
for c in ('6','32','33','37',''):
    r = uc.resolve_business_category(c)
    print(repr(c), r['source'], r['label'])
"
```

**期望:** `6` → `data_dictionary`;`32` → `code_enum`;`33`/`37` → `undefined`;`''` → `absent`。

### D2. ⭐ `send_mode` 这一列在真实导出里到底有没有

我们**只在数据字典的照片里见过 `send_mode`**,没见过真实导出的表头。代码是**乐观绑定**的
(有就用,没有就什么都不做),但要知道实际情况。

```bash
python -c "
from retriever import usecase_catalog as uc
d,p,rows,cols = uc._master_rows()
print('master columns:', len(cols))
b,_ = uc._master_column_map(cols)
print('send_mode bound to:', repr(b.get('send_mode')))
from collections import Counter
if b.get('send_mode'):
    print(Counter((r.get(b['send_mode']) or '').strip() for _l,r in rows).most_common(10))
"
```

**回报:** 绑定到哪一列(或 `None`),以及取值分布。

### D3. 如果 D2 绑上了 —— 第三份口径和 `rule_text` 对得上吗

`send_mode` 是**独立于 `rule_text` 的第三份**同一语义登记(1 同时发 / 2 按优先级 / 3 单渠道),
所以它能白送一个交叉验证。

```bash
python -c "
from retriever import usecase_consistency as ucc
q = ucc.quality_findings(limit=0)
if not q.get('available'):
    print('dataset absent'); raise SystemExit
n = [f for f in q['findings'] if f['check'] == 'send_mode_vs_rule_text']
print('send_mode vs rule_text disagreements:', len(n))
for f in n[:5]: print(' -', f['use_case_id'], f['message'])
"
```

**回报:** 冲突数量 + 最多 5 条示例。

**怎么读:** 冲突数**很少**(个位数)→ 两份登记基本一致,这是个好消息,说明 `rule_text` 的解释
是可信的。冲突数**很多**(几百条)→ **先怀疑我的映射写错了**,不要先怀疑数据 ——
把示例贴回来,我来看是不是 `send_mode` 的编号含义和我理解的不一样。

> 这一条沿用上一轮那个教训的反向版本:**case 红了,第一嫌疑人是断言,不是数据。**

---

## 回报模板

```
先决条件  pytest:        [ N passed ]

A1 词典原样输出:         [ ... ]
A2 expand 输出:          [ ... ]           (确认无 '?')
A3 totals / 测量数 / 覆盖数: [ ... ]
A3 前 20 个 token:       [ token=次数 ×20 ]
A4 填完后覆盖数变化:      [ 从 X 到 Y ]

B1 stage_transition:     [ ... ] / stages_are_live: [ ... ]
B2 报告片段:             [ ... ]
B3 可解析 / 含 fallback:  [ N / M ]

C1 五个数字:             [ matched / blank / holds / FAILS / undecidable ]
C2 live / idle / unknown: [ ... ]

D1 四个 source:          [ ... ]
D2 send_mode 绑定:       [ 列名或 None ] 分布 [ ... ]
D3 冲突数:               [ N ] 示例 [ ... ]
```

---

## 这一轮在防什么

前四轮内网抓到的缺陷,全都是**我断言了你们环境里的某件事**(名字 → 形状 → 值格式)。
这一轮业主给的三个答案里,有两个**天然带着这个陷阱**:

- "vendor 空**基本上**是因为 0%" —— 把"基本上"实现成规则,就等于断言。所以改成**逐行复核**,
  任务 C 量的就是这句话的真实成立率。
- "`send_mode` 在 `tbl_use_case` 上" —— 我只看过数据字典,没看过真实表头。所以**乐观绑定**,
  任务 D2 就是去看它到底在不在。

**保持抽象的那一半在我这边,每一个真实的名字、形状、格式都归你们。**

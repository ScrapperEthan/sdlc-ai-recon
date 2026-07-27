# RUNBOOK-53 —— rule_text 语义确认后的真实 UAT 数据验证

> **执行方:内网 Codex(在盒子上,有真实 UAT 数据)。**
> **背景:** 业主 2026-07-27 确认了渠道规则表达式的含义,配置已填好并推送。
> 在此之前解释功能一直是关闭的(故意不猜),**这是它第一次在真实数据上跑**。
> Round B 的这部分代码从未在真实 UAT 数据上验证过 —— 本 runbook 就是补这个验证。
>
> **本次不需要新数据**,盒子上已有的 UAT 三张表就够。

---

## 0. 前置

```bash
git pull                      # 需要包含 config/rule_text_semantics.json 的那次提交
ls config/rule_text_semantics.json     # 必须存在
```

确认 UAT 数据集就位(和 RUNBOOK-45 用的是同一份):

```bash
python -c "from retriever import usecase_catalog as uc; print(uc.snapshot_manifest())"
```

> ⚠️ 如果 `SDLC_USECASE_DATASET` 环境变量指向真实数据集,**跑测试时它会影响结果** ——
> 这是已知问题(RUNBOOK-45 已修过一轮)。若测试数出现异常,先确认是否是环境变量泄漏。

---

## 任务 A —— 回归:确认没有跑坏任何东西

```bash
python -m pytest -q
```

**预期:415 passed。** 少于这个数,把失败清单原样贴回。

---

## 任务 B —— 语义点亮了多少条(**最重要的数字**)

```bash
python - <<'PY'
from retriever import usecase_catalog as uc, rule_text as rt
ext = uc.ext_by_use_case_id()
blank = nonblank = available = unparsed = 0
for row in ext.values():
    raw = (row.get("rule_text") or "").strip()
    if not raw:
        blank += 1
        continue
    nonblank += 1
    ast = rt.parse(raw)
    if rt.interpret(ast)["available"]:
        available += 1
    elif not ast["operator_tree"]:
        unparsed += 1
print(f"ext rows          : {len(ext)}")
print(f"blank rule_text   : {blank}")
print(f"non-blank         : {nonblank}")
print(f"interpretable     : {available}")
print(f"unparseable       : {unparsed}")
print(f"source_precedence : {rt.source_precedence()}")
PY
```

**回报这 6 个数字。** 参考基线(RUNBOOK-45 Part B 在同一份数据上数出来的):非空 rule_text 约 **2,640**。
`interpretable` 应该接近这个数;差得多说明有一类表达式没解析成功,**把差额和几个例子贴回来**。

---

## 任务 C —— 严重级重新分布(证明"降噪"确实发生了)

```bash
python - <<'PY'
from retriever import usecase_consistency as ucc
import json
r = ucc.quality_findings(limit=0)
print("available      :", r.get("available", True))
print("total_findings :", r.get("total_findings"))
print(json.dumps(r.get("counts_by_severity"), indent=2, ensure_ascii=False))
resolved = [f for f in r.get("findings", []) if f.get("resolution")]
print("findings carrying a resolution:", len(resolved))
for f in resolved[:3]:
    print("  ", f["check"], "|", f["severity"], "|", f["resolution"][:90])
PY
```

**回报:** `total_findings`、三档严重级各多少、带 `resolution` 的有多少条。

**预期方向**(不是硬指标,是"应该朝这个方向变"):
- `expression_vs_priority` 从 **error → warning**,并且每条都带 `resolution`
- `blank_with_rules` 从 **warning → info**(业主说 rule_text 为空时看 priority 是正常兜底,不是缺陷)
- **error 档应该明显变少** —— 这正是这次改动的价值:真问题不再被"其实没问题"的条目淹没

---

## 任务 D —— 抽查那条标志性用例 I0141

```bash
python impact_report.py use-case:I0141
```

**在输出里确认这几行存在:**

```
- semantics: **owner-confirmed** (config/rule_text_semantics.json)
  - sends first: LETTER
  - sent together: EMAIL, SMS
  - NOTE: whether a later stage always sends or only on failure ... is NOT owner-confirmed
```

并且**不应该**再出现 `semantics: **unconfirmed**`。

再抽查 2~3 条自选用例(挑 rule_text 里带 `|` 的、和带混合运算符的各一条),确认没有崩溃、没有明显离谱的解读。

---

## 任务 E —— 例外与异常

跑一次刷新,把异常情况记下来:

```bash
python refresh.py
```

**回报:** 任何报错、任何 `unknown_channel` / `syntax_error` / `literal_escape_artifact` 的条数
(任务 C 的输出里就有)。特别注意 `literal_escape_artifact` —— 那是运行时那个已知 bug 的同类信号。

---

## ✋ 本次明确**不要**做的事

- **不要修改 `config/rule_text_semantics.json` 里的语义值。** 那是业主确认过的,改它等于伪造业务规则。
  (如果你认为某个取值和数据对不上,**报告**,不要改。)
- **不要**去改产品运行时的 Java 代码。
- **不要**提交任何数据、名册、人名。报告里只写数字和用例 ID,不要贴整行数据。
- **不要**试图给 `business_category` 33 / 37 补名称 —— 那个还没有答案,而且该字段疑似已迁到
  `tbl_use_case_router` 表(还没拿到)。

---

## 回报格式(贴回这些就够)

```
A 回归        : ___ passed / 失败清单
B 语义覆盖    : ext=___ blank=___ nonblank=___ interpretable=___ unparseable=___
C 严重级      : total=___  error=___ warning=___ info=___  带resolution=___
D I0141 抽查  : 通过 / 不通过(附实际输出)
E 异常        : refresh 是否成功 + unknown_channel/syntax_error/escape 各___条
其他发现      : 自由描述
```

内网 Codex 可以把这份报告提交到**内网仓库**;公网这边由项目负责人转达即可。

# RUNBOOK-71 — 严格零调用缺口：已修 + 三点回应

回应 `RUNBOOK-70-SCOPE-GATE-ZERO-CALL-EXTERNAL-HANDOFF-20260804.md`。
基线 `9f075ef` → 本轮，**1289 tests**。

## 0. 你们是对的，而且我先复现了才动手

不是"接受报告然后照改"。我按你们给的条件跑了一遍：

```
out-of-scope repo, valid window:
  plan.ok        : True
  targets        : [('mc-hk-hase-csl-sms-deli-job', out_of_scope=True, app_candidates=[])]
  MCP calls made : ['log.list_apps']        ← 缺口在这里
```

**范围闸门挡住了文件搜索和日志正文读取，然后在它们前面漏了一次元数据调用。**
这不是"少写了一个判断"，是结构性的：`log.list_apps` 在任何 per-target 检查**之前**跑。

修完（同样的条件，用 shipped config）：

```
out-of-scope repo -> MCP calls: NONE   evidence: 0
use-case only     -> MCP calls: NONE   plan.ok: False
unmapped repo     -> MCP calls: NONE
effective_sources : ['hkp3']
plan.log_files    : ['otx_trace.log', 'exception.log']
```

---

## 1. 三点回应

### ① 我没有照你们的写法做两份判断 —— 改成了一份共享的

你们 §3.A 和 §3.B 各写了一段 `log_targets = [...]` 的列表推导。**两份同样的谓词就是两个
会漂的东西**，而它们漂开之后的形状，正好就是这次的缺陷：规划层说"能跑"，调查层同意到
足够发出一次调用，然后才逐个发现根本没东西可查。

改成 `incident_plan.runnable_log_targets(plan)`，**一个定义，两层都用它**：

```python
def runnable_log_targets(query_plan):
    return [t for t in (query_plan or {}).get("targets") or []
            if isinstance(t, dict) and t.get("app_candidates")]
```

规划层用它算 `ok`，调查层用它决定要不要开 socket。语义只有一处可以改。

### ② 但你们坚持"调查层也要自己查一遍"是对的，而且理由比你们写的更硬

你们把 §3.B 叫"防御性检查"。它不只是防御 —— **它是必需的**，因为
`investigate_events(alert_text, ..., query_plan=None)` 里 `query_plan` 是一个**参数**：
调用方可以塞进来一份**不是这个模块生成的** plan。所以"规划层说 ok"根本不是关于
`targets` 内容的任何保证。

我加了一条测试专门钉这个：手工伪造一份 `ok: True` + 全是空 `app_candidates` 的 plan
传进去，断言 **0 次 MCP 调用**（`test_the_investigator_does_not_trust_a_supplied_plan`）。

### ③ `out["ok"]` 我按你们说的改了语义，但想说明为什么这次是安全的

改一个已有字段的含义通常有风险。这次查了所有消费方，结论是 **`ok` 本来就是"日志分支能不能跑"**，
只是算错了：

| 位置 | 用法 |
| --- | --- |
| `incident_investigator:1121` | `any_runnable or ok` —— 总闸门 |
| `incident_investigator:1160` | `if not ok` → **只结束日志分支** |
| `incident_plan:710` | `any_runnable = ok or cw.runnable or portal.runnable` |

所以这不是重新定义，是修正。Portal / CloudWatch 分支**不受影响**（范围外 repo + 有效告警名
仍然会跑指标分支）—— 这一点你们 §3.B 特意要求保留，测试里也钉住了。

另外按你们的要求，`out["targets"]` **保留全部 target**（含范围外的，带 `app_note` 说明原因），
新增 `out["log_targets"]` 明确列出真正可跑的那些。UI 和审计拿得到拒绝原因，
分支可运行性只看后者。

---

## 2. 关于 `use_cases` —— 我同意，并且把话写进了代码

你们指出 `parsed["use_cases"]` 也会让 `ok` 变 true，但 plan **从来没有**把 use case
转换成可查询的 repo/app target。所以那是"开了一个branch，里面什么都没有"。

已经从 `ok` 的计算里去掉了。代码注释里写明了：**如果以后要支持 use case → repo/app，
它应该产出带真实 candidates 的 target，那一行会自动接住它** —— 而不是重新加一个
"use case 也算 runnable"的特例。

---

## 3. 新增的回归测试

`tests/test_incident_scope.py::StrictZeroCallTests`，你们要的三条全在，外加两条：

| 测试 | 断言 |
| --- | --- |
| 1. 只有范围外 repo，时间窗有效 | `mcp_client.call` **0 次**；refusal 里出现 `explicit_mapping_only` 或 `OUTSIDE configured scope` |
| 2. 只有 use case | LogDream **0 次**；`plan.ok is False` |
| 3. Portal + 范围外混合 | `search_files`/`read` 的 `app` 只有 `portal`；范围外那个仍带 `app_note` |
| +. plan 公布可跑目标 | `plan.log_targets == ["mc-hk-hase-portal-web"]`，`targets` 仍是两个 |
| +. 不信任外部传入的 plan | 伪造 `ok:True`+空 candidates → **0 次调用** |

---

## 4. 我不认可 / 想澄清的地方

### ① §5 第 1 条现在不成立了 —— 但另外两条我完全同意

> 「1. 最新引擎仍有上述 `log.list_apps` 零调用缺口」

这条本轮修掉了。**但我不认为因此就可以部署** —— 你们另外两条理由（磁盘、`server.py` 定制）
和我无关也不由我判断，那是运维和你们的调用。**部署与否请按你们的判断，我不催。**

### ② "范围外 target 不应使 LogDream 分支变成 runnable" —— 措辞我收窄了一点

严格说，让分支不可跑的不是"范围外"，而是**"没有可查询的 app 候选"**。范围外只是其中一种原因，
另一种是"有映射但候选在任何 source 上都不存在"。我按后者（更一般的条件）实现，
因为按"范围外"实现会漏掉第二种。

拒绝文案仍然分得清是哪一种 —— 这是上一轮就定下的：
**"明确不在范围内"和"缺少映射"必须分开说**，否则刻意的限制看起来像缺陷。

### ③ 一个你们没提、但我顺手确认了的

`log.search_files` 和 `log.read` 本来就在 per-target 循环里，所以只有 `list_apps` 有这个问题。
我 grep 了整个 `investigate_events`，**`log.list_apps` 是唯一一处在 per-target 检查之前的
LogDream 调用**。Portal 和 CloudWatch 分支各有自己的闸门，不受影响。

---

## 5. 请你们验的

```bash
git pull
python -m pytest tests -q          # 期望 1289 passed
```

真机（`SDLC_MCP_ENABLED=1`）：

1. 范围外 repo + 有效时间窗 → **LogDream 一次调用都没有**（不是"没有 search/read"，是连
   `list_apps` 都没有）。请直接看 MCP 侧的访问日志确认，不要只看我们的 packet。
2. 只给 use case → 同样 0 次。
3. `mc-hk-hase-portal-web` → 正常查到日志。
4. 混合输入 → `search_files`/`read` 里的 app 只有 `portal`。

`config/mcp_tools.json` 这轮**我没有再动**，所以不会有新的冲突。

---

## 6. 仍然只有你们/owner 能给的（没变）

真实授权 tracking ID、能映射到 log group 的 alarm、目标资源 ARN、LogDream 服务端
keyword 修复、`log.investigate` 的 strict 三件套、`/home` 磁盘清理。

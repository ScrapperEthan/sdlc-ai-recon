# RUNBOOK-83 —— agent 模式退化：先把五件事测出来，再动代码

> 配套 spec：[`docs/specs/agent-ask-parity-and-answer-packet-zh.md`](docs/specs/agent-ask-parity-and-answer-packet-zh.md)
>
> **这一份不改任何代码。** 它只做一件事：把"agent 模式答成『结论：无』"这件事的机理
> **测出来**，因为外网的分析里有**一条关键推断还没有被证据钉死**（探针 2），
> 而按错的推断改代码，比不改更糟。
>
> **背景**：2026-08-17 同一条生产 SMS 告警，ask 模式给出了处置结论，
> agent 模式跑了 6 步检索、112 秒、两次重规划，最后返回
> 「当前没有通过证据出口闸门的确定性结论」＋「已确认事实：无」＋ 6 条 gap。
> 已经排除的：**权限**（`auto_allowed_by_policy`、`access all_readonly`）、
> **超时**（112s/360s）、**生产预算**（`incident 0/4`，一次没打）。

---

## 0. 三条规矩（每次都适用）

1. **对不上先回报，不要顺手适配。** 适配方案由差异决定，不由这份文档决定。
   外网这边已经因为"对着想象写"返工过五轮，每一轮的缺陷都是同一个形状：
   **我们对你们环境里的某样东西下了断言**（名字 → 响应形状 → 值格式）。
2. **原样回传，不要总结、不要改写。** 尤其是字段名和取值 —— 一个 `hk1` / `hkl` 的差别
   就够让下一份 spec 全错。
3. 🔴 **不要把原始生产日志、未脱敏 payload、真实 alarm name 贴进回报。**
   下面每个探针要的都是**字段名、结构、分类结果、计数**。
   需要举例时用**占位符**（`<app>` / `<repo>` / `<keyword>`），
   或者用工具自己给的指纹（`<tracking:...>`）。
   —— 这条不是客套：出口脱敏闸门存在的理由就是这个，一份回报文档不能成为它的绕道。

---

## 探针 1 —— 现状对齐（先跑这个）

外网快照是 2026-08-15 的，你们的代码已经有变动，而且外网**知道**至少有四个文件是你们自建的。

### 1a 跑这个（只读）

```python
# scripts/agent_parity_probe.py —— 只读，不改任何东西
import inspect, json, os, subprocess, sys
sys.path.insert(0, os.getcwd())

out = {"head": subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()}
out["webapp_files"] = sorted(f for f in os.listdir("webapp") if f.endswith(".py"))
out["prompt_files"] = sorted(os.listdir("prompts"))
out["config_files"] = sorted(f for f in os.listdir("config") if f.endswith(".json"))

# 这四个是外网清单里没有的、你们自建的模块 —— 只要签名，不要实现
for mod in ["answer_gate", "evidence_normalizer", "context_pack", "investigation_handoff",
            "agent_loop", "agent_planner", "agent_plan", "agent_state", "tool_subset",
            "intent_router", "answer_packet", "baseline_answer"]:
    try:
        m = __import__(f"webapp.{mod}", fromlist=["*"])
        out[mod] = {n: str(inspect.signature(f))
                    for n, f in vars(m).items()
                    if callable(f) and not n.startswith("_") and getattr(f, "__module__", "") == m.__name__}
    except ImportError:
        out[mod] = "NOT PRESENT"
print(json.dumps(out, ensure_ascii=False, indent=2))
```

```bash
python -m pytest -q 2>&1 | tail -3
```

### 1b 顺便回答（一句话一项就够）

[`docs/specs/agent-mode-implementation-zh.md`](docs/specs/agent-mode-implementation-zh.md) §2 的 21 项
（S0 / A1–A6 / B1–B7 / C1–C2 / D1–D3 / E1–E2），**每项标一个字**：
`done` / `partial` / `skipped` / `changed`（改了做法的写一句改成什么）。

### 1c 期望 vs 回报

| 锚点 | 外网的预期 | 对不上就写下来 |
| --- | --- | --- |
| `webapp/` 里有 `agent_loop.py` / `agent_plan.py` / `agent_state.py` / `tool_subset.py` | 有 | |
| `answer_gate.py` / `evidence_normalizer.py` / `context_pack.py` / `investigation_handoff.py` | **外网不知道它们的契约** | 把签名原样贴回 |
| `intent_router.py` / `answer_packet.py` / `baseline_answer.py` | 预期 `NOT PRESENT`（还没做） | 已经有了就说 |
| 测试数 | ≥1355 | |

---

## 探针 2 —— 🔴 最重要的一条：那一轮，每条证据为什么被拒？

**外网目前唯一没被证据钉死的推断**：

> `add_evidence()` 因为拿不到 `tier` / `environment`，把每条证据都按 fail-closed 规矩
> 落成了 gap，于是 `facts=0`、`claims=0`。

依据是 [`docs/specs/agent-mode-implementation-zh.md`](docs/specs/agent-mode-implementation-zh.md) §B2.0 那条规矩
（"`tier` 和 `environment` 必须来自工具结果里已有的字段…取不到 → 记 gap，不许给默认值"），
而外网实测发现：`unified_impact`、`search_code`、`read_file`、`incident_impact`
**这四个工具一个出身字段都没有** —— 恰好就是那一轮用到的工具。

**这条如果成立，问题就不在路由，而在证据归一化 —— 修的地方完全不同。**

### 2a 要什么

那一轮（`plan_ab7464dfd1d842e3bb6e571f4406594a` v3，或任意一次能复现的同类跑）的台账里：

| 要什么 | 形式 |
| --- | --- |
| 每个工具返回后，**尝试入台账的条目数** | `{"tool": "unified_impact", "attempted": 12, "as_fact": 0, "as_gap": 12}` |
| 每条被拒/落 gap 的**原因字符串**（代码里那句话，原样） | 逐条，去重后带计数即可 |
| `facts` / `gaps` / `assumptions` 三个通道的最终计数 | 三个数字 |
| 合成器**收到的输入**里 facts 有几条 | 一个数字 |
| claim 是**从来没生成**，还是**生成了被 gate 拒**，还是**被 repair 删掉** | 三选一，附计数 |

最后一行是分岔口：

- **从来没生成** → 印证上面的推断，修 §6.3（产地表）；
- **生成了被拒** → 看拒绝码，可能是引用不可核（§4.5）；
- **被 repair 删掉** → 是内网自己诊断的 §2.5 那条，修 §6.4（repair 质量比较）。

### 2b 怎么拿

优先用已有的检查点（`webapp_data/agent_turns/`，清单 §B2.2）。
如果那一轮没落盘 / TTL 过了，就**用 `LLM_MOCK=1` 造一次同形状的跑**：
一个只调 `unified_impact` ＋ `search_code` 的计划，看 facts 是不是同样为 0。
**这一步不需要真告警、不需要打生产。**

### 2c 期望看到

如果推断成立：`as_fact: 0`、原因字符串里出现"tier"/"environment"/"unavailable or unknown"字样。

**看到别的（比如证据其实进了 facts，是合成器没用）→ 立刻回报，先别改任何东西。**
那意味着外网的整条推理链要重写。

---

## 探针 3 —— 五个工具的出身字段实测（产地表要按这个填）

[`docs/specs/agent-ask-parity-and-answer-packet-zh.md`](docs/specs/agent-ask-parity-and-answer-packet-zh.md) §5
那张 `config/evidence_provenance.json` 是**按外网代码推出来的**，需要真机核一遍。

### 3a 跑这五个（参数用你们手上任意真实值，**不要打生产**）

```python
from webapp import tools
import json
for name, args in [
    ("unified_impact",   {"seed": "<任意类名或仓库名>"}),
    ("search_code",      {"pattern": "<任意方法名>", "glob": "*.java", "max_results": 3}),
    ("read_file",        {"path": "<任意文件>", "start": 1, "end": 5}),
    ("incident_impact",  {"alert_text": "<任意告警原文>"}),
    ("impact",           {"repo": "<任意仓库>"}),
]:
    packet = tools.dispatch(name, args)
    # 只要键的结构，不要值 —— 值里可能有仓库名/告警内容
    def shape(o, depth=0):
        if depth > 3: return "..."
        if isinstance(o, dict): return {k: shape(v, depth+1) for k, v in o.items()}
        if isinstance(o, list): return [shape(o[0], depth+1), f"...x{len(o)}"] if o else []
        return type(o).__name__
    print(name, json.dumps(shape(packet), ensure_ascii=False))
```

### 3b 逐条回答

| 问题 | 期望（外网实测的） |
| --- | --- |
| `unified_impact` 的 `dependency_edges.source` / `message_edges.source` 是什么值 | 两个 CSV 的路径 |
| `unified_impact` 有没有 `environment` / `tier` 字段 | **没有** |
| `search_code` 返回的是**裸行列表**还是带结构的对象 | 裸行列表 |
| `incident_impact` 包里那个 `environment` 的值 | **空字符串** `""`（而且它是**告警里解析出的环境标签**，不是证据的环境） |
| `impact` 的渠道块里有没有 `relation` / `rank` / `direct` / `citation` | 四个都有 |
| `impact` 的 `relation` 取值来自 `channel_evidence.relation_order` 那 6 个吗 | 是 |

### 3c 还差一行：`routing_source`

```bash
python -c "import csv;rows=list(csv.DictReader(open('index/message_edges.csv',encoding='utf-8')));print(sorted({r.get('routing_source','') for r in rows}));print(len(rows), sum(1 for r in rows if r.get('evidence')))"
```

**把 `routing_source` 的完整取值集合原样贴回。** 产地表里
`index/message_edges.csv` 那一行的 `kind_from_row.map` 现在是 `{"?": "?"}`，就等这个。

---

## 探针 4 —— 闸门在"有证据、零结论"时到底返回什么

不改代码，只把当前行为测出来，作为改 §6.4 的基线。

```python
# 用 LLM_MOCK=1，构造三种输入喂给你们的 answer_gate
```

| 场景 | 现在返回什么（回报） | spec 要求改成 |
| --- | --- | --- |
| facts=3、claims=[] | ? | `answer_status ∈ {partial, blocked}` |
| 初稿 2 accepted + 1 rejected，repair 后 0/0 | ? | 保留初稿，且记 `empty_repair_erased_supported_facts` |
| 5 个 claim：3 过 2 拒 | ? | 输出 3 个，另 2 个转 gap |
| 所有工具不可用 | ? | `blocked` ＋ 说明，不是"无事实" |

顺便回报两个字段名：你们的 `answer_status`（或等价物）**叫什么、有哪几个取值**，
以及前端顶栏现在优先显示的是执行状态还是答案状态。

---

## 探针 5 —— 那个布尔前置分类器，和被裁掉的工具

外网的判断是：**两个动作叠起来**才让 `incident_investigate` 消失 ——
① 一道正则前置分类给出 `incident_investigation_required: false`，
② `tool_subset` 按计划裁剪工具，于是执行器**看不见也调不了**。

### 5a 回报

| 问题 | 回报什么 |
| --- | --- |
| `_incident_investigation_required` 这个函数（或等价物）在哪个文件、判据是什么 | 函数名 ＋ **它认哪些信号**（外网只从截图知道 `alert_signal` / `diagnostic_signal` / `structured_exception` / `impact_only` 这四个名字） |
| 它的输出怎么进 Planner | 是一个 boolean 键，还是别的形状 |
| 那一轮，**每次模型调用实际发出的工具列表** | 逐次的工具名数组（清单 §B5.1 的硬指标是 ≤5） |
| `tool_subset` 的兜底集合是什么 | 配置里那个 `fallback_tools` 的实际值 |
| `incident_investigate` 是否**曾出现在**任一次调用的工具列表里 | 是/否 |

### 5b 期望

`incident_investigate` **一次都没出现**；那四个信号里 `diagnostic_signal=False`
（`Connection reset` / `TERR_30020` / `I/O error on POST request` 没被识别为生产诊断信号）。

**如果它其实出现在工具列表里、只是模型没选** —— 那结论完全不同（是提示词问题，不是路由问题），
**立刻回报。**

---

## 回报格式

一个文件，五节，节标题就用 `探针 1` … `探针 5`。
每节先贴命令输出（原样），再回答那几个问题。**没跑的探针写"没跑"和原因，不要留空。**

回报后外网出：
1. `config/evidence_provenance.json` 的**填好版**（表里的行说的都是我们自己工具的字段，
   不需要你们填）；
2. 按探针 2 的分岔结果，把 spec §6 那五处修复**收敛成实际要改的那几处**；
3. 内网那份《Ask 能力不退化…修复计划》的修订版（范围、阶段、开关数量都会按这次回报调）。

---

## 附：这一轮已经确定的事（不用再测）

- **不是权限问题**：`auto_allowed_by_policy`、`access all_readonly`。
- **不是超时**：112s / 360s。
- **不是生产预算耗尽**：`incident 0/4`，一次没打。
- **重规划是活的**：`patches 2/5`，`T2_F1` 是补出来的任务。
- **有序的证据词表已经存在**，在 `retriever/channel_evidence.py`
  （`relation_order` 6 个值 ＋ `_rank()` ＋ `OWNERSHIP_RELATIONS` ＋
  `config/channel_evidence.json` 配置钩子）。
  **新的 Answer Packet 不要再造一套 `evidence_grade`** —— 复用它，见 spec §3。

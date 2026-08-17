# Agent 模式 —— 实施清单（交内网 Codex）

> 配套设计文档：[`docs/specs/agent-mode-zh.md`](agent-mode-zh.md)。那一份定「为什么」和契约，
> **这一份定「做什么、按什么顺序、怎么算做完」**。
>
> **⚠️ 这份清单是对着 2026-08-15 的外网快照写的，内网的代码已经有变动。**
> 所以 **§0 的对齐探针是第一项任务，不是可选项** —— 先跑它、先回报差异，再开始写任何代码。
> 清单里所有的锚点都写成**函数名 / 契约 / 配置键**，**不写行号**，就是为了扛住这个偏差。

---

## 0. 业主已拍板的四件事（本清单据此收敛）

| 问题 | 决定 | 对实施的影响 |
| --- | --- | --- |
| agent 模式能不能读生产 | **能**，通过子代理的 MCP **只读**读生产日志 | 生产调用**单独计数、单独上限**，闸门一个不许拆（§5） |
| UAT 数据库 | **没有接**。现在只有从 UAT 导出的**若干 CSV**，要能读；**表述信息还没写，后续要补** | 新增 `dataset_query` 工具族，契约照抄 `db_query`；**必须自带「哪些还没填」的清单**（§4） |
| 模型分工 | **规划用 `sol`，执行用 `terra`**（暂定，**用户可选**） | 新增「角色→模型」绑定层；**代码里绝不出现这两个名字**（§3） |
| 延迟上限 | 按建议：**`max_seconds = 180`** | 停止条件 + 前端「已查 N 秒」计时（A4/E1） |

---

## 1. 先对齐：Step 0 探针（**第一个任务，独立提交**）

**问题**：外网快照和内网现状已经分叉，按快照写的改动可能落在已经不存在的位置上。
**不做的后果**：改到一半发现 `answer_events` 的签名变了、事件类型多了两个、`tools.TOOLS` 里的工具
名不一样 —— 返工是小事，**更糟的是「改对了一半」**：ask 模式的回归基线被悄悄破坏，
而这条基线是后面 16 个任务唯一的安全网。
**做完的效果**：一张写实的差异表，后面每个任务都能确认自己动的那一处到底长什么样。

在盒子上跑，把输出**原样回传**（不要总结、不要改写）：

```python
# scripts/agent_mode_probe.py —— 只读，不改任何东西
import inspect, json, os, subprocess, sys
sys.path.insert(0, os.getcwd())
from webapp import agent, tools, config, context_budget, session_store, llm, llm_usage

out = {}
out["head"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
out["dirty"] = bool(subprocess.run(["git", "status", "--porcelain"],
                                   capture_output=True, text=True).stdout.strip())
for name, fn in [("agent.answer", agent.answer),
                 ("agent.answer_events", agent.answer_events),
                 ("tools.dispatch", tools.dispatch),
                 ("tools.dispatch_events", tools.dispatch_events),
                 ("session_store.append_exchange", session_store.append_exchange),
                 ("llm.chat_stream", llm.chat_stream),
                 ("llm_usage.add_call", llm_usage.add_call),
                 ("context_budget.Budget.__init__", context_budget.Budget.__init__)]:
    out[name] = str(inspect.signature(fn))
out["tool_names"] = [t["function"]["name"] for t in tools.TOOLS]
out["subagent_tools"] = sorted(tools.SUBAGENT_TOOLS)
out["lane_shares"] = dict(config.CONTEXT_LANE_SHARES)
out["max_tool_iters"] = config.MAX_TOOL_ITERS
out["has_current_override_helper"] = hasattr(config, "current_llm_override")
out["config_files"] = sorted(f for f in os.listdir("config") if f.endswith(".json"))
out["prompt_files"] = sorted(os.listdir("prompts"))
print(json.dumps(out, ensure_ascii=False, indent=2))
```

再补三条命令的输出：

```bash
grep -n "event.type ===" webapp/static/app.js
python -m pytest -q 2>&1 | tail -3
python -c "import json;ls=[json.loads(l) for l in open('evals/cases.jsonl',encoding='utf-8') if l.strip()];print(len(ls), sorted({c.get('lane','') for c in ls}))"
```

**期望值（外网快照，2026-08-15）**，对不上的逐条说明：

| 锚点 | 期望 |
| --- | --- |
| `agent.answer_events` | `(question, history=None, owner='')` |
| `agent.answer` | `(question, history=None)`，返回含 `answer/tool_trace/usage/citations/views/subagent_steps` |
| `tools.TOOLS` 名单 | impact, hubs, message_flow, usecase_routing, list_repos, search_code, read_file, unified_impact, show_arch, source_system_impact, usecase_impact, search_usecases, usecase_quality_findings, incident_impact, critical_repos, incident_investigate, db_query |
| `SUBAGENT_TOOLS` | `{incident_investigate}` |
| `CONTEXT_LANE_SHARES` | history .25 / compaction .05 / tools .50 / subagent .20 |
| `MAX_TOOL_ITERS` | 8 |
| `config.current_llm_override` | **不存在**（本次要加，见 B3） |
| app.js 事件分支 | tool_start / subagent_step / view / token / done / error |
| 测试数 | ≈1355 |
| eval 用例 | 39 条，lane ∈ {incident, retrieval, …} |

**规则：任何一个锚点对不上，先回报，不要「顺手适配」。** 适配方案由差异决定，不由清单决定。

---

## 2. 任务总览（21 项，建议顺序即依赖顺序）

标 🟢 的是**纯新增文件**（不碰现存代码，最抗分叉）；标 🟡 的**要动现存文件**（每项只动一个小接缝）。

| # | 任务 | 类型 | 动到的文件 |
| --- | --- | --- | --- |
| **S0** | 对齐探针 | 🟢 | `scripts/agent_mode_probe.py` |
| **A1** | 模式策略 + 配置 | 🟢 | `config/agent_modes.json`, `webapp/turn_policy.py` |
| **A2** | `mode` 端到端透传 | 🟡 | `webapp/agent.py`, `webapp/server.py`, `webapp/session_store.py`, `webapp/static/app.js` |
| **A3** | 同轮工具结果缓存 | 🟢+🟡 | `webapp/tool_cache.py`（新）+ agent 接一行 |
| **A4** | 事件协议扩展 + 前端骨架 | 🟡 | `webapp/static/app.js`, `app.css` |
| **A5** | `plan` 车道 **+ 填上一直空着的 `compaction` 车道** | 🟡 | `webapp/config.py`, `webapp/context_budget.py`, `webapp/agent.py` |
| **A6** | ask 模式回归基线 | 🟡 | `evals/run.py`, `evals/cases.jsonl` |
| **B1** | Plan 数据结构与校验 | 🟢 | `webapp/agent_plan.py` |
| **B2** | 状态台账 **+ 三个状态原语**（只追加 / 检查点 / 可中断） | 🟢 | `webapp/agent_state.py`, `webapp/agent_checkpoint.py` |
| **B3** | 角色→模型绑定（sol/terra） | 🟢+🟡 | `webapp/model_roles.py`（新）+ `config.py` 加一个读取函数 |
| **B4** | 规划器 | 🟢 | `webapp/agent_planner.py`, `prompts/agent-planner-prompt.md` |
| **B5** | 调度 + 执行 + 合成 **+ 按任务裁剪工具 schema + 出口证据闸门** | 🟢+🟡 | `webapp/agent_loop.py`（新）, `webapp/tool_subset.py`（新）, `prompts/agent-synthesis-prompt.md`, agent.py 分岔 |
| **B6** | **计划先给人看，批准/改了再执行** | 🟢+🟡 | `webapp/agent_approval.py`（新）+ server / app.js 接线 |
| **B7** | **会话级「已确认事实」槽位** | 🟢+🟡 | `webapp/turn_facts.py`（新）+ session_store 一个字段 |
| **C1** | CSV 数据集登记表 | 🟢 | `config/csv_datasets.json`, `webapp/csv_registry.py` |
| **C2** | `dataset_query` 工具 | 🟢+🟡 | `webapp/csv_datasets.py`（新）+ `tools.py` 加一个工具 + 提示词加一节 |
| **D1** | 三层校验 | 🟢 | `webapp/agent_validate.py` |
| **D2** | 触发器表 + 增量重规划 | 🟢 | 扩 `agent_planner.py` / `agent_plan.py` |
| **D3** | **跑到一半能打断、能改向** | 🟢+🟡 | `webapp/agent_steer.py`（新）+ server / app.js 接线 |
| **E1** | 计划面板（完整版 + 回放） | 🟡 | `webapp/static/app.js`, `app.css` |
| **E2** | agent lane 评测 + RUNBOOK-81 | 🟡 | `evals/`, `RUNBOOK-81-*.md` |

**每项一个分支、一个提交、自带测试。** 任何一项做完，仓库都必须是可运行、测试全绿的状态。

---

## 3. 逐项：P0 管道（A1–A6）

### A1 🟢 模式策略 + `config/agent_modes.json`

**做什么**：`webapp/turn_policy.py` 提供 `policy(mode) -> TurnPolicy`，字段：

```
mode, max_tool_iters, max_tool_calls, max_production_calls, max_seconds,
max_tasks, planning_enabled, allow_production, allow_datasets,
roles{planner,executor,validator,synthesizer}, stop_on
```

配置文件（**committed 模板，全 `"?"`；盒子上 `cp` 一份 `config/agent_modes.local.json` 改**，
程序自动优先读它 —— 和 `db_queries.json` 完全同一条缝）：

```json
{
  "_README": [
    "两种模式的预算与开关。左边的键是我们的，不要改；值是你们的，随便调。",
    "roles.*.model 填你们模型列表里的真实 id（例如规划用 sol、执行用 terra）。",
    "留 \"?\" = 不指定 = 用这一轮的默认模型。代码不认识任何模型名字，只认这里的值。",
    "本文件是被 git 跟踪的模板；盒子上请改 config/agent_modes.local.json（已 gitignore，自动优先读）。"
  ],
  "modes": {
    "ask": {
      "max_tool_iters": 8, "max_tool_calls": 12, "max_production_calls": 2,
      "max_seconds": 90, "planning_enabled": false,
      "allow_production": true, "allow_datasets": true
    },
    "agent": {
      "max_tool_iters": 6, "max_tool_calls": 24, "max_production_calls": 6,
      "max_seconds": 180, "max_tasks": 6, "planning_enabled": true,
      "allow_production": true, "allow_datasets": true,
      "stop_on": {"no_new_evidence_replans": 2}
    }
  },
  "roles": {
    "planner":     {"model": "?", "note": "要能稳定输出 JSON"},
    "executor":    {"model": "?", "note": "要能稳定发 tool_calls"},
    "validator":   {"model": "?", "note": "只输出 pass/fail/need_more，可以用便宜的"},
    "synthesizer": {"model": "?", "note": "写最终答案，留空=跟 executor 一致"}
  }
}
```

**完成判据**：`ask` 的策略值与现状**逐项等价**（`max_tool_iters=8` 就是今天的
`config.MAX_TOOL_ITERS`）；配置缺失/损坏 → 回落到内置默认，**不抛异常**；单测覆盖
「模板全 `?`」「local 覆盖生效」「未知 mode → 回落 ask」。

### A2 🟡 `mode` 端到端透传

- `agent.answer_events(question, history=None, owner="", *, mode="ask")` ——
  **关键字参数 + 默认值**。现存调用方（`server.py`、`evals/run.py`、`mcp_server.py`、
  一批测试里 `lambda q, h=None` 的桩）必须一行不改就继续工作。
- `agent.answer(question, history=None, *, mode="ask")` 同理。
- `server.py` 的 `/api/chat`：从请求体读 `mode`，用 `turn_policy` 校验，非法值 → `"ask"`（不报错）。
- `session_store.append_exchange(..., mode="ask", plan=None, state_summary=None)` ——
  **新增三个持久化字段**（B7 再加一个会话级的 `turn_facts`，做到那一项时一起加）。
  教训在前：`subagent_steps` 当初只 streaming 不落库，刷新页面就没了，
  后来补的。这次一开始就落库。
- 前端：输入框旁边一个二选一开关（Ask / Agent），选择记在本地并随请求发送。

**完成判据**：`mode="ask"` 下 `answer_events` 产生的事件序列与改动前**逐字节一致**
（用一个「录制-回放」测试锁住）；老会话（没有 `mode` 字段）读出来仍能渲染。

### A3 同轮工具结果缓存

**问题**：agent 模式必然出现两个任务都要同一个 `usecase_impact` 的情况。
**不做的后果**：token 和延迟直接翻倍，`max_tool_calls` 一半浪费在重复调用上。
**做完的效果**：同一轮内相同调用只打一次。

- `webapp/tool_cache.py`：key = `(name, 规范化 args)`（键排序、丢掉 None/空串/false 默认值）。
- **不缓存 `incident_investigate`**：每次 sweep 都是**故意不一样**的，而且缓存一次生产读取
  等于把「没有重新读」伪装成「又读了一遍」。
- `db_query` / `dataset_query` **可以缓存，但命中必须打 `from_cache: true`**，
  并且**不得被计为第二次确认**（台账里同一条证据不能因为读了两次就变强）。
- 生命周期严格是一轮（一次 `answer_events`），绝不跨会话、绝不落盘。

### A4 🟡 事件协议扩展 + 前端骨架

新增事件（沿用现有 NDJSON 通道，不新开连接）：

```
{"type":"plan",       "plan": {...}}                       # 计划生成/被补丁后
{"type":"task_start", "task":"T2", "objective":"…"}
{"type":"task_token", "task":"T2", "text":"…"}             # 执行阶段的模型输出，进面板不进答案
{"type":"task_end",   "task":"T2", "status":"done|failed|skipped",
                      "evidence": 3, "gaps": 1, "seconds": 4.2}
{"type":"replan",     "patch": {...}, "trigger":"use_case_link.available=false"}
{"type":"notice",     "level":"warn", "text":"角色模型 X 不可用，已回落到默认模型"}
```

> **🔴 这里有一个必须一起改掉的现存行为**：今天的循环把**每一轮**的 delta 都当
> `{"type":"token"}` 发给前端，只有最后一轮（没有 tool_calls）才算答案。
> agent 模式下规划器输出的是 JSON —— **不改的话用户会在答案气泡里看到一坨 JSON**。
> 做法：执行器接一个 `emit_kind` 参数，agent 模式下中间轮发 `task_token`，
> **只有 Synthesizer 的输出发 `token`**。ask 模式的行为保持原样。

### A5 🟡 `plan` 车道 + 填上 `compaction` 车道

**A5.1 新增 `plan` 车道。** `CONTEXT_LANE_SHARES` 新增 `plan`。建议 agent 模式下
tools 0.50→0.40、plan 0.10；**ask 模式的车道默认值一个都不动**（车道表按 mode 取，ask 取旧表）。

进上下文的 plan/state **只能是摘要**：fact 的 `claim` + 证据指针（tool/field/value/tier），
**原始 packet 留在进程内存**，按 id 取。`Budget` 加 `fit_plan(...)`，写法对齐已有的
`fit_tool_result` / `fit_subagent_result`（结构感知裁剪，不做字节截断）。

**A5.2 把 `compaction` 车道真正填上。**

> 这条不是外面抄来的 —— **是你们自己代码里的注释写着的**：`context_budget.py` 开头
> 明说这条车道「预留了但还没填」，「等以后填是补一个洞，不是重铺管道」。洞已经挖好了。

- **问题**：今天历史超预算的做法是**丢掉中间几轮**，然后加一句系统提示告诉模型
  「需要的话请用户重述」。
- **不改的后果**：ask 模式里还能忍（人就在旁边）。**agent 模式一轮十几次模型调用、
  一轮 180 秒，裁剪会频繁得多，而「请用户重述」在一次自动调查中间是句废话 —— 那会儿没人在。**
  结果是：调查跑到第 4 个任务时，已经忘了第 1 个任务确认过什么。
- **改完的效果**：丢之前先摘要，摘要占 `compaction` 车道（5% 已经预留好了）。
- 实现要点：摘要**只保留结论和已确认的约束**（时区、目标 repo、口径），
  **不保留原始工具返回内容**；多花的那一次模型调用走 `as_role("validator")`（便宜那档）。
  摘要失败 → 回落到今天的「丢 + 提示」行为，**不能因为摘要挂了就让整轮挂掉**。

### A6 🟡 ask 模式回归基线

- `evals/cases.jsonl` 每条加 `"mode": "ask"`（显式写死，不靠默认）；
  `evals/run.py` 把 `mode` 透传给 `answer()`。
- **在动任何 P1 代码之前，先跑一次全量基线并存档**（对照 RUNBOOK-66）。
- 记住那条教训：**用例红了，第一嫌疑人是断言，不是模型。**

---

## 4. 逐项：P1 规划与执行（B1–B7）

### B1 🟢 `webapp/agent_plan.py` —— 纯数据，不 import llm / tools

- `parse(raw_text) -> (plan|None, errors[])`：容错解析（照抄 `agent.py` 里
  `_json_from_text` 的思路：去代码围栏、找首尾大括号）。
- `validate(plan) -> errors[]`：字段齐全、task id 唯一、`depends_on` 有向无环、
  `serves` 指向存在的 IR、`tool_policy.prefer` 里的工具名**必须在 `tools.TOOLS` 里存在**
  （名字校验通过参数传入，不 import tools —— 保持可离线测）。
- `done_when` 只接受**受限词表**，模型不能写自由代码：

  | 断言 | 含义 |
  | --- | --- |
  | `result.ok == true` | 工具返回成功 |
  | `has_field:<路径>` | 某字段存在（如 `has_field:count`） |
  | `count > 0` / `count >= N` | 计数达标 |
  | `gap_recorded:<字段名>` | 该未知已被记成 gap（**这条常常才是正确的完成方式**） |
  | `evidence_tier >= <档>` | 证据等级达标 |

- `apply_patch(plan, patch) -> plan`：只支持 `replace_task` / `add_task` / `drop_task` /
  `add_open_question`。**不提供「整份替换」的入口** —— 没有这个函数，就没人能走那条路。

### B2 🟢 状态台账 + 三个状态原语

**这一项是 LangGraph 那三样功能的自建版**（为什么不直接装框架，见 §11）。
三样加起来约 200 行，都是我们自己的代码、能被现有测试框架覆盖、不引入任何依赖。

#### B2.0 台账本体 —— `webapp/agent_state.py`

- `facts` / `gaps` / `assumptions` / `evidence` / `task_events` / `notices` /
  `budget` / `task_status`，结构见设计文档 §5.3。
- `add_evidence(...)` 强制带 `tier` 和 `environment`，**两者都必须来自工具结果里已有的字段**，
  不接受调用方现编。取不到 → 记 gap，不许给默认值。
- `gaps` 和 `facts` 平级；`summary_for_context()` 输出进上下文的摘要版。
- `budget`：`tool_calls` / `production_calls` / `seconds` 三个计数器，
  **`production_calls` 独立**（`incident_investigate` 每次 sweep + `db_query` 各计一次；
  `dataset_query` 读本地 CSV，**不计入生产**）。

#### B2.1 只追加的通道 + 显式 reducer（≈50 行）

**问题**：一次 agent 调查里，后面的任务会看到前面任务的结论。
**不做的后果**：后面的任务可以**悄悄改写**前面记下的东西 —— 最典型的就是把一条
「我们不知道」升级成一条「事实」。这正是这个项目反复栽的那个形状（0% 被读成"关掉了"、
55 被读成"存不下引用"），只不过换成发生在一次调查内部，而且**没有任何痕迹**。
**做完的效果**：台账只能追加，改写这条路在 API 层面就不存在。

- 上面那些通道**全部只追加**。`budget` 和 `task_status` 是仅有的两个可变项，
  而且只能通过类型化的方法改（`spend(kind, n)` / `set_status(task_id, status)`）。
- 每个通道一个显式 reducer：`facts`/`gaps` 用 `append_unique(key_fn)` 去重
  （key = claim + 证据指针），事件类用纯 `append`。
- **没有 update，没有 delete，没有 setter。** 要修正一条已有的结论，只能用
  `supersede(entry_id, by=<新条目>, why="...")` —— 它**追加**一条新条目并指向旧的，
  **两条都留着**。出处因此永远追得回去。
- 单测必须锁住：`state` 对象上不存在任何能改写已有条目的公开方法（用 `dir()` 断言）。

#### B2.2 每个任务结束后打一次检查点（≈80 行）—— `webapp/agent_checkpoint.py`

**问题**：agent 模式一轮最长 180 秒。中间刷新页面、掉线、进程重启，全没了。
**不做的后果**：用户白等 2 分钟，而且**生产日志的 sweep 白打了**（那是同事的服务器）。
更麻烦的是出问题以后没法复盘 —— 只能看最终答案，看不到「第三个任务拿到什么才拐的弯」。
**做完的效果**：刷新能续、崩了能查；每一轮的执行过程都是可回放的。

- `checkpoint(turn_id, plan, state)`：**复用 `webapp/atomic_json.py`**
  （它本来就是为了 Windows 上 `os.replace` 偶发失败写的，正好是这里要的东西）。
- 位置 `webapp_data/agent_turns/`（`webapp_data/` 已在 .gitignore 里）；
  **按 owner(uid) 隔离**，和原始日志侧存同一套规矩 —— 别的浏览器读不到。
- TTL + 条数上限，照抄现有的 `INCIDENT_RAW_MAX_ENTRIES` / `INCIDENT_RAW_TTL_HOURS` 那对旋钮。
- 🔴 **检查点里绝不允许出现原始工具返回包**，只存台账摘要 + 证据指针。
  否则检查点文件就变成了生产日志在磁盘上的一份副本 —— 那正是出口脱敏闸门存在的理由。
  单测要专门验这一条：把一个带 PII 的假 packet 灌进去，断言落盘文件里搜不到它。
- ⚠️ **这一处是全项目唯一 fail-OPEN 的地方，要写注释说明为什么**：
  检查点写失败**绝不能弄死正在跑的这一轮** —— 发一条 `notice` 然后继续。
  别处一律 fail-closed，是因为「算不准就别发请求」；这里反过来，是因为
  「丢掉续跑能力」不是安全问题，而「把一次已经打过生产的调查搞崩」是实实在在的损失。

#### B2.3 `paused` / resume（≈60 行）

**问题**：「缺时区就停下来问用户」这件事今天是**一次性**的 —— 问完，这一轮就结束了，
用户回答之后是**从头再规划一遍**。
**不做的后果**：前面已经查到的东西全部重来，包括已经打出去的生产查询。
**做完的效果**：问完接着跑，不重来。

- 轮次状态：`running` → `done` | `paused` | `failed`。
- `paused` 带 `open_questions[]`，每条写明**是哪个字段导致的**、**缺的是哪一半**
  （「这个 03:15 是 HKT 还是 UTC」而不是笼统的「信息不足」）。
- **resume 契约**：同一个会话里，如果上一轮是 `paused`，用户的下一条消息按
  「对未决问题的回答」路由到 `agent_loop.resume(turn_id, user_reply)`，
  从检查点继续，**不重新规划**。
- 🔴 **预算必须跨越 resume 累计**：`production_calls` 和 `tool_calls`
  **接着上一段的数继续算**，不能重置 —— 否则「停一下再问」就成了绕过生产预算上限的后门。
  `max_seconds` 可以为新的一段重新计时（人思考的时间不算在里面），这一点要在代码里写清楚。
- 过期的 `paused` 轮次（超过 TTL）→ resume 明确拒绝并说明，改为重新规划。
- **两个存储，不同寿命，别混**：`session_store` 存的是给人看和回放的**摘要**；
  `agent_checkpoint` 存的是能**续跑**的执行状态。这正是设计文档 §3.1 第 7 条
  「执行状态和聊天记忆分开」落到代码上的样子。

**这三样的接线点**（各一行，别漏）：
B5 的调度循环在每个 task 结束后调 `checkpoint()`；D2 遇到 BLOCKING 类拒绝时把轮次置为
`paused` 而不是直接结束；E1 的面板要能渲染 `paused` 状态并给出「回答后继续」的入口；
A2 的 `session_store` 只存摘要，**不要把检查点也塞进去**。

### B3 角色→模型绑定（`sol` / `terra`）

**问题**：规划和执行对模型的要求不一样 —— 规划要能稳定出 JSON，执行要能稳定发 tool_calls。
**不做的后果**：只能一个模型两头凑；而且用户自己挂的模型可能哪头都不行，最后表现为
「agent 模式时灵时不灵」，谁都查不出是模型的问题。
**做完的效果**：每个角色一个模型，用户可选，不可用时明确回落并且**在面板上说出来**。

- 🟡 `webapp/config.py` 加一个只读函数 `current_llm_override()`，返回当前 contextvar 里的
  override（或 None）。**只加这一个函数，不动别的。**
- 🟢 `webapp/model_roles.py`：

  ```python
  @contextmanager
  def as_role(role):
      """把这一段的模型换成该角色配置的模型；退出时精确还原。"""
      # base = config.current_llm_override() or {}   ← 必须继承当前 override，
      #        否则 token 模式下 provider/credential_id 会丢
      # model = 用户选择 > config/agent_modes.json roles.<role>.model > "?"（不换）
      # 同时设置 model 和 selected_model 两个键（现有 override 是这么用的）
      # try/finally 里 config.reset_llm_override(token)
  ```

- **代码里绝不出现 `sol` / `terra`。** 它们是内网模型列表里的名字，属于「他们的环境」——
  五轮真机验证的教训全在这条线上（名字 → 响应形状 → 值格式）。名字只出现在
  `config/agent_modes.local.json` 和用户的选择里。
- **用户可选**：模型选择器里为每个角色各给一个下拉，选项来自现有的模型列表接口，
  选择按浏览器（uid）存，和现有的 per-user LLM 路由同一套。
- **上线前每个角色跑一次探针**：复用 `server.py` 里那个 `_probe_llm(override, tools=...)` ——
  它本来就能带 tools 探，正好用来验「executor 这个模型到底会不会发 tool_calls」。
  探针失败 → 该角色回落默认模型 + 发 `notice` 事件，**绝不静默**。
- 🟡 `llm_usage.add_call(total, message, model="", role="")`（两个新的可选参数，向后兼容），
  `usage` 里增加 `by_model` / `by_role` 汇总 —— **两个模型两个价钱，不能混成一个数**。

### B4 🟢 规划器

`webapp/agent_planner.py` + `prompts/agent-planner-prompt.md`。

- `plan_turn(question, history_summary, tool_catalog, policy) -> Plan`，在 `as_role("planner")` 里调一次模型。
- 提示词里给模型的**只有**：目标、可用工具的名字+一句话用途、预算、以及
  「必须先写信息需求（IR），每条 IR 要标 `required_evidence_tier`」。
  **不给工具参数细节** —— 参数是执行器的活。
- **降级路径（必须实现）**：JSON 解析/校验失败 → 带着错误信息重试**一次** →
  再失败就**降级到 ask 模式**，并在答案里明说「没能生成计划，按普通模式回答」。
  **绝不允许自己编一个计划顶上。**

### B5 调度 + 执行 + 合成

`webapp/agent_loop.py`（新）+ `prompts/agent-synthesis-prompt.md`（新）+ `agent.py` 一个分岔。

- **调度器纯代码**：算 ready 集合、查依赖、查三个预算、查停止条件。模型不参与
  「T1 做完没有」这类判断。**每个 task 结束后调一次 `checkpoint()`**（B2.2）。
- **`resume(turn_id, user_reply)`** 与 `answer_events` 并列的第二个入口：从检查点接着跑，
  不重新规划；预算按 B2.3 的规矩累计。`server.py` 侧：同一会话上一轮是 `paused` 时，
  下一条消息路由到这里而不是新开一轮。
- **执行器复用现有循环**：把 `agent.answer_events` 里那段「模型 ↔ 工具」的循环抽成
  `run_executor(objective, allowed_tools, budget, emit_kind) -> ToolResult[]`，
  agent 模式按 task 调用它，ask 模式原样调用它。**抽取要做到 ask 路径行为不变**
  （这就是 A6 基线存在的意义）。
- **合成器**在 `as_role("synthesizer")` 里跑，输入是**台账**不是原始 packet；
  产出必须满足：每条事实能追到一条证据；每个 gap 原样出现在答案里，并写明
  「是哪个字段这么说的、谁能补上」。
- `agent.py` 的分岔就一句：`mode == "agent" and policy.planning_enabled` → 委托 `agent_loop`。
  **`agent.answer()` 的返回契约只增不改**（`BACKLOG.md` 明确要求它稳定），
  新增 `plan` / `state_summary` / `mode` 三个键。

#### B5.1 🟢 按角色/任务裁剪工具 schema —— `webapp/tool_subset.py`

**这可能是整个 agent 模式里最大的一笔省钱，而且解法直接落在已有设计上。**

- **问题**：`tools.py` 里 17 个工具的描述加起来约 330 行散文（光 `incident_investigate`
  一个就有 90 行）。**这一坨每次模型调用都要付一遍**，粗算 ≈8k tokens；
  加上 33KB 的系统提示词（≈10k），每次调用的固定开销 ≈18k。
- **不改的后果**：ask 模式一轮调 1–2 次，忍了。**agent 模式一轮十几次，
  这就是每轮 20 万 token 的固定成本** —— 比所有工具返回的内容加起来还多，
  而且这笔钱买不到任何信息（那 90 行诚实规则跟"现在该不该调这个工具"多半无关）。
- **改完的效果**：每次调用只带这一步真正可能用到的工具。

做法（**不新增数据，只是把已有的 `tool_policy.prefer` 拿来当裁剪依据**）：

| 谁在调模型 | 带哪些工具 | 带到什么详细程度 |
| --- | --- | --- |
| 规划器 | **全部 17 个** | **只要名字 + 一句话用途**（新增一个 `short_description` 字段，或从现有描述取第一句） |
| 执行器（跑 task T） | 只带 `T.tool_policy.prefer` ∪ 少量必要兜底 | **完整描述**（那些诚实规则正是执行时要遵守的） |
| 校验器 / 合成器 | **一个都不带** | — |

- 兜底规则：`prefer` 为空或全被 `forbid` 掉 → 回落到一个配置里写死的小集合
  （`config/agent_modes.json` 里加 `fallback_tools`），**绝不回落到全量**。
- **ask 模式不裁**（保持行为不变，A6 基线要绿）。
- 🔴 **验收（硬指标）**：agent 模式下每次模型调用实际发出的工具数 **≤ 5**；
  单测直接断言传给 `llm.chat_stream` 的 `tools` 列表长度。
  另外记录一条改造前后的 token 对比，放进 RUNBOOK-81。

#### B5.2 出口证据闸门 —— 运行时拦，不只是评测断言

- **问题**：`every_claim_cited` 是**评测**断言 —— 事后才发现。
- **不改的后果**：合成器可以写出一句「因此受影响的是 361 个 use case」而证据指针指向一条
  **不存在的**台账条目，答案照样发出去了，下一次跑评测才知道。
- **改完的效果**：当场拦住。
- 做法：合成器的每句结论带证据 id；出口处逐条解析，**解析不了就不许输出那句话**
  （改成一条明写的 gap）。这和 `retriever/citations.py` 校验 `file:line` 是不是真实存在
  是同一件事，**推广到台账上**，尽量复用同一个报告结构。

---

### B6 计划先给人看，批准 / 改了再执行

**问题**：现在的设计是「规划完自动开跑」。而 agent 模式**能打生产日志**（同事的服务器）。
**不改的后果**：计划第一次跑歪，就是一串没意义的生产查询已经发出去了 —— 而且发出去
就收不回来，只能事后道歉。更日常的版本是：用户一眼就看出来「你查错服务了」，
但那时候两分钟已经花完了。
**改完的效果**：该确认的确认，能改的能改；用户从旁观者变成参与者。

- 🟢 `webapp/agent_approval.py`：把一份计划挂成「待批准」，等一个决定：
  `approve` / `edit`（改完再批）/ `reject`（放弃这轮）。
- **复用 B2.3 的机器**，不新造：「等批准」就是 `paused` 的一种状态，落的是同一个检查点，
  走的是同一条 resume 路径。**这也是为什么这项排在 B2 后面。**
- **默认策略（写进 `config/agent_modes.json`）**：
  `require_plan_approval: "when_production"` —— **计划里含生产调用时才要批准**，
  纯本地检索的计划直接跑。另两档 `"always"` / `"never"` 留给你们调。
- 用户能改的部分**限定在四种操作**（就是 D2 那四种，共用同一套实现）：
  删一个任务、加一个任务、改某个任务的 `tool_policy`、加一句约束（如「只看 hkp3」）。
  **不允许自由编辑 JSON** —— 用户改出一份非法计划，责任在我们这边。
- 🟡 接线：`server.py` 加一个批准/驳回的入口；`app.js` 在计划面板上给按钮。
- 验收：批准前 **`production_calls == 0`**（单测直接断言，这是这项存在的全部理由）；
  驳回后不留下任何半跑状态；批准后的计划哈希与展示给用户的**一致**
  （展示一份、执行另一份是这类功能最经典的骗局，要有测试盯着）。

### B7 会话级「已确认事实」槽位

**问题**：用户第 1 轮说了「时区是 HKT」，第 5 轮不该再问一次。
但历史只占 25% 车道，长会话里那句话早被裁掉了。
**不改的后果**：助手反复问同一件事 —— 这是最快消耗信任的一种表现，
而且在 agent 模式里更糟：一次自动调查会因为「缺时区」直接 `paused`，
而那个时区**用户十分钟前就说过了**。
**改完的效果**：说过一次就记住，且不受历史裁剪影响。

- 🟢 `webapp/turn_facts.py`：一张**类型化的小槽位表**，会话级，随会话持久化。
  **只允许这几类**（配置里可加，代码里不许塞自由文本）：
  `timezone` / `alert_time` / `target_repos` / `use_case_id` / `channel` / `source_system` /
  `log_sources`。
- 🔴 **只记用户明确说过的，不记模型推断的。** 每个槽位带 `said_at`（第几轮）和原句片段，
  用的时候要能说出「你在第 2 轮说过时区是 HKT」。**这条边界绝不能松** ——
  一旦允许推断值进来，它就变成了一个会自我强化的错误缓存，
  正是 §11.5 里说的「自动记住用户纠正」那个我们明确不做的东西的变体。
- 冲突处理：用户后来改口 → **追加新值 + 标记旧值被取代**（和台账的 `supersede` 同一套），
  永远用最新的，但旧的留着。
- 用在两处：规划器的输入（少列一条 IR）、`incident_investigate` 的参数补全
  （**但仍然只补用户说过的，绝不自己算「昨天」**）。

---

## 5. 逐项：CSV 数据源（C1–C2）

**问题**：UAT 库没接，只有导出的若干 CSV，而且**表述信息还没写**。
**不做的后果**：CSV 一旦以「裸表格」的形式进模型，就会重演已经犯过两次的错 ——
**给对方的字段安上我们以为的含义**（把 55 读成「存不下引用」、把 0% 读成「关掉了」）。
一份没有列说明的导出表，是这种错误最肥沃的土壤。
**做完的效果**：模型只能读**登记过的**数据集和**登记过的**列；没填说明的列会被明确标成
「未说明」而不是被猜；工具自己交出一份**排序好的填空清单**，你们照着填就行。

### C1 🟢 `config/csv_datasets.json` + `webapp/csv_registry.py`

契约**照抄 `config/db_queries.json`**（同一条缝、同样的 `.local.json` 覆盖、同样的
`"?"` = 没填 = 拒绝执行）：

```json
{
  "_README": [
    "从 UAT 导出的 CSV 数据集登记表 —— 内网维护。模型永远不写 SQL，也不能读没登记的文件。",
    "  - file          = 相对 SDLC_CSV_DIR 的文件名（默认 index/uat-exports/，已 gitignore）",
    "  - as_of         = 【必填】这份导出是哪一刻的快照。留 \"?\" 这条数据集【拒绝执行】。",
    "                    原因：CSV 是一个冻结的时刻，没有时刻的『0 行』既没意义又危险。",
    "  - columns       = 白名单。只有写在这里的列会进模型上下文和浏览器。PII 列不写=永远出不去。",
    "  - column_notes  = 每一列是什么意思、取值有哪些。**这就是现在还没填的那部分**，",
    "                    可以先留 \"?\" —— 助手会把它报成『未说明』，绝不会自己猜一个含义。",
    "  - source_table  = 这份导出来自哪张表，只用于标出处。",
    "  - caller_policy = product（聊天里的模型可调）/ internal（只有控制台）/ disabled。默认 internal。",
    "本文件是模板；盒子上改 config/csv_datasets.local.json（已 gitignore，自动优先读）。"
  ],
  "defaults": {"environment": "uat-export", "max_rows": 200, "caller_policy": "internal"},
  "datasets": {
    "<dataset_name>": {
      "file": "?", "as_of": "?", "source_table": "?", "description": "?",
      "columns": ["?"],
      "column_notes": {"?": "?"},
      "max_rows": 200, "caller_policy": "internal", "enabled": true
    }
  }
}
```

`csv_registry.py`：加载 + 合并 `.local.json` + `readiness()`（哪些数据集可用、
哪些卡在哪个 `"?"` 上）。**占位符判定直接复用 `retriever/glossary.py:is_unfilled()`，
不许写第二道闸门。**

### C2 `dataset_query` 工具

- 无参调用 → **目录 + 填空清单**（照 `db_query()` 无参返回目录的做法）：
  哪些数据集在、哪些可调、`as_of` 各是什么、**哪些列还没有说明**（按「缺得最多」排序，
  就是当初词典覆盖率报告的做法）。
- 带参调用：`dataset`、`filters`（列=值）、`contains`（列⊆子串）、`offset`、`limit`。
  **过滤在 Python 里做，不做任何表达式求值，不接受用户传入的表达式。**
- 返回包**照抄 `db_query` 的形状**，这样提示词里已经写好的那套诚实规则原样适用：

  ```
  ok, dataset, columns, rows, row_count, total, truncated, columns_dropped,
  environment: "uat-export", production_verified: false, as_of, source_table,
  undocumented_columns: [...],   # 这次答案里用到、但 column_notes 还没填的列
  state: not_wired | not_ready | refused | disabled   # ok:false 时
  ```

- **三条硬规则**（写进工具描述，模型必须照做）：
  1. **`as_of` 必须出现在答案里。** 这是导出快照，不是现在的库。
  2. **`undocumented_columns` 里的列，只能引用值、不能解释含义。**
  3. **不自动跨数据集 join。** 需要连表就由规划器开一个任务、两边证据分别入台账 ——
     一个 join 是一个论断，不能让它悄悄发生。（路由表两个方向只有一半重合，
     就是这个道理的现成例子。）
- 🟡 `tools.py` 加一个工具 schema + 一个 dispatch 分支；`prompts/qa-system-prompt.md`
  在 `db_query` 那一节**后面**加一小节（这是唯一允许动 ask 提示词的地方，
  因为它是新增能力，不是改现有行为 —— 加完要重跑 A6 基线确认没漂）。
- **不计入生产预算**（读的是本地文件），但**计入 `max_tool_calls`**。

---

## 6. 逐项：P2 校验与重规划（D1–D3）

### D1 🟢 `webapp/agent_validate.py`

| 层 | 判什么 | 谁判 |
| --- | --- | --- |
| L1 | JSON 能不能解析、必需字段在不在、`ok` 是什么 | 纯代码 |
| L2 | `done_when` 词表求值 + **本项目专属诚实检查**：`available:false` 不得计为 0；`matched:0` 必须落 gap；`known:false` 不得解释含义；占位符一律丢弃（复用 `is_unfilled`）；`points_seen:0` 不得读成「正常」 | 纯代码 |
| L3 | 只判「这个结果算不算回答了这条 IR」，**只允许输出 `pass`/`fail`/`need_more` + 一句理由**，不许改写事实 | 模型（`as_role("validator")`） |

**L3 每个 task 最多调一次**，而且只在 L1/L2 都过了之后才调。

### D2 🟢 触发器表 + 增量重规划

把设计文档 §5.5 那张表实现成一个**纯数据的映射**（`config` 里可调），
输入是工具返回包，输出是 patch。要点：

- **BLOCKING 类的窗口拒绝 → 不重试**，转成 `add_open_question`（问用户），
  并把这一轮置为 `paused`（B2.3）——**不是结束这一轮**，前面查到的东西要留着。
- 补丁只有四种操作；**没有「重画」这个入口**。
- 连续两次 replan 没有产生新证据 → 停，并在答案里说「换了两个方向仍无新证据」。

### D3 跑到一半能打断、能改向

**问题**：180 秒是很长的一段时间，而现在发出去就只能等。
**不改的后果**：用户看着面板上「正在查 hkl 的 exception.log」，明知道该查 hkp3，
却只能干等两分钟，然后**重问一遍 —— 而重问就是重来**，包括已经打出去的那几次生产查询。
**改完的效果**：当场改向，前面查到的留着。

- 🟢 `webapp/agent_steer.py`：两个动作 —— `cancel`（停，把已有台账交出来，
  **不是丢掉**）和 `steer(text)`（把用户这句话变成一个 patch）。
- 🔴 **只在 task 边界生效，不在任意时刻打断。** 一个正在飞的 MCP 请求不去中途掐 ——
  半路掐断一个生产查询，对面看到的是什么状态我们并不知道，而「不对他们的环境做假设」
  是这个项目最贵的一条教训。所以：标记 → 当前 task 收尾 → 应用。
- `steer` 产出的 patch **只能是 D2 那四种操作**，走同一套校验。
  改向后**发一个 `replan` 事件**并注明 `trigger: "user"` ——
  用户改的和系统改的必须在面板上分得清。
- `cancel` 之后仍然要跑合成器，给一份「查到这里」的答案 + 完整 gap 清单。
  **半途而废也要交出证据**，因为那几次生产查询已经花掉了。
- 🟡 接线：`server.py` 一个入口（按 `turn_id`，且校验 owner）；`app.js` 面板上两个按钮。

---

## 7. 逐项：P3 面板与验证（E1–E2）

### E1 🟡 计划面板

任务树（状态 / 证据数 / gap 数 / 耗时 / 用了哪个模型）+ 顶部「已查 N 秒 / 预算 X-Y-Z」计时条。
复用现有 subagent 面板的样式和折叠交互。**必须支持刷新回放**（数据来自 A2 落库的 `plan`）。

**`paused` 状态要单独渲染**（B2.3）：把未决问题原样显示出来 —— 哪个字段导致的、缺的是哪一半 ——
并明确告诉用户「回答后从这里继续，前面查到的不会重来」。这是这一版里用户最能直接感觉到的改进，
不要把它藏在一句普通的追问里。

面板上还要有三处交互（对应 B6 / D3）：**批准 / 改 / 驳回**（计划待批时），
以及运行中的**停止**和**改向**。改向产生的 `replan` 要标 `trigger: user`，
和系统自己触发的重规划在视觉上分得清 —— 谁改的，事后要能说清楚。

### E2 🟡 评测 + RUNBOOK-81

新增断言：`mode` / `plan_must_serve` / `must_record_gap` / `max_production_calls` /
`every_claim_cited` / `must_replan_on`。

新增 agent lane 用例，**至少覆盖这 8 条**：

1. 多跳影响面（改一个仓库 → 通知名单），断言 `every_claim_cited`
2. 渠道故障通报（业务面 + owner + 出口 + 日志），断言 `plan_must_serve`
3. 告警根因（必须多次 sweep），断言 sweep 次数 ≥ 2 且**每次改了什么**写在答案里
4. 缺时区 → 必须 `add_open_question`，且 `production_calls == 0`
5. `matched:0` → 必须 `must_record_gap`，禁止说「无业务影响」
6. 纯本地问题 → `max_production_calls: 0`
7. CSV：`as_of` 必须出现在答案里；`undocumented_columns` 的列不得被解释含义
8. 规划器返回坏 JSON（用桩模拟）→ 必须降级到 ask 并**明说**
9. 含生产调用的计划：**批准前 `production_calls == 0`**（B6）
10. 中途 `steer("只看 hkp3")`：后续查询确实只打 hkp3，且面板上标着 `trigger: user`（D3）
11. 中途 `cancel`：仍然交出「查到这里」的答案 + 完整 gap 清单，不是一句「已取消」（D3）
12. 用户第 2 轮说过时区，第 6 轮的日志任务**不许再问**（B7）
13. 每次模型调用发出的工具数 ≤5（B5.1，直接断言 `chat_stream` 收到的 `tools` 长度）
14. 长会话触发历史裁剪时，摘要进 `compaction` 车道；摘要失败要回落而不是整轮失败（A5.2）

`RUNBOOK-81-agent-mode-verify.md`：给内网的真机验证脚本，按老规矩写成
「跑什么命令 / 期望看到什么 / 看到别的怎么回报」。

---

## 8. 一次性检查清单（每个提交都过一遍）

- [ ] ask 模式行为没变（A6 基线绿）
- [ ] 新增的可调项进了 `config/*.json`，**没有硬编码进 Python**
- [ ] 没有把他们的名字/形状/格式写进代码（模型名、CSV 列名、表名一个都不许）
- [ ] 一个名字只有一个绑定（`from x import` 只拿常量；patch 打在拥有者模块上）
- [ ] 没碰 `webapp/llm.py`（facade merge 规则）
- [ ] 没碰 `incident_investigator` 的出口脱敏闸门、`db_readonly` 的 `caller_policy` 闸门
- [ ] 预算是 fail-closed 的：算不出预算就不发请求，而不是「先发了再说」
- [ ] 台账没有任何改写/删除的公开方法（要修正只能 `supersede`，两条都留着）
- [ ] 检查点文件里没有原始工具返回包（灌一个带 PII 的假 packet，断言落盘文件搜不到）
- [ ] resume 之后生产预算是**累计**的，不是重置的
- [ ] agent 模式每次模型调用发出的工具 ≤5，且 ask 模式**不裁**
- [ ] 展示给用户批准的那份计划，和实际执行的那份**是同一份**（哈希一致）
- [ ] 打断只在 task 边界生效，**没有半路掐断正在飞的 MCP 请求**
- [ ] 会话事实槽位里只有用户明确说过的值，**没有任何推断值**
- [ ] 没有引入任何第三方依赖（见 §11）
- [ ] 新代码能在 `LLM_MOCK=1` 下离线跑通测试

---

## 9. 风险与回滚

| 风险 | 兜底 |
| --- | --- |
| 规划器在弱模型上出不了合法 JSON | B4 的降级路径：重试一次 → 降级 ask + 明说 |
| 两个模型的账混在一起看不清成本 | B3 的 `by_model` / `by_role` 汇总 |
| agent 模式把生产日志打太多 | `max_production_calls` 独立计数 + 硬上限 + 面板实时显示 |
| 上下文爆掉 | A5 的 plan 车道 + 台账只进摘要 + A3 缓存 |
| 内网代码分叉导致改错位置 | S0 探针先行；21 项里绝大多数以新增文件为主，动现存代码的每项只动一个小接缝 |
| agent 模式每轮固定成本过高（工具 schema） | B5.1 按任务裁剪，硬指标每次调用 ≤5 个工具 |
| 计划跑歪，生产查询已经打出去 | B6 批准闸门（默认「含生产调用才要批」）+ D3 中途改向 |
| 整个 agent 模式要下线 | 一个配置开关：`modes.agent.planning_enabled = false` → 退化成「大预算的 ask」，**代码不用回滚** |

---

## 10. 关于回流（流程上的一句话）

代码现在由内网维护，而内网不能往这个仓库推。为了下一轮建议不再基于过时的代码，
**至少这两样要回流到外网仓库**：

1. `config/*.json` 的**模板**（不含真实值）——它们是契约，不是数据；
2. spec / RUNBOOK 文档，以及每一轮验证抓到的缺陷。

代码本身回不回流可以再说，但**契约一旦只存在于盒子里，外网这边的每一份建议都会开始漂**。

---

## 11. 为什么不引入 LangChain / LangGraph（结论已定，别再重开）

> 这一节是给**下一个接手的人**看的。「手动管状态很麻烦，装个框架吧」是个很自然的念头，
> 2026-08-16 认真评估过一次，结论是**不装**。理由在下面，**除非其中某一条不再成立，否则不要重开这个议题**。

### 11.1 先把「状态很麻烦」拆开

现在被一个词裹在一起的其实是四样东西：

| 在管的东西 | 框架能不能替我们管 |
| --- | --- |
| 聊天记录、会话标题、按浏览器隔离、原始日志侧存 | **不能**。这是产品 UX，框架不碰 |
| 上下文预算分车道 + **按结构**裁剪工具返回包 | **框架不如我们现在的**（见 11.2） |
| 执行状态（哪个任务完成、证据台账） | 能 ✅ |
| 跑一半崩了/刷新了怎么续 | 能 ✅ |

**只有两样是框架的强项，而这两样就是 B2 那 200 行。**

### 11.2 我们已经领先框架的一处

LangChain 的历史裁剪是**按消息**裁的（丢整条、或者切文本），它**不理解工具返回包的 JSON 结构**。
我们的 `context_budget.fit_tool_result` 是**按结构**裁的 —— 当初正是因为按字节切，
模型拿到半截 JSON 却认不出那是半截的。**这块迁过去是降级，不是升级。**

### 11.3 LangGraph 真正会给我们的三样（不贬低它）

断点续跑、中断问人、带 reducer 的只追加状态通道。这三样是真的有价值 ——
**所以我们把它们自己实现了，就是 B2.1 / B2.2 / B2.3**，而且零件已经有一半：
`atomic_json.py` 就是现成的检查点落盘，「BLOCKING 拒绝 → 问用户」就是现成的中断语义。

补一句技术判断：LangGraph 最擅长**编译期定死的图**；我们的任务图是规划器**每次现生的数据**，
这种动态图在它那里要走 Send API，反而别扭 —— **调度器无论如何都得自己写**。

### 11.4 在这个环境里的代价（决定性的五条）

1. **依赖树 vs 断网银行。** LangChain+LangGraph 会拖进一棵传递依赖（pydantic v2、httpx、
   tenacity、jsonpatch 之类）。审一个包是个项目，审一棵树并跟住它的版本变动是一份长期税。
   钉死一个升不动的旧版本，比标准库更糟。（`BACKLOG.md` 守则：优先标准库，pip 可能被封。）
2. **provider 层白写，还得再包一层。** Copilot direct / Copilot responses / OpenAI chat、
   token 与 tunnel 双模、凭据存储、按用户路由、auth 错误归一化、streaming 降级 ——
   LangChain 的 ChatModel 抽象接不住这套凭据模型，结局是**留着现有层 + 再加一个适配器**。
3. **1355 个测试。** 里面编码的是五轮真机验证抓到的缺陷（`hk1`→`hkl`、响应形状、
   `alert_time` 格式、alarm name 泄漏）。**这是这个仓库最值钱的东西**，重写编排会作废一大片。
4. **控制流就是我们的安全模型。**「缺时区 → 零次调用」「`caller_policy` 是引擎硬闸门」
   「出口按值整包指纹化」—— 这些保证今天靠**读一个函数**就能证明。
   搬进别人的运行时之后，「没有任何代码路径能绕过这道闸门到达生产」这句话就难证明了。
   在银行里，这句话能不能证明，值很多钱。
5. **LangSmith 用不了。** 很多人上 LangGraph 是冲着它的可观测性 —— 那是托管服务，不能出网。
   这部分收益直接归零。

### 11.5 什么情况下才重开这个议题

四条**全部**满足才值得再谈；目前**一条都不满足**：

- 要做真正的多 agent 编排（复杂 fan-out/join、跨长流程）—— 我们明确不做，
  现有唯一的子代理是按「原始日志不能进主上下文」拆的，理由是数据边界不是工具数量；
- 环境不再断网，且依赖审批变便宜；
- 是绿地项目，还没有 provider 层和这批测试；
- 团队规模大到需要一套通用词汇来降低上手成本。

**一句话：我们缺的从来不是编排框架，是 B2 那 200 行状态原语。**

---

## 12. 候选项：等 P1 跑出真实数据再定（**先别做**）

这三条评估下来是**有价值但要过一道门槛**的。记在这里是为了不丢，
**不是让人现在就去做** —— 门槛能不能过，要 P1 上线后的真实数据说了算。

### 12.1 第二个子代理：宽搜索的「探索者」

原来的规矩是「**按真实的隔离理由拆子代理，不按工具数量拆**」。
唯一那个子代理的理由是「原始日志不能进主上下文」。

**上下文经济是第二个真实理由**：`search_code` 扫 460 个仓库、翻 880 个 use case ——
这类任务**扇出很宽、结论很窄**，放主上下文烧 5 万 token，放子代理只回来 500 token 的结论。

**门槛（写死，达不到就直接调，不许加这一层）**：预期结果 > 8k token **且** 结论 < 1k token。
P1 跑完统计一下真实的工具返回体积，够不够格一看便知。

### 12.2 确定性的「上下文包」构造器

aider 的做法：按当前问题里的实体，在代码图上排序，出一份**有 token 预算上限**的仓库地图喂给模型。

我们的资产更好（依赖图 + 消息图 + 仓库标签 + use case 目录），但**今天的"narrow first"
是靠提示词求模型自觉**（`qa-system-prompt.md` 第 1 条）。做成一个确定性函数：
问题实体 → 排过序、有上限的上下文包 → 喂给规划器。

**规划器的输入质量决定整份计划的质量**，这是投入产出比可能最高的一处 ——
但要等 P1 攒够「计划本身质量不行」的实例，才知道值不值得。

### 12.3 声明式的 pre-tool 闸门

`config/tool_gates.json`：某个工具在什么条件下**直接拒绝**（不进 dispatch）。

**价值在处境**：内网不能推代码，今天加一条拒绝规则要改 Python；有了它就是改一行配置。

**只做「拒绝」这一种，不要做完整的 hook 体系。** 一旦允许配置里跑逻辑，
这个文件就会变成第二套代码，而且是没有测试的那一套。

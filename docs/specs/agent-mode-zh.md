# Agent 模式改造方案（ask / agent 双模）

> **这份文档只定方向和契约，不含逐行实施细节。** 批准之后再出「改哪个文件、加哪个函数、写哪些测试」的实施清单。
>
> 写作依据：2026-08-15 实际读过的代码 —— `webapp/agent.py`、`webapp/tools.py`、
> `webapp/incident_plan.py`、`webapp/incident_investigator.py`、`webapp/context_budget.py`、
> `webapp/server.py`、`webapp/session_store.py`、`webapp/static/app.js`、`prompts/qa-system-prompt.md`、
> `evals/cases.jsonl`、`config/`。文中所有行号都是这次实测的行号。

---

## 0. 一句话

今天的助手做的是**一次问答**：模型看一眼问题，挑一个工具，拿到结果就作答。

agent 模式要做的是**一次调查**：先把「要回答这个问题必须先知道哪几件事」列成清单，逐条去取，
边取边核，取不到就明说取不到、换一条路，最后把结论和每一条证据绑在一起交出来。

**关键判断：这个项目缺的不是「循环」，循环已经有了。缺的是目标分解、状态台账、和验收标准这三样。**

---

## 1. 现状盘点（实测，不是印象）

| 事实 | 在哪 |
| --- | --- |
| **已经是一个多轮循环**，最多 8 轮，每轮可以调多个工具 | `webapp/agent.py:161`，上限 `config.MAX_TOOL_ITERS`（`webapp/config.py:112`，默认 8） |
| 约束模型「只调一次工具就答」的**不是代码，是提示词** | `prompts/qa-system-prompt.md`（454 行，绝大部分是「什么场景调哪个工具 + 怎么诚实报告」） |
| 模型可见工具 15 个，其中 14 个是本地确定性检索，1 个是子代理 | `webapp/tools.py:149-485`，`SUBAGENT_TOOLS` 在 `:490` |
| 上下文预算已经分车道：history 25% / compaction 5% / tools 50% / subagent 20% | `webapp/config.py:132-137`，实现在 `webapp/context_budget.py` |
| 前端事件协议已有：token / tool_start / tool_end / view / subagent_step / done / error | `webapp/static/app.js:1765-1800`，服务端出口 `webapp/server.py:759` |
| 会话已持久化 tool_trace / usage / citations / views / subagent_steps | `webapp/session_store.py:205` |
| 评测集 39 条，已有 `must_call_tools_any` / `max_tool_calls` / `citations_must_verify` 等断言 | `evals/cases.jsonl`、`evals/run.py` |

### 1.1 最重要的一条：**我们已经有半个 agent 了，而且它是对的**

`webapp/incident_plan.py:422 plan()` + `webapp/incident_investigator.py:1053 investigate_events()`
做的正是教科书里那一套，而且是被真机验证过五轮的那一套：

- **结构化的计划对象**（targets / keywords / window / sources / refusals + 三条互不阻塞的分支），
  不是一段自然语言；
- **预算**（`max_queries`）和 **fail-closed 闸门**（缺时区 → 计划不可跑 → **零次调用**，RUNBOOK-61）；
- **出口闸门**：只有分类结果和脱敏摘要离开进程；
- **过程可见**：逐步 `subagent_step` 事件流给前端；
- **它不是模型即兴决定的，是代码算出来的**——模型只负责「要不要发起」和「怎么解读」。

> **所以本方案的第一原则：agent 模式不引进新框架，而是把这套已经跑通的东西升一层。**
> 上层的 Plan 和下层 incident 的 Plan 是同构的，只是尺度不同。

---

## 2. 为什么要做（三个真实场景，每个都讲清楚不改的后果）

### 场景 A：「SMS 渠道出问题了，现在影响谁？」

- **今天怎么答**：调一次 `show_arch(channel=sms)`，画个图、讲一下受影响链路，停。
- **缺什么**：受影响 use case 的业务 owner 是谁（`source_system_impact`）、走哪个厂商出口
  （`usecase_impact.delivery_chain`）、这些仓库里哪几个最不能出问题（`critical_repos`）、
  生产日志现在到底在报什么（`incident_investigate`）。这四件事今天全靠用户一句一句追问。
- **不改的后果**：值班的人要问 5 轮才能凑出一份能发出去的通报，而且**每轮之间的口径对不上**
  （某一轮说的 45 和另一轮说的 361 是两个不同口径，中间没人交代）。真出事的时候，
  「凑」这个动作本身就是事故放大器。
- **改完的效果**：一次提问，出一份带出处的通报 —— 谁受影响（多少个、按哪个口径）、
  谁该被通知（分层 owner）、出口是谁、日志现在说什么、**哪些没查到、下一步要谁给什么**。

### 场景 B：「我要改 `mc-hk-hase-xxx`，要通知谁 / 风险多大」（旗舰需求）

- **今天怎么答**：`impact(repo)` 给出上下游数字，渠道证据分层也带出来了。到此为止。
- **缺什么**：下游仓库承载哪些 topic → 这些 topic 上有哪些 use case → 这些 use case 的业务
  owner 是谁；这个仓库在 `critical_repos` 的哪一根轴上排在前面；有没有已知的数据质量问题会被
  这次改动引爆。
- **不改的后果**：「通知名单」这件事永远差最后一跳 —— 我们能说出「32 个仓库会被连累」，
  但说不出「所以要通知这 7 个业务 owner」。而后者才是人要的东西。
- **改完的效果**：一条从「代码改动」到「要发给谁的通知名单」的完整链，每一跳都有出处，
  **断在哪一跳也写清楚**。

### 场景 C：「这个告警什么原因、影响多大」

- **今天怎么答**：提示词里已经写了 `incident_investigate` 可以在同一轮反复调
  （`prompts/qa-system-prompt.md:310`），但**调不调、调几次全看模型当时的心情**。
- **不改的后果**：同一个告警问两次，一次查了 3 轮说「找到 ConnectException」，一次查了 1 轮
  说「日志里没发现异常」。后者不是错的答案，是**没查完的答案伪装成结论** —— 这正是这个项目
  过去所有翻车的共同形状。
- **改完的效果**：sweep 次数、每次改了什么、哪次有结果，是计划里的任务节点；
  「还要不要再来一轮」由**代码**依据 packet 里已有的字段判定
  （`queries_executed < queries_attempted`、`app_resolved` 为空、`evidence` 为空但计划可跑）。

---

## 3. 对 ChatGPT 那份建议的取舍

你说「ChatGPT 说的未必正确、未必完整」——是的。按这个项目的实际情况分成三堆：

### 3.1 照单全收（这些是对的，而且和本项目已有的做法同构）

1. **Plan 是可执行的任务图，不是一段自然语言 checklist。**
2. **Planner 只决定 WHAT，Executor 决定 HOW。** —— 正好复用现有接缝：
   「我们出查询计划，他们执行」。
3. **每个 task 必须有 completion criteria**，否则「做完了没有」无人可判。
4. **确定性判断交给代码，不确定性判断交给模型。** 这条在本项目已经是硬规矩
   （fail-closed 闸门都写在代码里，不写在提示词里）。
5. **Replan 是打补丁，不是重新生成整份计划。**
6. **预算意识 + 明确的停止条件**，否则 agent 会自己转圈。
7. **执行状态（ExecutionState）和聊天记忆（ConversationMemory）分开。**
8. **先把单 agent 做透，不急着 multi-agent。** 本项目只有 1 个子代理，而且是按
   「原始日志绝不能进主上下文」这个真实理由拆的，不是按工具数量拆的。**保持。**

### 3.2 改造后才收（原话方向对，但对本项目不够）

9. **「先有 Information Requirement，再选 Tool」** —— 方向对，但这里要再进一步：
   IR 不能只写「我需要知道 X」，必须写成 **「我需要知道 X，且证据必须达到 E 级」**。
   理由很实在：这个项目每一次翻车都不是「没查」，而是**查了，但把弱证据当强证据说**
   （0% 说成「关掉了」、55 说成「存不下」、名字推断说成代码证据）。所以 IR 要带
   `required_evidence_tier`。
10. **Assumption Ledger（假设台账）** —— 收，但假设不该由模型自由发挥。
    **本项目的假设来源应该是工具已经返回的那些诚实字段**：`available:false`、`matched:0`、
    `known:false`、`scanned_without_evidence`、`vendor_selection.method == channel_upper_bound`。
    这是一份现成的、被真机验证过的「我们不知道」清单。模型可以新增假设，但必须标
    `source: model`，且**不得作为任何结论的唯一支撑**。
11. **DAG + 并行** —— DAG 收，**并行暂不收**。理由：单进程、只能用标准库；本地检索工具是
    毫秒级的，真正慢的只有 MCP 那两条，而那两条恰恰是最不该并发打过去的（对面是同事的服务器）。
    DAG 在这里的价值是「依赖清楚 + 能局部重规划」，不是吞吐量。

### 3.3 明确不做

12. **二十几个字段的 Task schema** —— 砍到 8 个字段。工具只有 15 个，任务图通常 3–6 个节点；
    字段一多模型填不准，**填不准的字段比没有更糟**（它会被下游当真）。
13. **Goal Manager / Scheduler / Evaluator / Replanner 各做成一个 Agent** —— 不做。
    它们是**函数**，不是 agent。多一次模型调用就多一次幻觉机会、多一份 token、多一段延迟。
14. **默认用 LLM 当 Evaluator** —— 不做默认。三层校验里能用代码判的一律代码判（见 §5.4）。
15. **引入通用编排框架（LangGraph 之类）** —— 不做。断网 + 优先标准库是硬约束
    （`BACKLOG.md` 守则）。六个小文件够了。

---

## 4. 架构：**模式即策略**，一个循环两套策略

```
                    user query  +  mode(ask | agent)
                               │
                 ┌─────────────┴──────────────┐
                 │  TurnPolicy (config 驱动)   │  预算 / 迭代上限 / 是否允许规划 /
                 └─────────────┬──────────────┘  是否允许生产调用 / 停止条件
                               │
   ask ─────────────► 现有 agent.answer_events（行为一字不改）
                               │
   agent ──► Goal 归一 ──► Planner（模型，1 次）──► Plan(JSON) ──► 代码校验
                                        │
                                  ┌─────▼─────┐
                                  │ Scheduler │  纯代码：算 ready 集合 / 查预算 / 去重
                                  └─────┬─────┘
                                        │  一次一个 task
                                ┌───────▼────────┐
                                │    Executor    │  ＝ 现有工具循环，
                                │  (ReAct，复用) │     只是把 objective 换成该 task
                                └───────┬────────┘
                                        │  ToolResult
                                ┌───────▼────────┐
                                │   Validator    │  L1 代码 / L2 代码 / L3 模型（少量）
                                └───────┬────────┘
                                 pass   │   fail / refusal
                                        │
                                ┌───────▼────────┐
                                │   Replanner    │  只出 patch，绝不重画整份
                                └───────┬────────┘
                                        │
                                ┌───────▼────────┐
                                │  Synthesizer   │  结论 ← 证据台账，逐条绑定
                                └────────────────┘
```

**六个新模块**（都放 `webapp/`，都能离线测；括号里是硬性依赖约束，为的是可测性）：

| 文件 | 职责 | 约束 |
| --- | --- | --- |
| `webapp/turn_policy.py` | 模式 → 预算 / 上限 / 开关；读 `config/agent_modes.json` | 不 import llm |
| `webapp/agent_plan.py` | Plan / Task 数据结构、JSON schema 校验、patch 应用 | **不 import llm，不 import tools** |
| `webapp/agent_state.py` | 证据台账、gap 台账、假设台账、预算计数 | **不 import llm** |
| `webapp/agent_planner.py` | 唯一一处调用模型做规划 / 重规划 | 只出 Plan 或 Patch |
| `webapp/agent_loop.py` | 调度 + 执行 + 校验的编排；agent 模式的 `answer_events` | 复用现有 executor |
| `webapp/agent_validate.py` | 三层校验；L1 / L2 纯代码 | L3 才允许调模型 |

`webapp/agent.py` 只加一个分岔：`mode == 'agent'` 时委托给 `agent_loop`，其余原样。
**`agent.answer()` 的返回契约不变**（`BACKLOG.md` 明确要求它稳定）——新字段只增不改。

---

## 5. 六个关键决策（这一节是要你批的核心）

### 5.1 Plan 的数据结构（精简版）

```json
{
  "goal": {
    "objective": "SMS 渠道故障：确定业务影响面、通知名单、当前日志证据",
    "deliverable": ["受影响 use case 及口径", "分层 owner 名单", "厂商出口", "日志证据", "未知项清单"],
    "success_criteria": ["每个结论都能追到一条工具证据", "每个未知都写明是哪个字段说的"]
  },
  "constraints": {
    "max_tasks": 6, "max_tool_calls": 24, "max_production_calls": 4,
    "max_seconds": 180, "allowed_tools": ["..."], "forbid_tools": ["db_query"]
  },
  "information_requirements": [
    { "id": "I1", "need": "SMS 渠道上配置了哪些 use case", "priority": "P0",
      "required_evidence_tier": "snapshot",
      "acceptable_gap": "上界即可（channel_upper_bound），但必须说成上界" }
  ],
  "tasks": [
    { "id": "T1", "objective": "取 SMS 渠道的受影响链路与 use case 集合",
      "depends_on": [], "serves": ["I1"],
      "tool_policy": { "prefer": ["show_arch", "search_usecases"], "forbid": ["search_code"] },
      "done_when": ["result.ok == true", "has_field:count"],
      "status": "pending", "attempts": 0 }
  ],
  "assumptions": [
    { "id": "A1", "statement": "路由快照覆盖该 topic", "source": "tool", "confidence": "low",
      "from_field": "use_case_link.available" }
  ]
}
```

三个要点：

- **`tool_policy` 只给候选和禁用，不给参数。** 参数是 Executor 的活。理由是血的教训：
  参数属于「他们的环境」，四轮真机验证抓到的缺陷全在这条线上（名字 → 响应形状 → 值格式）。
  抽象的一侧我们留着，**具体的名字、形状、格式一律让拥有它的那一侧决定**。
- **`done_when` 必须是代码能判的东西**，不能是「研究透彻」。给一个受限判定词表
  （`result.ok == true`、`has_field:X`、`count > 0`、`gap_recorded:X`），模型不能写自由代码。
- **`required_evidence_tier` 是本项目独有的一栏**，见 5.3。

### 5.2 调度器是代码，不是模型

- ready 集合、依赖检查、预算检查、停止条件 —— 全部纯代码。模型不参与「T1 做完没有」这类判断。
- **同一轮内的工具结果缓存（新代码，必须有）**：key = `(tool, 规范化后的 args)`。
  agent 模式一定会出现两个任务都需要同一个 `usecase_impact` 的情况，不缓存就是双倍 token
  和双倍延迟。ask 模式今天没有这个问题（通常只调一次），所以这是新增能力。

### 5.3 证据台账 + 证据等级（复用已有的分层，不新造）

`prompts/qa-system-prompt.md:361` 那张表已经定义了五档关系，直接沿用为证据等级：

```
direct_code_evidence > direct_config_evidence > business_declared > message_carried > name_derived
```

IR 声明「我需要哪一档」，Validator 判定「拿到的够不够」。**不够就写进 gaps，绝不许降档冒充。**

State（**和 chat history 分开存，不进 messages**）：

```json
{
  "facts": [
    { "id": "F1", "claim": "SMS 渠道上有 361 个 use case",
      "evidence": [ { "tool": "search_usecases", "args": {...}, "field": "count",
                      "value": 361, "tier": "business_declared", "environment": "uat" } ] }
  ],
  "gaps": [
    { "id": "G1", "what": "这些 use case 的生产路由", "why_unknown": "use_case_link.available == false",
      "who_can_close_it": "内网（需要同环境路由表）" }
  ],
  "assumptions": [ ... ],
  "budget": { "tool_calls": 7, "production_calls": 1, "seconds": 42 },
  "task_status": { "T1": "done", "T2": "failed" }
}
```

> **`gaps` 是一等公民，和 `facts` 平级。** 这是这个项目的命门：`available:false`、`matched:0`、
> `points_seen:0`、`record_found:false`、`not_investigated`、`scanned_without_evidence` ——
> 今天靠提示词求模型别说错，agent 模式里应该变成**结构化的 gap 记录**，
> Synthesizer 有义务原样交出去。

### 5.4 三层校验：能用代码判的，绝不问模型

| 层 | 判什么 | 谁来判 |
| --- | --- | --- |
| **L1 结构** | JSON 能不能解析、必需字段在不在、`ok` 是什么、有没有 `count` | 纯代码 |
| **L2 语义-确定性** | `done_when` 受限词表求值；**本项目专属诚实检查**：`available:false` 不得计为 0、`matched:0` 必须落 gap、`known:false` 不得解释含义、占位符 `TBC`/`???`/空 一律丢弃（**直接复用 `retriever/glossary.py:44 is_unfilled()`，不写第二道闸门**） | 纯代码 |
| **L3 语义-模型** | 只判「这个结果算不算回答了这条 IR」；**只允许输出 `pass` / `fail` / `need_more` + 一句理由**，不允许它改写事实 | 模型，少量 |

### 5.5 Replan 的触发器 ＝ 系统里**已经存在**的诚实信号

这是本方案最省事也最可靠的一处设计：**重规划不靠模型「觉得不对」，靠字段。**
而这些字段全部是真机验证过的。

| 信号 | 来自 | 补丁动作 |
| --- | --- | --- |
| `ok: false` | 任意工具 | 换工具，或直接落 gap |
| `use_case_link.available: false` / `matched: 0` | `incident_impact` | 新增一个走 `channel_upper_bound` 的替代任务 **+ 记 gap** |
| `truncated: true` | `search_usecases` / `source_system_impact` | 新增分页任务（`offset`/`limit`） |
| `callers.available: false` | `unified_impact` | 新增 `search_code` / `read_file` 兜底任务 |
| `queries_executed < queries_attempted` | `incident_investigate` | 再跑一次 sweep（换 `keywords` / `sources`） |
| `plan.refusals` 含 **BLOCKING window** | `incident_investigate` | **不重试**，转成一条「问用户」的 open question |
| `vendor_selection.method == channel_upper_bound` | `usecase_impact` | 停止找厂商，改为把上界写成上界 |
| `evidence_available: false` / `scope_known: false` | `channels` 块 | 记 gap，**禁止**说「没有渠道」 |
| `columns_dropped` / `ok:false` | `db_query` | 记 gap，不得当成「查无此记录」 |

补丁操作只有四种：`replace_task` / `add_task` / `drop_task` / `add_open_question`。
**永远不允许「重新生成整份 Plan」**——那会导致计划漂移，而漂移的计划没法审计。

### 5.6 预算与停止条件：**两类预算必须分开计**

- **本地检索调用**：便宜，上限可以放宽（建议 24 次）。
- **生产 / 外部调用**（`incident_investigate` 的每次 sweep、`db_query`）：**单独计数**，
  默认上限很小（建议 4 次），超了必须停下来问用户。
  理由很具体：对面是同事的服务器和 UAT 库，且一次 log read 要 3–10 秒。
- **停止条件**（满足任一即停）：所有 P0 IR 有结论（含「结论是 gap」）｜ 预算耗尽 ｜
  连续两次 replan 没有产生新证据 ｜ 出现 BLOCKING 类拒绝。
- **绝不允许**：agent 模式在用户没要求的情况下自动升级到生产调用。默认 agent 模式
  **只用本地工具**；生产分支要么用户显式打开，要么在计划里作为「需确认」的任务出现（见 §9）。

---

## 6. 与现有代码的接缝（逐文件）

| 文件 | 改什么 | **不许改什么** |
| --- | --- | --- |
| `webapp/agent.py` | 加 mode 分岔；把现有循环抽成可复用的 executor | `answer()` 的返回契约；**ask 模式的行为要逐字节不变** |
| `webapp/server.py:759` | 请求体新增 `mode` 字段（缺省 `"ask"`），透传给 `answer_events` | 现有事件流的形状 |
| `webapp/session_store.py:205` | 新增持久化字段 `mode` / `plan` / `state_summary` | 已有字段的含义 |
| `webapp/context_budget.py` + `config.py:132` | 新增 `plan` 车道（建议 tools 50→40，plan 10）；进上下文的 plan/state **必须是摘要不是原始 packet** | 现有车道的默认值（ask 模式不受影响） |
| `webapp/static/app.js:1765` | 新增 `plan` / `task_start` / `task_end` / `replan` 事件渲染，复用现有 subagent 面板样式 | 现有事件分支 |
| `prompts/` | **新增** `agent-planner-prompt.md`、`agent-synthesis-prompt.md` | **不动 `qa-system-prompt.md`** —— ask 模式是基线，动它等于两个模式一起漂 |
| `config/agent_modes.json` | 新增，**committed**，配 `.local.json` 自动优先覆盖 | 内网不能 push，所有可调项必须走这条缝（`BACKLOG.md` 守则） |
| `evals/` | 新增 agent lane 用例 + 新断言 | 现有 39 条在 `mode=ask` 下必须逐条不变 |
| **完全不许碰** | `webapp/llm.py`（facade merge 规则）、`incident_investigator` 的出口脱敏闸门、`db_readonly` 的 `caller_policy` 闸门 | |

---

## 7. 本项目特有的 7 个坑（每个都会真炸）

1. **中间轮的 token 已经流给前端了。** `webapp/agent.py:164-168` 每一轮都把 delta 当 `token`
   事件发出去；只有最后一轮（无 tool_calls）才算答案，但前面几轮模型如果先说了话，
   用户已经看见了。agent 模式里 Planner 输出的是 JSON ——
   **必须把规划 / 执行阶段的模型输出改成不进 answer 流**，否则用户会看到一坨 JSON。
   这是 P0 级实现要求，不是优化项。
2. **计划 JSON 解析不了怎么办。** BYO-LLM，用户可能挂一个很弱的模型。规矩：
   带错误信息重试一次，再失败就**降级到 ask 模式并明确告诉用户「没能生成计划，按普通模式回答」**。
   **绝不允许自己编一个计划**。容错思路复用 `webapp/agent.py:49 _json_from_text`。
3. **同轮工具去重 / 缓存**（见 5.2）。不做，预算直接翻倍。
4. **上下文膨胀。** 6 个任务 × 每个都把完整 packet 塞回 `messages` ＝ 必爆。
   进上下文的只能是台账摘要（fact 的 claim + 证据指针），原始 packet 留在进程内存里，按 id 取。
5. **8 轮上限是 ask 模式的数**（`config.py:112`）。agent 模式要独立上限，
   而且**总要有一个代码层的硬上限**，不能只靠模型自觉。
6. **子代理已经有自己一套 plan / refusal 词表**（`incident_plan.py`）。
   上层 Plan **不要重新定义一遍** —— 上层任务节点直接把子代理的 `refusals` 吸收成 gap。
   两套词表打架，就是下一次翻车。
7. **UAT / prod / snapshot 三种环境不能在台账里混。** 每条证据必须带 `environment`，
   而且这个值由**工具结果自带的字段**填进台账，模型只能引用、不能改写。

---

## 8. 分四期交付（每期可独立上线、独立验收）

### P0 —— 管道（不含任何规划能力）

- **做**：mode 端到端透传（前端开关 → server → agent）；`TurnPolicy` + `config/agent_modes.json`；
  同轮工具缓存；`plan` 车道占位；session 新增持久化字段；事件协议扩展；前端面板骨架。
- 此时 agent 模式 ＝ ask 模式 ＋ 更大预算 ＋ 一句「把该查的查完再答」。
- **验收**：现有 39 条 eval 在 `mode=ask` 下**逐条不变**；agent 模式在三个多跳问题上
  工具调用数明显上升、答案覆盖面变宽。
- **为什么先做这一期**：它不引入任何模型不确定性，全是可测的管道 —— **先把回归防线立起来**，
  后面三期才敢动。

### P1 —— Planner ＋ 调度器 ＋ 台账

- **做**：`agent_plan` / `agent_state` / `agent_planner` / `agent_loop`；Planner 一次调用出
  goal + IR + DAG；调度器纯代码；执行复用现有循环；台账记 facts / gaps / budget；
  Synthesizer 出结论。**不含 replan。**
- **验收**：计划 JSON 100% 要么通过 schema 校验、要么降级；每条结论都能追到台账里的一条证据；
  P0 级 IR 覆盖率 100%（没覆盖的必须以 gap 形式出现在答案里）。

### P2 —— Validator ＋ 增量 replan

- **做**：L1 / L2 代码校验 ＋ L3 少量模型校验；§5.5 那张触发器表；
  假设台账从工具字段自动生成；四种 patch 操作。
- **验收**：造 6 个「必然触发 replan」的场景（缺时区、`matched:0`、`truncated`、
  `callers` 不可用、生产预算耗尽、sweep 没跑满），每个都必须产生正确的补丁，
  **且不重画整份计划**。

### P3 —— 面板 ＋ 评测 ＋ RUNBOOK

- **做**：前端计划面板（任务树 ＋ 每个任务的状态 / 证据数 / 耗时 / gap）；
  agent lane 的 eval 断言；`RUNBOOK-81` 交内网做真机验证。
- **验收**：内网跑 RUNBOOK-81 全绿；agent 模式答案里每一条「我们不知道」
  都能点开看到**是哪个字段这么说的**。

---

## 9. 评测：在现有断言上加（`evals/cases.jsonl`）

| 新断言 | 判什么 |
| --- | --- |
| `mode` | `"ask"`（缺省）/ `"agent"` —— 缺省保证 39 条老用例行为不变 |
| `plan_must_serve` | 计划里必须有任务服务于这些信息需求 |
| `must_record_gap` | 必须把某个未知记成 gap，而不是记成 0 |
| `max_production_calls` | 本地问题不许打生产（期望 0） |
| `every_claim_cited` | 结论里每条事实都能在台账里找到证据 |
| `must_replan_on` | 该重规划时必须重规划（如 `missing_timezone`） |

**回归红线：现有 39 条在 `mode=ask` 下必须逐条不变。**（提醒一句 —— RUNBOOK-66 的教训是
「用例红了，第一嫌疑人是断言不是模型」，新断言要先自测。）

---

## 10. 明确不做的事（以及为什么）

| 不做 | 理由 |
| --- | --- |
| multi-agent 编队（Planner Agent / Research Agent / Critic Agent…） | 现有唯一的子代理是按「原始日志不能进主上下文」这个真实理由拆的。按工具数量拆 agent 只会换来延迟、成本和无法调试 |
| 引入通用编排框架（LangChain / LangGraph） | 2026-08-16 认真评估过：断网银行环境里依赖树是长期税、provider 层接不住、1355 个测试会作废一大片、而闸门保证靠「读一个函数就能证明」——**但它的三样好东西我们自己实现**（只追加通道 / 检查点 / 可中断续跑，约 200 行）。完整理由与「什么情况下才重开这个议题」见实施清单 §11 |
| 并行执行工具 | 单进程；慢的只有 MCP 那两条，而它们最不该并发 |
| 默认用 LLM 当裁判 | 能代码判的一律代码判；LLM 裁判只在 L3 出现 |
| agent 模式自动升级到生产调用 | 对面是同事的服务器和 UAT 库，必须用户显式同意 |
| 改 `qa-system-prompt.md` | ask 模式是基线和回归标尺，动它等于两个模式一起漂（唯一例外：新增 `dataset_query` 那一小节） |
| 后台跑 + 完成后通知 | 在 stdlib 线程服务器里加并发，复杂度远超收益。先做「可打断」（D3），不做「可后台」 |
| **自动学习并记住用户的纠正** | 🔴 这是本项目最不能承受的失败形状：一条错的「事实」被固化，之后每轮都带着它，还没人知道它怎么进来的。用户纠正走 `config/`（有人审），不走自动记忆。B7 的槽位只记**用户明确说过的**，不记推断的 |
| 完整 hook 体系 / 插件市场 | 需要的只是「能拒绝」这一种闸门，不是一套扩展框架 |

---

## 11. 需要你拍板的 4 件事

1. **agent 模式默认能不能碰生产（MCP 日志 / UAT DB）？**
   建议：**默认不能**。用户在 agent 模式里显式勾选「允许查日志 / 数据库」才开；
   或者计划里出现该任务时先停下来问一句。
2. **模式怎么选？** 只给手动开关，还是允许助手在 ask 模式下**建议**「这个问题建议用 agent 模式」？
   建议：手动开关 ＋ 只建议、不自动切。
3. **规划那一次模型调用，用同一个模型，还是允许单独配一个更强的？**
   （BYO-LLM 下有人挂的模型很弱。）建议：同一个 ＋ 降级机制，不引入第二套凭据。
4. **agent 模式的延迟上限用户能接受多少？**
   一次完整调查（含 2 次日志 sweep）大概 60–180 秒。这个数决定 `max_seconds`，
   也决定 P0 的 UI 要不要做「已经查了 X 秒，继续 / 停」。

---

## 12. 一句话对外说法

> 现在的助手像一个**很懂行的接线员**：你问什么，他从正确的柜子里抽出正确的那一份给你。
> agent 模式要把他变成一个**会自己列调查提纲的分析员**：先想清楚这件事要弄明白哪几点，
> 一点一点去查，查不到的明说查不到、并且说清楚是谁能补上，最后给你一份结论 —— **每一句都能翻到出处**。

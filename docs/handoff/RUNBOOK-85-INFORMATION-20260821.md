# Ask 不退化与 Agent 修复：实施前信息梳理

> 对应验收规格：`ask-no-regression-bar-and-agent-fixes-zh.md`  
> 收集日期：2026-08-21（Asia/Shanghai）  
> 本文性质：当前事实、前置条件、契约面与待定决策；不包含实现方案，也没有执行产品测试、评测或真实请求。

## 0. 本次静态梳理的边界

- 已将 `master` fast-forward 到 `c63b74b`；本文上游所涉的文件只有目标验收规格。
- 工作树除已处于多人并行修改状态，尤其 `webapp/agent.py`、`webapp/static/app.js` 为已修改文件，`webapp/agent_loop.py`、`webapp/agent_debug_trace.py`、`webapp/time_policy_presenter.py` 及多个相关测试目前都是未跟踪文件。
- 因此，本文将“当前工作树内容可视为已进入 `master` 的稳定事实”区分对待。后者可直接作为基线；前者在施工前必须先确认是否已被其他 session 接管、提交或替换。
- 未运行 `unittest`、`evals`、Web 服务、Copilot API、MCP 或生产只读查询；没有修改产品代码、active pointer、部署配置或任何本地证据数据。

## 1. 规格要求，已转成实施前必须保留的验收语义

### 1.1 Ask 的“不比旧版差”不是“实现相同”

必须同时守住以下三类可观察行为：

1. **答案质量**：同一评测 case 中，Fast Ask 的全量 case 集合必须是 legacy Ask 全量集合的超集；不能只比较总分、平均分或 case 数。
2. **能力面**：
   - legacy Ask 的模型工具轮上限为 `MAX_TOOL_ITERS`（当前默认 8）；
   - 普通工具失败后，失败结果必须进入下一次模型输入，且模型在可恢复场景仍有可调用工具；
   - terminal `done` 的既有字段须 additive，不能静默移除。
3. **首字延迟**：Fast Ask 的第一枚 token 不应显著晚于 legacy Ask；Agent Mode 不适用此条，但 Agent 的 progress 事件必须能让用户看到工作正在发生。

下列项明确不应作为本轮修复手段：放松 `answer_gate`、把 Fast Ask 变成 Agent、调整 `tool_subset`/context lane 份额、恢复已被证伪的 `runtime_context` 假设。

### 1.2 Fast Ask 的已确认接口

规格要求的关键不是“每轮都开放工具”，而是：

- 前一工具轮全部成功：保持 Fast Ask 现有的收敛行为；
- 前一工具轮存在失败：在有再次校验结果 schema 的情况下，让模型可以根据失败信息改正后重试；
- 达到上限仍失败：最终回答必须明确“尝试了 N 次仍失败”，不可静默退化成泛化常识；
- 回馈必须足以让模型修正：至少能表达字段、期望格式/值、实际获得值（若这些信息可得）。

### 1.3 Agent 的三项独立验收目标

1. `invalid_proposal` 与 `internal_contract_conflict` 是不同后续路径：前者是可由模型修正的 proposal 形状错误；后者是内部/注册表/契约冲突，不能伪装成需要用户补资料。
2. 面板、session terminal 和 debug 不得把“没有记录”解释成“没有发生”；未知布尔/计数必须显示 `—`（或等价 unknown），不能降格为 `否` / `0`。
3. `notice_required=true` 时，最终用户答案必须包含 HKT 默认时区 disclosure。

## 2. 开工前的硬前置条件

| 前置条件 | 当前静态发现 | 实施文档必须明确 |
|---|---|---|
| Copilot API 可用性 | 2026-08-21 的恢复重跑先见 502/DNS；但本次没有重新探测。 | 以前的双路径评测运行证明，而不是沿用一次历史成功。502/网络失败须单列为环境失败，不能计入能力退化。 |
| 双路径评测能力 | 现有 `evals/run.py` 的 in-process 调用为 `agent.answer(question)`，未带 mode，即 legacy 兼容路径；HTTP body 也没有 mode 参数。 | 如何分别调用 legacy 与 Fast Ask、保存运行元数据、比较“全量 case id 集合”。 |
| 不覆盖的基线文件 | `evals/run.py --out` 可指定输出，但默认会覆盖 ignored 的 `evals/last_run.json`，且其中保留答案文本。 | 基线的不可覆盖命名/目录、保留策略、访问边界；不得把模型答案或真实失败写进 tracked Markdown/JSON。 |
| 并行热区 | Ask、Agent、server、前端、debug 和相关测试均存在并行改动。 | 施工前重新核对目标 diff、职责边界和 task ledger；不得 reset/restore/stash 他人改动。 |
| legacy 临时止血 | 浏览器不会把 mode 省略；它总发 `investigation_mode: "ask"`；server 侧即 normalize 为 `fast`。 | 若要临时走 legacy，不能假定“前端不传字段”即可生效；须定义受控的 transport/feature-flag/兼容路径，并验证普通 HTTP 与 SSE 两条入口。 |

## 3. Ask 当前事实

### 3.1 产品入口与 legacy 的实际可达性

当前浏览器初始化 `currentRunMode` 为 `"ask"`，请求中始终携带该值。`server.py` 对请求执行 `investigation_policy.normalize_mode()`，其中 `"ask"`、空值和未知值均归一为 `"fast"`，并把显式值传给 `agent.answer()` / `agent.answer_events()`。

`webapp/agent.py` 只在 Python 调用方将 `investigation_mode=None` 传入时设定 `legacy_compat=True`；此时保留多轮兼容路径。也就是说：

- 浏览器 Ask Mode 当前实际是 Fast Ask，不是“不传 mode 的旧 Ask”；
- legacy 仍在 Python API/旧调用兼容路径存在；
- 现有 HTTP/SSE 产品入口并没有自然暴露 legacy 选择。

这同时解释了两件事：目标规格 §2.7 的临时措施需要额外的入口设计；而当前 `evals/run.py` 的 in-process 默认结果可视为 legacy 模式结果，但不是产品浏览器 Fast Ask 的结果。

### 3.2 Fast Ask 的工具轮与预算约束

当前 Ask 普通工具循环位于 `webapp/agent.py` 的 `_legacy_answer_events()`：

- `tool_schemas` 来自已登记工具，并替换 task-bound 的 `incident_investigate`；静态数量为 17（注册表 18 减 1）。Incident 能力不是丢失，而是由 Ask baseline 中的受控 Contract 提供执行。
- Fast Ask 的 `allow_tools` 条件在第一轮工具执行后变为 `false`；第二次模型调用无 schema。
- `MAX_TOOL_ITERS` 当前默认值为 8，但 Fast policy 的 `FAST_MAX_REPLANS` 默认值为 0。`InvestigationBudget.begin_tool_round()` 在第二个工具轮会因 replans 拒绝。
- 普通工具结果会以 `role: "tool"` 回写；失败判定当前主要是 `result` 为 dict 且含 `error`。
- 完全相同的 `(tool, args)` signature 会被去重。故即使重新开放 schema，完全相同参数的临时性失败目前仍会被跳过；“修正参数后重试”可得新的 signature，但“同参网络重试”不能直接成立。

因此，目标规格的“实回一轮”至少同时涉及 schema 暴露条件、工具轮预算、重复签名语义和最终失败数据四个现有约束；只修改 `allow_tools` 条件不能达到验收。

### 3.3 错误信息的当前形状

普通工具的失败主要是 `{ok: false, error: "...", hint: "..."}` 或类似自由文本。工具注册与 `context_budget.fit_tool_result()` 负责裁剪、截断和 JSON 序列化，但当前没有跨工具统一保证 `field` / `expected` / `actual` 三个字段。

实施规格必须先限定以下边界：

- 哪些错误属于“模型可修复的参数/proposal 错误”，哪些是权限、无数据、对端不可达、预算或内部错误；
- 当错误没有结构化字段信息时，回馈如何表达“具体字段未知”，而不是编造期望值；
- 对连通性失败是否允许同参的有限重试，还是只能让模型修改参数后再调用。

### 3.4 现有评测记录的准确状态

当前 ignored `evals/last_run.json` 的时间戳为 `2026-08-21T02:17:14Z`，摘要为 **32/39 cases 全集、177/186 checks**，`RUNBOOK-85-SEND-BACK-20260821.md` 说明：

- 更早的 retained baseline 只有 **4/4**；
- 2026-08-20 曾遭受 **19 次 Copilot 502/DNS** 影响；
- 2026-08-21 恢复后得到上述 **32/39**，7 条为实际断言失败，未见 502/DNS。

所以“当前只有 4/4”不是最新工作树事实；4/4 是本轮开始前的旧 retained 基线。32/39 也不能直接当作本轮“不退化”基线：它只覆盖默认 legacy 调用、可被默认输出覆盖、没有对应的 Fast Ask 运行，也没有按过滤集合进行比较。

## 4. Agent 当前事实

### 4.1 `invalid_proposal` 与内部契约冲突确实在控制器合流

`webapp/agent_harness.py` 已经区分两类 decision：

- proposal schema/必填字段校验失败 → `invalid_proposal`，并带 rejection code；
- capability、source、operation、scope ceiling 等冲突 → `internal_contract_conflict`。

但 `webapp/agent_loop.py` 的 preflight classification 目前将二者都映射为 `internal_contract_error`；Incident Contract 随后写 gap、`incident_runtime_entry` blocked 和 zero-call summary 后返回。该分支没有把“相同 proposal 错误交回 executor 产生新的工具调用”。

保留样本的计数（N=12）为 `invalid_proposal=8`、`internal_contract_conflict=4`。这不足以证明业主所称的“参数校验错误”都属于 harness 的 `invalid_proposal`；实施前/验收时必须区分：

1. harness decision 的 proposal schema 错；
2. 普通工具的参数/服务端校验错误；
3. 经过 harness 之后的运行级 capability-runner 错。

三者不得靠一个 telemetry 纹理或一个用户提示写为一类。

### 4.2 “一份账”现有数据链及丢失点

目前至少存在三种载体：

1. 流式 runtime events；
2. session terminal 的 `run_steps`；
3. safe debug trace 的 span/view model。

`agent_loop.py` 在 Incident Contract 执行时会生成 `incident_contract_dispatch`、`incident_runtime_entry` 和 `incident_mcp_summary`，事件本身带有 `contract_triggered`、`runtime_entered`、MCP attempted/executed/failed/suppressed count 等字段。

但 Agent 的 run-step whitelist 未看到上述字段；Ask 的 renderer() allowlist 同样没有暴露它们。故 session 持久化的 `run_steps` 无法可靠重建这些状态。当前工作树中的 `agent_debug_trace.py` 有记录合规路径并写到 safe span，但诊断 projection 仍使用 `bool(missing)` 和 `int(missing or 0)`，会把缺失直接降为 `false/0`。

结论：实施规格必须选择一个执行事实写入、可持久化、可跨 terminal/重载视图读取的权威记录，并规定：

- unknown 用显式缺失/null/状态字符串表示，不能用 `false` 或 `0` 代替；
- debug trace 可以复制该记录，不应靠事后猜测重构；
- `run_steps`、terminal 现有字段和新增遥测字段都应是 additive。

### 4.3 paused 被 SSE 伪装为 done

Agent loop 在可 resume 的停止场景已把 `run.status` 设为 `paused`，并保留 `pause_reason`。但其 terminal event 当前仍为 `type: "done"`。`server.py` 与 `app.js` 也只识 `done`、`cancelled`、`budget_exhausted` 当作 terminal。

这正是“内部状态 paused、外层事件 completed/done”的证据。实施规格须定义：

- 新 terminal 类型/状态到 UI 命名与 `run.status` 对应关系；
- server debug finish、session persistence、`run_control.finish()` 到浏览器 terminal switch 的兼容处理；
- 旧消费端遇到新 terminal 的降级行为；
- 不允许把 `paused`、`blocked`、`completed` 重新混同。

### 4.4 用户答案位置的 closure 说明

`who_can_close_it` / `resolution_owner` 已在 gap、closure、Answer Packet 等结构中存在，当前 `answer_gate` 也能将部分 gap 信息写入回答。不过，未见一个已验证的 terminal 加三段契约：

- `why_stopped`
- `who_can_close_it`
- `what_will_happen_after_resolution`（命名待定）

实施规格应明确这些字段的表示形式、来源条件的安全文案，以及内部/外部不可关闭者如何用户化封装。

### 4.5 HKT disclosure 在当前工作树已存在静态实现

`webapp/time_policy_presenter.py` 定义了固定 HKT notice；Fast Ask 在生成最终内容时调用 `append_notice()`，Agent 的 `_synthesize_and_gate()` 也在关键路径按需调用它。当前未跟踪的 `tests/test_time_policy_presenter.py` 已覆盖“`notice_required=true` 时只追加一次并标记 included”。

这里是 §3.3 可能已在并行工作树中完成或部分完成，不能重复施工。仍需在落地前确认：

- 这些文件是否会进入本轮目标分支；
- Ask、Agent、structured/unstructured answer 分支是否共享一个最终文本断言；
- 测试断言的是最终 terminal answer，不是仅断言 state/debug 标记。

## 5. 已有测试与明显缺口

| 项 | 已有静态覆盖 | 仍缺/需重跑才为本轮验收 |
|---|---|---|
| Fast 首轮工具 schema | `tests/test_investigation_modes_and_sessions.py` 明确断言 tool surface 为 `[True, False]`。 | 该测试会与“失败时实回一轮”冲突；应拆成“全成功 `[True, False]`”及“失败 `[True, True, ...]`”。 |
| Fast 多工具调用 | 已验证 Fast 第一轮可含多个普通工具调用。 | 缺少“部分失败”、错误回灌内容、次数上限、同参/改参重试、最终 N 次失败数据。 |
| Agent harness | 已测试 unregistered source/operation → `internal_contract_conflict`。 | 没有 `invalid_proposal` 的 executor 重试测试，也没有三类不同后续动作的分型。 |
| debug diagnosis | 已有静态测试验证 zero-call 因果链。 | 缺少字段不存在时连续 `—` 的断言，以及“runtime 已进入、MCP 对端失败时不得归因 Harness”的端到端串联。 |
| paused run | 已有测试确认 terminal 内 `run.status == "paused"`。 | 缺少 SSE terminal type 不为 `done` 的 server、浏览器和 session 回归测试。 |
| 时区提示 | presenter 单测已存在于当前工作树。 | 需加/确认最终 answer text 的 Ask 与 Agent 全链路断言。 |
| answer quality | 39 个 case 的 runner 与评分已存在。 | 无 mode 参数、无 legacy/Fast 双跑、无过滤集合精确比较、无首 token 计时基线。 |

## 6. 实施规格必须先锁定的决策

以下问题不应由编码过程中临时猜测：

1. **“默认 2 轮”的计数语义**：是总工具轮最多 2，还是首轮之外还能重试 2 轮？最终“试了 N 次”的 N 是 model tool round、实际 dispatch 次数，还是每个 tool 的尝试次数？
2. **可重试范围**：普通工具的参数错误、timeout、connection refused、duplicate、policy denied、incident task-bound Contract 失败分别是否允许 retry；哪些必须 fail closed？
3. **同参数 retry 的去重例外**：若对网络时效失败要重试，如何在不放开无限重试调用的前提下与 `canonical_signature` 去重共存。
4. **legacy 临时入口**：是否需要对用户开放，是否 feature-flag，SSE 与非流式 API 是否一致，如何避免空/未知 mode 被意外解释 legacy。
5. **评测基线存储**：不可覆盖运行 id、运行的 mode/model/config/commit/时间、case id 状态，以及 raw answer 只留 ignored 本地文件的界限。
6. **“一份账”协议载体**：哪份执行记录为 source of truth；debug、session、terminal 如何引用/投影它；unknown 的 API/JSON/UI 具体表示。
7. **terminal 契约**：`blocked`、`paused`、`done/completed`、`cancelled`、`budget_exhausted` 的事件名、run status、是否可 resume、用户可操作者及兼容策略。
8. **`invalid_proposal` 的可修复反馈**：rejection code 如何转成模型可理解而不泄露内部密态的修正信息；第二次调用仍必须重新经过完整 preflight、harness、approval、budget 和 allow-list。

## 7. 安全与兼容性不可违反项

- 不为 retry 放宽 read-only allow-list、target/scope/time gate、approval、MCP budget、caller policy 或 redaction。
- `Incident_investigate` 仍是 task-bound Contract；不能因 Fast/Agent retry 把它变成任意可自由组装参数的普通工具。
- 不把 `internal_contract_conflict` 向用户问问题，也不把 `policy_denied` 描述成“技术可重试”。
- 不放松 `answer_gate` 来掩盖工具/证据失败。
- terminal、run、run_steps、Answer Packet、debug 和 API provenance 只做 additive 演进，并提供全部消费者后向改名/改义。
- 评测输出、真实 repo 映射、原始会话/日志/MCP body 继续留在 ignored 本地路径；实施文档和测试 fixture 使用合成值。

## 8. 后续实施文档应包含的验证矩阵（本次未执行）

1. **环境 gate**：Copilot API 健康；失败时明确标为环境不可用，不发布退化结论。
2. **双路径质量 gate**：同一 resolved case 集合分别跑 legacy 与 Fast，保存不可覆盖的本地运行记录，比较 Fast 全量集合是否包含 legacy 全量集合。
3. **Fast 行为测试**：
   - 首轮全部成功时第二次模型调用无 schema；
   - 首轮失败且仍在上限内时第二次模型调用有 schema，发生第二个 dispatch；
   - 回馈包含可用的字段/期望/实际信息；
   - 上限耗尽时 final answer 说明尝试次数；
   - 预算、去重、MCP/incident gate 不被绕过。
4. **Agent 分景测试**：构造 `invalid_proposal`、`internal_contract_conflict`、映射区三例，验证 retry/telemetry/用户动作三条路径不同。
5. **版本与 terminal 测试**：未知整数 `—`；runtime 已进入后 MCP 失败归因对端；paused/blocked terminal 不再为 `done`；session reload 与 debug view 一致。
6. **时区测试**：`notice_required=true` 时，Ask 与 Agent 的最终 terminal answer 都包含 disclosure。
7. **延迟预算**：记录首 token 开始、首个 provider delta、首个 SSE token 到最终 terminal；只在可用的相同环境/模型/配置下比较 legacy 与 Fast。
8. **回归范围**：先跑相关定向 tests，再按仓库约定扩大至相邻 contract tests；结束时执行 `git diff --check` 并检查目标 diff/工作树，不清理并行改动。

## 9. 施工前建议先核对的文件面

此列表是消费/事实地图，不是修改清单：

- Ask mode、工具循环、结果回写与 terminal：`webapp/agent.py`
- mode normalize、HTTP/SSE terminal 和 session 写入：`webapp/server.py`、`webapp/investigation_policy.py`、`webapp/static/app.js`
- 预算与重试轮定义：`webapp/investigation_policy.py`、`webapp/config.py`
- Agent Contract 分支、runtime event 与 pause：`webapp/agent_harness.py`、`webapp/incident_preflight.py`、`webapp/agent_loop.py`
- run-step/session/debug 投影：`webapp/agent_state.py`、`webapp/session_store.py`、`webapp/agent_debug_trace.py`
- 最终时区提示：`webapp/time_policy_presenter.py`
- 质量比较与本地结果存储：`evals/run.py`、`evals/cases.jsonl`、`evals/last_run.json`
- 最接近的测试入口：`tests/test_investigation_modes_and_sessions.py`、`tests/test_agent_harness.py`、`tests/test_agent_loop.py`、`tests/test_agent_debug_trace.py`、`tests/test_agent_server_api.py`、`tests/test_static_assets.py`、`tests/test_time_policy_presenter.py`

## 10. 可作为实施规格的输入结论

可以立即写实施规格，但它应先把 §6 的八项决定定下来，并明确当前哪些并行未跟踪文件会被纳入。最关键的事实有四个：

1. 当前产品 Ask 已明确是 Fast，而非 legacy；临时回退不是简单省略请求字段。
2. Fast 的第二轮模型受 schema、replan budget、duplicate signature 和错误结构四重限制。
3. Agent 不是没有状态，而是执行事件、持久化 run steps、debug projection 的字段集合不一致，并且缺失值被压成 `false/0`。
4. HKT disclosure 和部分 debug/Agent 代码在当前工作树很可能已经有并行实现，先确认其归属与测试，避免重复覆盖/踩踏。

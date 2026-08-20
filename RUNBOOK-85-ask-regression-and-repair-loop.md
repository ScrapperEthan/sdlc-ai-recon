# RUNBOOK-85 —— Ask 退化 + 一 block 就停：把七件事测出来

> **这一份不改任何代码。**
>
> **背景（2026-08-20，业主口述 + 你们的 handoff 文档）**：
> 升级之后
> ① **新 Ask 和新 Agent 两个都不如外网这版老 Ask**；
> ② 症状是三个，且是同一条链上的三个：**工具调得少了 / 不调了** → **引用和证据不见了** → **答案更空泛、拒答变多**；
> ③ harness **经常参数校验错误**，出错之后**不会自己纠错重跑**，**一 block 就停**；
> ④ 停了之后**不知道为什么停**，要 trace 老半天。
>
> 你们文档里说「`investigation_mode is None` 时保留 Ask/legacy 行为」——
> 这句话对**分流那一行**是成立的，但 §9 清单里 Ask 依赖的**每一个零件**都被改了：
> `tools.py`（工具表）、`context_budget.py`（上下文预算）、`llm.py`（模型入口）、
> `retriever/citations.py`（引用校验）、`static/app.js`（渲染）。
>
> **所以 Ask 不是"从上面"被改的，是"从下面"被改的 —— 在 `agent_loop.py` 里是查不到的。**
> 下面七个探针全部落在**共用层**，因为业主的回答是"两个都变差"，
> 这个事实本身就把"Agent 循环写错了"排除掉了。

---

## 0. 三条规矩（每次都适用）

1. **对不上先回报，不要顺手适配。** 外网这边已经因为"对着想象写"返工过六轮，
   每一轮缺陷都是同一个形状：**我们对你们环境里的某样东西下了断言**
   （名字 → 响应形状 → 值格式 → 现在轮到控制流）。
   这份文档里凡是写成问句的，都**不是**外网的结论。
2. **原样回传，不要总结、不要改写。** 尤其是字段名、枚举值、错误文本模板。
3. 🔴 **不要把原始生产日志、未脱敏 payload、真实 alarm name / app 名贴进回报。**
   下面每个探针要的都是**字段名、结构、分类结果、计数、时间**。
   举例一律用占位符（`<app>` / `<repo>` / `<keyword>`）。

---

## 1. 先说外网这一轮已经能下的判断（这部分不用测，但请核对）

### 1.1 三个症状是一条链，**头在最前面那个**

```
工具调得少 / 调用被拒
      ↓  证据集比老 Ask 小
answer_gate 正确判定「证据不足」
      ↓  没有被接受的 claim
没有 citation 可挂
      ↓
答案空泛 / 拒答变多
```

🔴 **推论：答案空泛不是 answer_gate 的错，gate 是在替上游的"证据饥饿"背锅。**

**所以请不要先去松 gate。** 上一轮（RUNBOOK-83 / commit `857de51`）已经用真机证明过
"闸门是对的"——那次是两个模型各写了一个时区，gate 拦得完全正确。
松了 gate，结果不是"答案变好"，是**从"空泛但诚实"变成"自信但错误"**，
而后者在生产事故里是要命的那一种。

**要修的是上游：让工具能被调到、调失败能自己修、修不了能说人话。**

### 1.2 老 Ask 之所以更强，是因为它有一条自愈循环 —— 就两行代码

外网这版 `webapp/agent.py`：

| 位置 | 代码 | 效果 |
| --- | --- | --- |
| `agent.py:161` | `for iteration in range(config.MAX_TOOL_ITERS)` | 一个问题最多 **8 轮**工具 |
| `agent.py:164` | `llm.chat_stream(messages, tools.TOOLS)` | 每一轮把**工具全集**摆在模型面前 |
| `agent.py:220-223` | `try: dispatch(...) except Exception as e: result = {"error": str(e)}` | **异常不终止运行，异常文本变成工具结果** |
| `agent.py:249-253` | `messages.append({"role":"tool", "content": content})` | 那条错误**回喂给模型**，进入下一轮 |
| `agent.py:178,188` | `answer_text = 模型原文`；`citations.verify(answer_text)` | 校验器**只标注、从不改写答案** |

于是老 Ask 在参数写错时的真实行为是：

```
第 1 轮  模型调 usecase_route(use_case_id="I0141x")   → dispatch 抛 KeyError
        → 工具结果 = {"error": "..."}                 ← 不停，喂回去
第 2 轮  模型看到错误，改成 usecase_route(use_case_id="I0141") → 成功
第 3 轮  拿到数据，给出带引用的结论
```

**同一个错误，老 Ask 花掉一轮预算就自己修好了；新 harness 直接停在第 1 轮。**
这就是业主感觉到的"不如 claude code / opencode / codex"的**准确位置**——
那三个工具的共同点不是模型更强，是**工具报错是一条普通观察，不是运行的终点**。

### 1.3 你们的 `HarnessDecision` 已经把分类做出来了，但**没接到控制流上**

你们文档 §5.1 列了七个值：

```
invalid_proposal            ← 模型把参数写错了     —— 模型自己能修
internal_contract_conflict  ← 我们两个组件对不上   —— 🔴 这是 bug，模型修不了，用户也修不了
user_input_required         ← 真的缺人给的事实     —— 必须停下来问人（缺时区就是这类）
policy_denied               ← 触了 ceiling         —— 永久拒绝，说清楚是"故意限制"
approval_required           ← 等审批
approved / approved_with_narrowing
```

这张表**本身是对的**，而且和外网 `docs/specs/agent-single-source-facts-and-specialist-zh.md` §2
交过去的三分类是对得上的。**问题在于：分类只写进了 decision 对象，没有变成七条不同的下一步。**

业主的原话是"**一 block 就停，然后我还不知道他为什么停**"——
这句话说明至少 `invalid_proposal` 和 `user_input_required` 现在落在同一个桶里，
而这两个的正确处理**完全相反**：前者应该回喂给模型让它改，后者应该停下来问人。

🔴 **关键安全论证（这条是本轮最重要的一句）：**
把 `invalid_proposal` 回喂给模型 **不会扩大任何权限**。
因为它发生在 `action_within_harness_decision()` **之前**，
是对一个 *proposal* 的校验，**此刻还没有任何真实 MCP / 生产动作发生**，
ceiling、approval、budget 三道闸门一条都没动。
模型改完参数**还要再走一遍完整的 preflight + harness**。
"不回喂"换来的不是安全，只是一次白白浪费的往返。

---

## 探针 0 —— 先把"变差"变成数字（最便宜，先跑这个）

外网**已经有**答案质量评测和一条基线，就在你们盒子上：

- `evals/cases.jsonl` —— **39 条** case，含 incident / retrieval / 诚实性三类；
- `evals/run.py` —— runner，支持 `--http` 打一个正在跑的服务；
- `evals/last_run.json` —— **RUNBOOK-66 那次的基线**（这个文件在 `.gitignore` 第 15 行，
  所以它不在 GitHub 上，**只在你们盒子上**，请先备份再跑）。

runner 有一列 `VS LAST`，会逐条打印 `DOWN was PASS` / `up now PASS`。
**这一列就是这次要的东西。**

### 0a 跑这个

```bash
# 1) 先备份基线，别覆盖掉
cp evals/last_run.json evals/baseline-old-ask.json

# 2) 新 Ask，进程内（默认 out 就是 last_run.json，所以 VS LAST 会拿老基线来比）
python -m evals.run
```

### 0b 回报

1. **表格原样**（那张表是特意做窄的，手机拍照就行）；
2. 所有 `DOWN was PASS` 的 case id **全列**；
3. 这些红 case 的实际回答（runner 已经存进 `last_run.json` 的 `answer` 字段，**不用重跑**）；
4. 如果 runner 直接报错跑不起来 —— **这本身就是发现 #1**，见探针 6b，请把 traceback 原样贴回。

### 0c 期望 vs 回报

- 外网基线是 **19/20 绿**（RUNBOOK-66 之后修正过断言，`asserts_phrase` 那条 bug 已修）；
- 如果新 Ask 掉到明显低于这个 → "变差"这件事**被钉死了**，后面六个探针是在找原因；
- 如果新 Ask **没掉** → 那么退化在**评测覆盖不到的地方**（多半是探针 5 的体感/流式，
  或者是 incident 这条 lane 的真机行为），后面探针的优先级要重排。

### 0d 顺便：能不能把同一批 case 打到 Agent 模式？

**不要现在改 runner。** 只回答一句：
**`/api/chat` 有没有一个字段能把同一个问题送进 Agent 模式**（有 → 字段名叫什么；没有 → 说没有）。
有的话下一轮就能跑出"老 Ask / 新 Ask / 新 Agent"三列，那是最有说服力的一张表。

---

## 探针 1 —— 🔴 最重要：一次失败的工具调用，之后发生了什么

这是本轮的核心探针。请挑**一次真实发生过的"参数校验错误"**（业主说"经常"，所以应该好找），
把这条链**逐格**回报。

### 1a 要什么

| # | 问题 | 期望的回报形式 |
| --- | --- | --- |
| 1 | 这次 harness 返回的 decision 是七个里的哪一个？ | 枚举值原样 |
| 2 | 🔴 **这个 decision 之后，模型总共又被调用了几次？** | 一个整数。**`0` 就是病本身** |
| 3 | 模型有没有收到一条描述这次失败的 `role: "tool"` 消息？ | 有 / 没有 |
| 4 | 如果有，那条消息的**文本模板**长什么样？ | 原样贴模板，真值用占位符 |
| 5 | 那条消息里有没有说清 **哪个字段错了 / 期望什么格式**？ | 有 / 没有 |
| 6 | 老 Ask 里 `agent.py:222` 那行 `result = {"error": str(e)}` 的**等价物**，在新路径上是哪一行？ | 文件:行号；**如果没有，就说"没有"** |
| 7 | 这次 run 的 terminal event 的 `type` 是什么？ | `done` / `cancelled` / `budget_exhausted` / 其它（写出来） |
| 8 | 用户在界面上**看到的那句话**是什么？ | 原样贴（这句话决定了"不知道为什么停"是不是真的） |

### 1b 再要一个计数

最近 **N 次**（N 由你们定，20 次左右就够）Agent run 里，**七个 decision 值各出现了几次**：

```
invalid_proposal            <n>
internal_contract_conflict  <n>     ← 🔴 这个 > 0 就是我们自己的接线 bug，不是 gap
user_input_required         <n>
policy_denied               <n>
approval_required           <n>
approved                    <n>
approved_with_narrowing     <n>
```

### 1c 期望看到

- 如果 **1a-2 = 0** 且 **1a-6 = "没有"** →
  **自愈循环确实丢了**，这是"不如 claude code"的第一原因，
  修法在下面 §"建议修复顺序" F1，**很小，且零权限风险**（论证见 §1.3）。
- 如果 **`invalid_proposal` 的计数明显最高** →
  同时说明第二件事：**模型在写它不该写的参数**。
  这条外网上一轮已经交过 spec（`agent-single-source-facts-and-specialist-zh.md` §1.3
  「不许模型生成的参数封闭清单」）——**如果那条还没落地，它和 F1 是同一个病的两头**：
  一头是不让模型写错，一头是写错了能自己改。**两头都要，先做 F1（更便宜）。**

---

## 探针 2 —— 模型一轮里到底看得见几个工具

业主症状里的"**工具调得少了 / 不调了**"，最可能的解释不是模型变懒，是**它手上没有那个工具**。

### 2a 回报（一行一项）

| # | 问题 | 回报 |
| --- | --- | --- |
| 1 | `len(tools.TOOLS)` 当前是多少？ | 整数 |
| 2 | 🔴 **新 Ask 一轮里实际传给模型的 schema 有几个？** | 整数（**老 Ask 是全集**，见 `agent.py:164`） |
| 3 | 🔴 **Ask 路径走不走 `tool_subset.select()`？** | 走 / 不走 + 那一行的 文件:行号 |
| 4 | Agent 每个 task 的 `selected_tools` 通常几个？ | 整数 |
| 5 | `AGENT_MAX_TOOLS_PER_TASK` 当前值？ | 整数（外网记得是 4，请确认） |
| 6 | 最近若干次 run 里，`excluded` 的原因**各出现几次**？ | 原因枚举 + 计数 |
| 7 | 🔴 **`selected_tools` 是谁定的、什么时候定的？** | 见下 |

第 7 项展开——这是本探针真正要问的：

- (a) **planner 模型在出 Plan 的时候定的**（也就是**在看到任何证据之前**）；还是
- (b) **执行到这个 task 时，根据上一步的实际结果重新选的**？

如果是 (a)，那么这就是上一轮那条结论的第二个实例：
**「决策要贴着数据」** —— 上一轮是 investigator 把 200 行压成 5 行、
决策依据正好在被砍掉的部分；这一轮是**工具白名单在证据出现之前就冻结了**。
需要哪个工具，**只有看过上一个工具的输出才知道**。

### 2b 还有一件事必须单独确认

你们文档 §4.2 特别规则写了：
「`incident_investigate` 从 generic Executor schema 中移除，标记为 `contract_only`」。

请回答：**新 Ask 模式下，`incident_investigate` 还在不在模型可见的工具表里？**

- 在 → 它被调用时走哪条路？（还是 `dispatch_events`，还是也要过 preflight/harness？）
- 不在 → **那新 Ask 就完全丧失了查生产日志的能力**。
  这和 RUNBOOK-83 记的背景直接冲突：那次事故里，
  **恰恰是 ask 模式给出了处置结论、agent 模式答成了"已确认事实：无"**。
  如果现在连 ask 也没这个工具了，那条业主唯一夸过的路径就被关掉了。

---

## 探针 3 —— "停了，但不知道为什么停"

业主原话："**一 block 就停，然后我还不知道他为什么停，trace 老半天。**"

**这不是 trace 不够的问题，是"停止原因"根本没有走到用户面前。**
你们文档 §7 写的终结事件只有三个：

```
done | cancelled | budget_exhausted
```

**没有 `blocked`。** 那么一次被 harness 拦下的 run，它最后到底以哪个身份结束？

### 3a 回报

1. 一次 block 的 run，前端收到的 terminal event **原样**（字段名 + 值，值可占位符化）；
2. 用户界面上**看到的那句话**，原样；
3. 从"点发送"到"知道为什么停"，用户要**点几下**、要不要开 debug 面板？（一个数字）
4. `who_can_close` 这个字段**存在吗**？
   （外网 spec §2 的验收要求是：缺时区 / 内部不一致 / 超范围 三种情况，
   `cause` 和 `who_can_close` **三条各不相同**，分别指向 **用户 / 我们 / 配置拥有者**）
5. `internal_contract_conflict` 这一类，现在**会不会被当成 gap 显示给用户去补**？
   🔴 如果会，那是错的 —— **用户补不了我们自己的接线 bug。**

### 3b 期望

一个正确的停止，用户**在答案位置**就应该看到三句话，不用点任何东西：

```
为什么停在这里：  <一句人话>
谁能解开：        用户 / 我们（这是我们的缺陷）/ 配置拥有者
解开之后会怎样：  <继续做什么>
```

---

## 探针 4 —— 上下文预算是不是被新 lane 稀释了

老 Ask 的预算是**一个** budget 切成四条 lane（`context_budget.py` 开头的注释画了这张图）：
`system` / `history` / `tools` / `subagent`，其中 **compaction lane 是预留但故意不填的**。

你们新增了 `context_pack.py`、`conversation_compaction.py`、`turn_facts.py`、
`agent_context_coordinator.py`、`specialist_context.py`，而且改了 `context_budget.py`。

**如果 tools lane 的份额被新 lane 挤小了，那么每个工具结果都被砍得更狠 —— 证据变少，没有任何报错。**

### 4a 回报

1. 新的 lane 名单 + 各自份额（老的在 `config.CONTEXT_LANE_SHARES`）；
2. 一次典型 Ask turn 的 `Budget.report()`（或你们的等价物）**原样**；
3. tools lane 这一轮实际花了多少 / 上限多少；
4. `shrink_tool_result` 的 `kept/total`（**这个字段外网这版早就有**，不用新加）；
5. 🔴 **`conversation_compaction` 现在是不是真的在跑？**
   老版本是"lane 预留、不填"，因为多一次模型调用就多一种出错方式。
   如果它现在在跑，那 Ask 的历史就不再是 `budget.fit_history()` 那套
   （保留开场问题 + 明确告诉模型丢了几轮），而是一份**模型写的摘要** ——
   摘要丢东西是不报错的。

---

## 探针 5 —— 模型角色（必须先排除，否则上面全部作废）

你们新增了 `model_roles.py`，改了 `llm.py` 和三个 provider 文件。

**如果新 Ask 用的根本不是老 Ask 那个模型，那"退化"就跟架构一点关系都没有。**
这条最便宜，但**必须先排除**。

### 5a 回报

| | 模型 id | temperature | max_tokens | 一次问答的模型调用次数 |
| --- | --- | --- | --- | --- |
| 老 Ask（外网这版） | | | | |
| 新 Ask | | | | |
| Agent - planner | | | | |
| Agent - executor | | | | |
| Agent - synthesizer | | | | |

### 5b 期望

- 老 Ask 和新 Ask **同一个模型、同一组参数** → 探针 0 的分差是架构造成的，继续往下查；
- 🔴 **不同** → 先把新 Ask 切回老模型再跑一遍探针 0，
  否则后面所有对比都在比两件不同的东西。

---

## 探针 6 —— 两个"静默丢东西"的缝

### 6a 工具结果被归一化压成了什么

你们文档 §4.3 写了 `capability_runner` 1106-1147 是「Ask / Agent 共用的 dispatch wrapper」，
59-91 是「bounded/sanitized payload 与摘要」。

老 Ask 放进模型的是**结构化收缩后的原始结果**
（`shrink_tool_result`，按 JSON 结构裁，不是字节切片，且会记 `kept/total`）。

请拿**一个普通工具**（`impact` 或 `usecase_route`，**不要用生产工具**）对比：

| | 进模型的 tool message 字符数 | 剩下哪些字段（只要字段名） |
| --- | --- | --- |
| 老路径 `shrink_tool_result` | | |
| 新路径 归一化后 | | |

🔴 **这个形状上一轮已经出现过一次**：investigator 把 200 行压成 5 行，
而**决策依据正好在被砍掉的部分**。请确认它没有扩散到普通工具上。

### 6b HTTP 层的静默丢弃

你们文档 §6.4 说 `server.py` 的 `supported_kwargs()` 存在是为了
"不让新字段导致旧 adapter 抛 `TypeError`"。

但你们**同一份文档 §6 结尾**又写了：
「**参数缺失应该返回结构化 unavailable/blocked，不应默默扩大生产 scope**」。

🔴 **这两句是互相矛盾的** —— `supported_kwargs()` 做的正是"默默丢掉"。
按上一轮定下的规矩，**内部不一致 = bug，不是 gap**，所以请回答：

1. 当前 `supported_kwargs()` 实际过滤掉了哪些字段？（字段名清单）
2. 🔴 **`owner` 在不在被过滤掉的那批里？**
   （老 Ask 的 `owner` 一路穿到子代理，是 owner-scoped 原始证据的隔离依据，
   `agent.py:130` 的 docstring 写了这件事。丢了 `owner` = 保留的原始日志谁都点不开，
   看起来就是"引用和证据不见了"。）
3. `history` 呢？丢了 history，多轮追问就退化成单轮。
4. 一个字段被丢掉的时候，**有没有任何地方留痕**？

---

## 探针 7 —— 是"内容变差"还是"看起来变差"

`static/app.js` 被改了（你们文档 §7 说 2849-4162 是 Agent state / approval UI，
4481 起是流事件消费）。老 Ask 是**逐字流式**的（`agent.py:167` 直接 `yield token`）。

**如果前端现在要等 terminal event 拿到 `answer_packet` 才渲染，
那么内容一个字没变，体感也会差一大截 —— 而这要修的是前端，不是 Agent 架构。**

### 7a 回报（同一个问题，各测一次）

| | 首 token 到达（秒） | 总耗时（秒） |
| --- | --- | --- |
| 老 Ask | | |
| 新 Ask | | |
| 新 Agent | | |

外加一句：**`token` 事件现在还在发吗？前端还在逐个渲染吗？**

---

## 回报格式

每个探针一段，**按编号**，回答不了的写"没测"，不要留空。
一段里先写数字/字段名，再写一句话说明。**不要重写这份文档的结构。**

---

## 建议的修复顺序（等你们数字回来定稿，先看方向对不对）

| | 做什么 | 触发条件 | 为什么排这个位置 |
| --- | --- | --- | --- |
| **F1** | 🔴 **工具错误回喂模型 = 自愈循环** | 探针 1a-2 回报 `0` | 最小、最高杠杆、**零权限风险**（论证见 §1.3）。这一条就是"不如 claude code"的主因 |
| **F2** | 七个 decision 接成七条不同的下一步 | 探针 1b `invalid_proposal` 计数高 | 分类你们已经做完了，**只差接线** |
| **F3** | terminal event 加 `blocked` + 三段渲染（为什么停 / 谁能解开 / 解开会怎样） | 探针 3 | 直接消掉"不知道为什么停、trace 老半天" |
| **F4** | 工具预选改成"执行时选"，或**至少 Ask 不裁** | 探针 2 回报 (a) | "决策要贴着数据"的第二个实例 |
| **F5** | `ask_user_question`（`pause` → 下一轮 `resume`） | F2 落地后 | **这条已经在 RUNBOOK-84 保留下来了**，spec 在 `docs/specs/harness-borrowings-implement-zh.md` §B。它正好是 F2 里 `user_input_required` 那一类的正规出口 |
| **不做** | ❌ **不要松 answer_gate** | —— | gate 是对的（`857de51` 真机证明过）。松了 = 把"空泛但诚实"换成"自信但错误" |

**F1 单独说一句：如果探针 1 回来确实是 `0` 次重试 + 没有等价的错误回喂，
这一条不用等其它六个探针，可以直接开工。**
它的形状在外网代码里就是 `webapp/agent.py:220-223` 加 `249-253` ——
`except` 把异常变成 `{"error": str(e)}`，当作普通工具结果进下一轮消息。
差别只在于：你们的错误来自 harness 的 decision 对象，
所以要把 decision **翻译成一句给模型看的、说明哪个字段错了、期望什么格式**的文本，
而不是把 decision 对象本身丢给模型。

---

## 附：这一轮不用再测的事

- **不是权限问题**（上一轮已排除：`auto_allowed_by_policy`、`access all_readonly`）；
- **不是超时**（112s / 360s）；
- **不是生产预算耗尽**（`incident 0/4`）；
- **闸门本身是对的** —— `857de51` 那次两个模型各写了一个时区，gate 拦得完全正确；
- **有序的证据词表已经存在**，在 `retriever/channel_evidence.py`，不要再造第二套 `evidence_grade`。

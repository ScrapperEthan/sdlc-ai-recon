# Ask 自修复与一份账 —— RUNBOOK-85 回报之后的四条修复

> 上游：[`RUNBOOK-85-ask-regression-and-repair-loop.md`](../../RUNBOOK-85-ask-regression-and-repair-loop.md)
> ← 内网 `RUNBOOK-85-SEND-BACK-20260821`（2026-08-20 至 08-21，代码改动：无）
>
> **这份写的是"要什么、不能违反什么、怎么验收"，不写"怎么实现"。**
> 代码在内网，文件怎么分、函数怎么命名、状态放哪，**由内网 codex 判断**。
>
> **本轮的性质变了**：前三轮是"外网猜、内网测"，这一轮**三条缺陷已经在源码层面被内网自己确认**，
> 所以这份不是又一个 recon，是**可以直接开工的修复规格**。

---

## 0. 先说外网这一轮错了六条

回报把外网六个假设直接证伪了。**列在最前面，是为了防止有人按已经作废的假设去改代码。**

| # | 外网上一轮的假设 | 真机 / 源码 | 结论 |
| --- | --- | --- | --- |
| 1 | `runtime_context` 没传到 `mdc_knowledge`（RUNBOOK-85 的 **F0**） | 源码明确传入 `runtime_context={"knowledge_pin": ...}`（`webapp/agent.py:1019-1028`） | ❌ **F0 作废，不要做** |
| 2 | Ask 的工具可见集被裁窄了 | Ask 看得见 **17/18**，且**不走** `tool_subset.select()` | ❌ 不是病因 |
| 3 | `supported_kwargs()` 静默丢掉 `owner` / `history` | 两个 callable 的 11 个 kwargs **`filtered=[]`**，一个没丢 | ❌ 不是病因 |
| 4 | 模型角色被换了 | 五条路径**全是** `gpt-5.6-terra` / `temperature=0` / `max_tokens=4096` | ❌ 排除 |
| 5 | Ask 的工具结果被新归一化压小了 | Ask 仍走 `context.fit_tool_result()` → `shrink_tool_result()`（`webapp/agent.py:1133-1144`），**和老 Ask 一样** | ❌ 不是病因 |
| 6 | 盒子上有一条 19/20 的评测基线 | 实际只有 **4/4** —— 35 条显示 `new`，可比的只有 4 条 | ❌ **基线要重建** |

**外网这条毛病的形状没变**：六轮下来每一次缺陷都是**外网对内网环境下了断言**
（名字 → 响应形状 → 值格式 → 控制流 → 这次是**参数传递**）。
**这份文档里凡是写成"要什么/验收"的都是规格；凡是提到内网具体行号的，都以真机为准。**

**顺带一条判断是对的、要记住**：19/21 个失败是本地 copilot-api 的 **HTTP 502（upstream DNS/连接失败）**，
0.0–0.1 秒就红了 —— 那是模型压根没被调到。
内网把它判为"环境可用性发现，不能当作模型或 Answer Gate 退化"，**这个判断完全正确**，
**不要因为这 19 条去改任何 prompt 或闸门。**

---

## 1. 这一轮真正确认的三条缺陷

三条都在源码层面被内网自己指出来，**它们合起来正好解释业主的三个症状**。

### 1.1 Fast Ask 在第一次 tool round 之后撤掉工具

> 内网原话：「Ask 当前普通工具路径会把失败 `result` 作为 `role:"tool"` 加回 messages
> （`webapp/agent.py:1133-1144`）；但 **Fast Ask 明确只给首个 tool round 暴露 schema，
> 下一轮不再允许工具**（`webapp/agent.py:821-836`）。所以失败文本虽然会被模型看见，
> **模型仍不能在该 Fast Ask turn 内重调工具自修**。」

🔴 **这比外网猜的"没回喂"更糟：模型看得见自己失败了，手上却没有工具，还被要求给出答案。**

截图一那个答案的形状——「抱歉，当前文档查询参数不兼容」＋ 一段通用常识 ＋「未验证」＋
`no citations to verify`——**是这个约束下唯一可能的产物**。它不是模型偷懒，是被逼的。

### 1.2 `invalid_proposal` 和 `internal_contract_conflict` 被并成了一个桶

> 内网原话：「当前源码确认 `invalid_proposal` 和 `internal_contract_conflict` **都映射到**
> `internal_contract_error`（`webapp/agent_loop.py:495-507`）；Incident Contract 在这种 decision 下
> **直接记录 gap/blocked runtime，而不把可修的工具错误回喂给 executor**
> （`webapp/agent_loop.py:671-836`）。**新 Agent 路径没有找到老 Ask
> `result={"error": ...}` → `role:"tool"` 的同等纠错回路。**」

这两个东西的性质**正好相反**：

| decision | 是什么 | 谁能修 | 正确的下一步 |
| --- | --- | --- | --- |
| `invalid_proposal` | 模型把参数写错了 | **模型自己** | 回喂 → 重试 |
| `internal_contract_conflict` | 我们两个组件对不上 | **模型和用户都不能** | 标成我们的缺陷、落 telemetry、**别去问用户** |

**好消息：分类你们已经做对了一半。** 那唯一一条 `internal_contract_conflict` 样本带的是
`user_action_required=false`、`resumable=true`、`primary_cause=internal_contract_conflict` ——
**判得很准**。缺的只是把 `invalid_proposal` 从这个桶里拿出来。

### 1.3 三份记录不是同一份账 —— 而且面板把"不知道"渲染成了"没有发生"

> 内网原话：「同一 assistant turn 关联的 safe debug trace **没有** incident-contract/runtime/MCP 类型 span；
> 它只保留了 execution/model/terminal 类 span。与此同时，run-step serializer **又没有保留**
> `contract_triggered`、`runtime_entered` 和 MCP counter 的具体值。
> **结论：两块 UI 确实没有从同一份完整版本读取；现有保留数据也不足以还原右侧三个布尔值的来源。**」

而 session 侧的记录是**清楚的**：`run_steps` 里有 `incident_contract_dispatch`、
`incident_runtime_entry`、`incident_mcp_summary` ＋ 5 条 `subagent_step`，
**harness decision = `approved`**。

> 内网原话：「session-side 记录支持"**Contract 已触发且实际发生过 Incident/MCP 路径**"，
> 不是"未触发 Contract"。」

🔴🔴 **所以截图二左边是对的，右边是错的。屏幕上那句「Harness 未批准进入 Incident runtime」是假的。**
真实情况是 harness **批准了**（`approved`），运行进去了，然后 **Portal MCP 连接被拒**。

**这个 bug 的准确形状不是"数字算错了"，是"把『不知道』渲染成了『没有发生』"** ——
面板拿不到值，于是显示 `否 / 0`，而不是显示"这一段没有记录"。

---

## 2. 三个症状 → 三条缺陷的对应

```
业主症状                        对应缺陷
────────────────────────────────────────────────
工具调得少 / 不调了      ←──  1.1（第二轮起没有工具）
                              1.2（可修的错误不回喂）
引用和证据不见了         ←──  同上：没有工具 → 没有证据 → 没有可挂的引用
答案更空泛 / 拒答变多    ←──  同上的下游结果
一 block 就停            ←──  1.2
不知道为什么停、trace 老半天 ←──  1.3
```

🔴 **再强调一次：answer_gate 一次都没有做错。**
探针 0 里 `honesty-no-timezone-must-not-describe-logs` **PASS 9/9**，
截图一里它准确地标了"未验证 / no citations to verify"。
**松闸门 = 把"空泛但诚实"换成"自信但错误"。这条不做。**

---

## 3. S0 —— 止血：UI 上那个 "Ask Mode" 走的是哪条路？

**一个问题，可能换来一个开关。**

内网回报：`/api/chat` 支持 `investigation_mode`（`fast` / `deep`）。
你们文档 §3.1 写的是：`investigation_mode` **为 `None`** 时保留 Ask/legacy 行为。
而截图一的 trace 写的是「Built the **bounded** Ask baseline」。

**请回答两句话：**

1. 前端那个 "Ask Mode" 按钮，发出去的 `investigation_mode` 是 **`fast`** 还是 **不传**？
2. 如果是 `fast` —— **legacy 那条路（不传）现在还可达吗？跑得起来吗？**

- **可达** → 那么在 S1 落地之前，**把 UI 的 Ask 切回 legacy 就是止血**，零代码风险。
- **不可达** → 直接说，止血方案就只剩 S1，不用再找开关。

**这一条不阻塞 S1 开工**，但它可能让业主今天就能用回一个正常的 Ask。

---

## 4. S1 —— 失败的工具调用要"买回一轮"

### 问题

Fast Ask 的判据是**第几轮**（只有第 1 轮有 schema）。
所以"这一轮成功了"和"这一轮失败了"得到**完全一样的待遇**：下一轮都没有工具。

### 不改的后果（重点）

**任何一次工具失败 = 整轮报废。** 而工具失败在这套系统里是**常态**，
不是异常：参数格式对不上、对端不可达（Portal `connection_refused`）、数据集没绑上……
每一次都会退化成"道歉 + 通用常识 + 未验证"。

用户看到的是**"它突然什么都不知道了"**——而系统其实只是被夺走了第二次机会。

### 改完的效果

第一次参数写错 → 模型看到"哪个字段错了、期望什么" → 第二轮改对 → 拿到数据 → 带引用作答。
用户什么都不用做，多花一两秒。**这就是老 Ask 的行为**（`webapp/agent.py:220-223` ＋ `249-253`）。

### 怎么改（三条，都很小）

#### 4.1 判据从"第几轮"改成"上一轮有没有失败的工具调用"

- 上一轮**全部成功** → **维持现状**，撤掉 schema。**"fast"的本意不动。**
- 上一轮**有失败** → 下一轮**继续带 schema**。

🔴 **这是一个判据的改动，不是一个架构的改动。** 不要把 Fast Ask 改成 Agent。

#### 4.2 给"买回"一个明确的上限

一个配置项（名字你们定），**默认 2 轮**。
用完还失败 → 正常收尾，**并且在答案里写明"试了 N 次仍失败"**，
而不是静默地退回通用常识。

#### 4.3 🔴 回喂的文本必须说清「哪个字段 / 期望什么 / 实际拿到什么」

不是把异常对象 `str()` 一下。
**模型改不对参数最常见的原因是错误信息没说期望值** ——
这正是外网第四轮踩过的那个坑（`2026-07-30 03:15 HKT` 原样传被拒，
真机要 `alert_time` ＋ `timezone` 分开传；错误信息如果只说"格式错误"，模型永远猜不到）。

### 验收

1. 端到端：构造一个必然参数失败的工具调用 → **第二轮模型带着 schema 重试 → 成功**；
2. 断言：上一轮**全部成功**时，schema 仍按现状撤掉（**证明没有把 fast 改成 deep**）；
3. 断言：买回轮数用完后，最终答案里出现"试了 N 次仍失败"字样；
4. 断言：回喂文本里**包含字段名和期望格式**，不是裸异常字符串。

---

## 5. S2 —— 把 `invalid_proposal` 从内部冲突的桶里拿出来

### 问题

见 §1.2：两个性质相反的 decision 映射到同一个 `internal_contract_error`，
然后都走"记 gap / blocked"，都不回喂。

### 不改的后果（重点）

- 模型自己能修的错误，被当成系统缺陷记下来 → **白白丢掉一次本来能成功的调查**；
- 反过来，真正的系统缺陷（`internal_contract_conflict`）如果被当成"可重试"，
  就会变成**重试同一个必然失败的东西**，把 90 秒耗光。

**两个方向都是错的，而现在它们共用一条路。**

### 改完的效果

三种拒绝，三条不同的下一步：

| decision | 下一步 | 用户看到 |
| --- | --- | --- |
| `invalid_proposal` | 翻译成"哪个字段错、期望什么"→ 回喂 executor → 重试（受 S1 的上限约束） | 通常什么都看不到，因为它自己修好了 |
| `internal_contract_conflict` | **标成我们的缺陷**、落 telemetry 到**具体字段**、**不问用户** | "这是我们这边的接线缺陷，已记录" |
| `user_input_required` | 停下来问人（S5 的正规出口） | 一句明确的问题 |
| `policy_denied` | 说清是**配置里的故意限制**，不是"查不到" | "这是范围限制，不是空结果" |

### 🔴 安全论证（这是唯一可能被挡下来的地方，先写在这里）

**把 `invalid_proposal` 回喂给模型，不扩大任何权限。**

它发生在 `action_within_harness_decision()` **之前** ——
是对一个 *proposal* 的校验，**此刻没有任何真实 MCP / 生产动作发生**，
ceiling、approval、budget 三道闸门**一条都没有动**。
模型改完参数**还要再走一遍完整的 preflight ＋ harness**。

**"不回喂"换来的不是安全，只是一次白白浪费的往返。**

### ⚠️ 一个必须顺手查的洞

你们的样本里 **`invalid_proposal = 0`**，但业主说"**参数校验错误经常出现**"。
两者不可能都对。两种可能：

- (a) 它真的很少，业主看到的"参数校验错误"是**别的东西**
  （比如截图一那句「文档查询参数不兼容」——那是**工具层**的错，不是 harness 的 decision）；
- (b) 它发生了，但**没被记成这个值** —— 在到达 harness 之前就失败了。

🔴 **如果是 (b)，那分类表本身有个洞：有一类参数错误根本没进入这七个枚举。**
请在 S2 里顺手确认是 (a) 还是 (b)，**这决定了 S2 要不要多修一处**。

### 验收

1. 三个用例（参数写错 / 内部不一致 / 缺时区）→ **三条不同的下一步**；
2. `invalid_proposal` 那条**产生第二次工具调用**；
3. `internal_contract_conflict` 那条**不出现在给用户的提问里**，
   但 telemetry 里能定位到**具体是哪个字段**对不上；
4. `policy_denied` 的措辞里出现"限制"，**不出现"查不到 / 无数据"**。

---

## 6. S3 —— 一份账，以及「不知道」不许渲染成「没有发生」

### 问题

见 §1.3：safe debug trace / session `run_steps` / state summary 三份记录，
面板从其中一份**重建**出了另一份的结论，重建错了。

### 不改的后果（重点）

**每次出问题都被指向错误的那扇门。** 截图二里，屏幕让业主去查权限和审批，
而真相是 **Portal 服务连接被拒绝** —— 一个网络/对端问题。

**更糟的是第二层**：一个对什么情况都说同一句「Harness 未批准」的面板，
**等于没有面板**。真的哪天是 harness 拒绝，你也分不出来了。

### 改完的效果

停下来的时候，用户在**答案位置**就看到三句话，不用点任何东西、不用开 debug：

```
为什么停在这里：  Portal 服务连接被拒绝，重试 2 次后放弃
谁能解开：        不是你，也不是我们的策略 —— 是 Portal 那边或网络
解开之后会怎样：  能补上短信投递记录，把这次的结论闭合
```

### 怎么改（五条）

1. 🔴 **面板只读一份账，而且必须是执行时写的那份**，不是事后重建的。
   哪一份由你们定。
2. 🔴🔴 **「没有记录」≠「否 / 0」。**
   布尔值和计数器取不到时显示 `—`。
   你们右侧那格 `Zero Call Reason` **已经会显示 `—` 了** —— 把这个做法**推广到全部字段**。
   **这一条是本节最重要的：当前的 bug 不是数字算错，是缺失被渲染成了否定。**
3. **run-step serializer 要保 `contract_triggered` / `runtime_entered` / MCP counter 的具体值**
   （这一条是你们自己指出来它没保的）。
4. **加 terminal `blocked`。** `webapp/server.py:1521-1525` 现在只认
   `done | cancelled | budget_exhausted`，于是一个 `run.status=paused` 的 run
   **外层发的是 `done`**。
   🔴 这条 RUNBOOK-84 §B.4-2 已经定过了：「**pause 理由必须显式，绝不许伪装成 completed**」——
   现在它有了第二个实例。**一个"在等"的 run 和一个"做完了"的 run，在记录里必须一眼分开。**
5. **三段渲染**：`who_can_close_it` / `resolution_owner` **已经在 gap/closure 记录里了**，
   只差把它提到 terminal 顶层、并在**答案位置**渲染出来。

### 验收

1. 一条断言：**取不到值的布尔/计数器渲染成 `—`，不是 `否` / `0`**；
2. 一条端到端：一次 MCP 连接失败的 run，面板显示的原因指向**对端**，
   **不出现"Harness 未批准"**；
3. 一条断言：`run.status=paused` 的 run，terminal event **不是** `done`；
4. 一条断言：三类停止的 `who_can_close` **三条各不相同**（用户 / 我们 / 配置拥有者）。

---

## 7. S4 —— 时区 disclosure 只差一条断言

**这一条你们做对了，而且比外网上一轮要求的更完整**：
那次 run 带了 `timezone_source=policy_default_hkt`、`notice_required=true`，**还有 rule id**。
出身、来源、规则三样齐了。

**只差最后一格**：`notice_required=true` 的时候，
🔴 **答案正文里必须真的出现那句 disclosure** ——
"这个时区是按默认策略补的，不是从告警原文里读到的"。

**为什么值得单独立一条**：
一个"记了但没显示"的 notice，和没记是一样的——**用户看不到**。
这和外网上一轮抓的 **"0% ＝ 双厂商备用，不是关掉了"** 是同一个形状：
系统内部知道，用户不知道，于是用户按错的理解做决定。

### 验收

一条断言：`notice_required=true` → **最终 answer text 中包含时区来源说明**。
（探针 0 的 `honesty-no-timezone-must-not-describe-logs` 本次 PASS 9/9，
说明"缺时区不许往下走"这条已经守住了；这条补的是"补了默认要说出来"。）

---

## 8. 不做什么

- ❌ **不要松 `answer_gate`。** 它一次都没做错（§2）。
- ❌ **不要因为那 19 条 502 去改任何 prompt 或闸门。** 内网这个判断是对的。
- ❌ **不要把 Fast Ask 改成 Agent。** S1 的判据是"上一轮有没有失败"，不是取消 fast。
- ❌ **不要现在动 `tool_subset` 或 lane 份额。** 见 §9。
- ❌ **不做 RUNBOOK-85 的 F0**（`runtime_context` 那条已被源码证伪，见 §0-1）。

---

## 9. 已排除、本轮不用再查的

| 项 | 数据 | 结论 |
| --- | --- | --- |
| 模型角色 | 五条路径全是 `gpt-5.6-terra` / `temp=0` / `4096` | 排除 |
| Ask 工具可见集 | **17/18**，不走 `tool_subset.select()` | 排除 |
| `supported_kwargs` | `filtered=[]`，`owner` retained，`history` 走位置参数 | 排除 |
| Ask 工具结果压缩 | 仍走 `shrink_tool_result()` | 排除 |
| 预算稀释 | tools lane **50% → 45%**（新增 `plan` 10%，history 25%→20%）；绝对值 **51485 tokens** | **是真的，但不是病因** |

关于最后一条多说一句：**份额确实被切了一刀（相对少了约 10%），但 51485 tokens 不是饥饿。**
**先不动。** 等 S1 落地、工具真的能被调用起来之后，如果**那时**还有截断证据，再回来看。
现在动它，只会在真正的病因还在的时候制造一个新的变量。

---

## 10. 顺序

| | 做什么 | 依赖 | 大小 |
| --- | --- | --- | --- |
| **S0** | 问清 UI 的 Ask 走 `fast` 还是 legacy；legacy 是否可达 | 无 | **一个问题，可能是一个开关** |
| **S1** | 失败的工具调用买回一轮 | 无 | **小**（一个判据 ＋ 一个上限 ＋ 错误文本） |
| **S2** | `invalid_proposal` 拆出来、回喂重试 | S1 的重试机制 | 中 |
| **S3** | 一份账 ＋ `blocked` terminal ＋ 三段渲染 | 无（可与 S1 并行） | 中 |
| **S4** | 时区 disclosure 断言 | 无 | **一条断言** |
| S5 | `ask_user_question`（`pause` → 下轮 `resume`） | S2 落地后 | 见 `harness-borrowings-implement-zh.md` §B |

**S1 和 S3 可以并行，互不依赖。S0 是一句话，先问。**

---

## 附：还没测的四件事（不阻塞上面任何一条）

1. **`invalid_proposal` 的真实样本** —— 当前 N=12 里是 0。见 §5 的 (a)/(b)。
2. **`Budget.report()` / `shrink_tool_result` 的 `kept/total` 实际值** ——
   session persistence 里没有。这正好是 `harness-borrowings-implement-zh.md`
   **阶段 A（预算台账落盘）** 要解决的事 —— **那条已经批过了，做它就有了。**
3. **首 token 时间 / 总耗时**（探针 7）—— 等 502 修好后重跑。
   源码上已经清楚：Ask 的 delta **立刻**发 `token`（`webapp/agent.py:837-842`），
   Agent 在全部执行 ＋ synthesis ＋ gate 之后才发（`webapp/agent_loop.py:3480-3529`）——
   **Agent 首字天然晚于 Ask，这不是 bug，但前端必须把中间 progress 当可见进度**，否则就是白屏。
4. **截图一那次 run 的实证** —— 该 session 在盒子上找不到了。
   `mdc_knowledge` 那句「参数不兼容」的**机器可读错误**还没拿到。
   **不用为它单独去找**：S1 落地后，同一个问题再问一次就会自己暴露，
   而且那时它会**自己修好**——如果没修好，错误文本会被回喂，届时直接就能看到。

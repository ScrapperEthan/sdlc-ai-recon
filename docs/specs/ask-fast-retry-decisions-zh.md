# Ask Fast 重试与一份账 —— 八项决定，三处更正，一条必须先裁决的矛盾

> 上游：内网 `ask-no-regression-bar-and-agent-fixes-information-zh.md`（2026-08-21）§6「实施规格必须先锁定的决策」
> ＋ `RUNBOOK-85-SEND-BACK-20260821` 的 fast vs no-mode 补充
>
> **验收标准仍在 [`ask-no-regression-bar-and-agent-fixes-zh.md`](ask-no-regression-bar-and-agent-fixes-zh.md)，本文不重复它**，
> 只更正其中三处（§2），然后逐条回答你们要的八项决定（§3）。
>
> **这份写的是"定什么、为什么这么定、怎么验收"，不写"怎么实现"。**
> 代码在内网，文件怎么分、函数怎么命名、状态放哪，**由内网 codex 判断**。
> 凡是提到内网具体行号或取值的，一律以真机为准 —— 对不上先回报，不要顺手适配。
>
> 你们那份 information doc 挖出了外网看不到的四个约束
> （`FAST_MAX_REPLANS=0`、同参去重、`normalize_mode` 归一、以及一条**已经断言了当前行为的测试**）。
> **这四个约束把"改一个判据"变成了"改四个地方"，本文按你们的发现重写了这一节。**

---

## 0. 🔴 先裁决一条内部不一致 —— 它决定第 3.2 和 3.8 项的优先级

两次回报对**同一个 N=12 样本**给出了完全不同的 decision 分布：

| | `SEND-BACK` 探针 1 | information doc §4.1 |
| --- | --- | --- |
| `invalid_proposal` | **0** | **8** |
| `internal_contract_conflict` | **1** | **4** |
| `user_input_required` | 5 | — |
| `policy_denied` | 0 | — |
| `approval_required` | 5 | — |
| `approved` | 1 | — |
| `approved_with_narrowing` | 0 | — |
| 合计 | 12 | 12 |

**两组都合计 12，但分布互不相容。**

按这个项目已经定下的规矩：**内部不一致 = bug，不是 gap。**
所以这不是"再测一次"的事，是**先说清楚这两个数各自统计的是什么**：

- 是**两个不同的口径**吗？（比如一个统计 `agent_harness` 最终产出的 decision，
  另一个统计 `agent_loop` preflight classification 的入参分类）
- 还是**两个不同的样本集**恰好都是 12？
- 还是其中一次算错了？

### 为什么这条要排在最前面

**这两个数指向完全相反的结论：**

- 若 **`invalid_proposal = 8`（12 条里 8 条）** → 两个桶合流的代价**极大**：
  **8 次本来模型自己就能改好的错误，被记成了系统缺陷并终止**。
  那么 §3.2 的重试就是本轮**最高优先级**，而且业主说的"参数校验错误经常出现"**被数据证实**。
- 若 **`invalid_proposal = 0`** → 合流目前**不产生任何实际损失**，
  §3.2 就是一条"防患于未然"的结构修复，可以排在一份账（§3.6）后面，
  而业主看到的"参数校验错误"是**别的东西**（多半是普通工具层的错，比如截图一那句「文档查询参数不兼容」）。

🔴 **请先给一句话裁决。这一条不解决，第 3.2 项的排序就是猜的。**

### 顺带一条（同类形状）

你们写的是已 fast-forward 到 **`c63b74b`**，外网这边推的那个 commit 是 **`e63b74b`**。
**`c` / `e` 是转录里最容易混的一对**，这个项目已经在 `hk1` / `hkl` 上栽过一次。
**请核一下是不是同一个 commit** —— 如果不是，你们看到的规格可能不是最新那份。

---

## 1. fast vs no-mode 的那张表：**它证明不了退化，也证明不了没退化**

你们跑出来的是：

| Run mode | Cases PASS | Checks PASS |
| --- | --- | --- |
| `investigation_mode` omitted（legacy compat） | **32/39** | 177/186 |
| `investigation_mode="fast"` | **31/39** | 176/186 |

差 1 条。但**真正要看的不是这个数字，是失败集合的形状**：

```
两边都失败(4):   honesty-vendor-is-not-inferable-from-repo-name
                 mcp-off-is-not-a-clean-log
                 mcp-two-lines-are-not-a-root-cause
                 send-mode-zero-is-pending-not-drift
只有 no-mode 失败(3): blank-vendor-is-not-evidence-of-which-carrier
                      category-33-37-are-defined-nowhere
                      cite-no-fabricated-references
只有 fast 失败(4):    cite-who-calls-a-service
                      delivery-path-is-classification-not-routing
                      refuse-unknown-use-case-id
                      standby-zero-pct-is-not-switched-off
```

🔴 **11 个失败里有 7 个是单边的，而且两个方向都有。**
**如果 fast 真的系统性更弱，不应该出现"legacy 失败而 fast 通过"的 3 条。**

**结论：这一次对照跑出来的差异，落在两次运行之间的自然波动范围内，判不出退化。**
（`temperature=0` 不等于逐位确定 —— 工具返回顺序、provider 侧的非确定性都会变。）

**所以基准线 A 的设计要改，见 §2.2。这不是你们跑错了，是外网原来的设计只跑一次，本来就判不出来。**

---

## 2. 对验收规格的三处更正

### 2.1 🔴 §2.7「临时止血：把 `investigation_mode` 改成不传」—— **作废**

你们查出来的事实推翻了它：

> 浏览器初始化 `currentRunMode = "ask"`，请求中**始终携带**该值；
> `server.py` 执行 `investigation_policy.normalize_mode()`，
> 其中 **`"ask"`、空值和未知值均归一为 `"fast"`**。

**所以"不传字段"不会走到 legacy，只会走到 fast。** 外网那条止血方案是错的。

**而且更正之后的结论是：不要为止血去改 `normalize_mode`。**

`"未知值 → fast"` 这条归一化**本身是对的**：一个拼错的、旧版本的、或者被中间层吃掉的 mode
**不应该落到一条行为不同的路上**。为了临时方便在这里挖一个 `"空 → legacy"` 的口子，
恰好造出**最容易被误触发的那种规则** —— 你们 §6-4 自己也担心这一点，这个担心是对的。

**替代方案**：见 §3.4 —— 开一个**非用户面**的开关，只给评测和排障用。
**用户面不做 legacy 回退**，直接做 §3.1–3.3 的修复。

### 2.2 基准线 A：换对照物 ＋ 必须重复采样

原规格写的是"legacy 跑一遍、fast 跑一遍，fast 的通过集合必须 ⊇ legacy"。
两个问题：

#### (a) 对照物要说清楚是什么

你们在探针 7 note 3 里指出来了：

> 「legacy compatibility Ask」是**当前代码的兼容路径**，不是无法在本机运行的外网历史 runtime；
> 因此**不能把第一行当作外网老 Ask 的真机对照**。

**这个提醒是对的，而且很重要。** 盒子上跑不出"外网老 Ask"。

**所以基准线 A 的对照物就定为「当前代码里的 legacy compat 路径」**，
但**前提是先静态核对它确实等价于老 Ask 的四条能力面**：

| # | 老 Ask 的行为 | legacy compat 路径是否一致？ |
| --- | --- | --- |
| 1 | 每一轮都把工具全集交给模型 | ? |
| 2 | 最多 `MAX_TOOL_ITERS`（8）轮工具 | ? |
| 3 | 工具异常 → 异常文本作为 `role:"tool"` 回喂 | ? |
| 4 | 回喂之后**下一轮模型手上仍有工具** | ? |

🔴 **这四格要先填。如果 legacy compat 自己也被削过，它就没有资格当基准线** ——
那就得回到静态对照（拿外网那份 `webapp/agent.py` 的行为当纸面基准）。
**这四格是本轮开工前最便宜的一件事。**

#### (b) 必须重复采样，比较的是「稳定失败集合」

**同一 mode 至少跑 3 次**，然后：

- **稳定失败** = 3 次都失败 → 这才是能力问题；
- **波动失败** = 1～2 次失败 → 这是方差，**不计入退化判定**，但要单独列出来（它本身是个信号：断言太脆或答案不稳定）。

**判据改成**：`fast` 的**稳定通过集合** ⊇ `legacy` 的**稳定通过集合**。

理由就是 §1 那张表 —— **单次比较会把方差读成退化，也会把退化藏进方差。**

### 2.3 §2.5「改一个判据就行」—— **不成立，是四重约束**

你们查得很清楚，外网原来的说法太轻了：

> 「目标规格的『买回一轮』至少同时涉及 **schema 暴露条件、工具轮预算、重复签名语义和最终失败数据**
> 四个现有约束；**只修改 `allow_tools` 条件不能达到验收**。」

尤其这一条外网完全不知道：

> `MAX_TOOL_ITERS` 当前默认 8，但 **Fast policy 的 `FAST_MAX_REPLANS` 默认值为 0**；
> `InvestigationBudget.begin_tool_round()` 在第二个工具轮会因 replans 拒绝。

**所以真正挡住第二轮的是预算，不只是 schema 开关。**
外网原话「这是改一个判断条件，不是改架构」**收回**——
准确说法是：**这是改四个约束，但它们都在 Fast 这一层，不需要动 Agent 编排。**

还有一条你们发现的、外网原来当成"疏忽"的东西：

> `tests/test_investigation_modes_and_sessions.py` **明确断言 tool surface 为 `[True, False]`**。

🔴 **这说明"第一轮之后收走工具"是被测试锁死的有意设计，不是漏写。**
所以改它要按"改变已断言行为"来走：**把那条测试拆成两条**
（全成功 → `[True, False]`；有失败且未超上限 → `[True, True, ...]`），
**而不是把它删掉或改松。** 这一点你们已经写在 §5 表里了，外网确认这个做法。

---

## 3. 你们要的八项决定

### 3.1 「默认 2 轮」的计数语义

**定：**

- **"买回轮"计的是"因为上一轮存在失败、而额外获得 schema 的模型工具轮"**，与首轮无关。
  默认 **2** → 最多 **3** 个带 schema 的工具轮（首轮 ＋ 2 个买回轮）。
- 这个计数**不复用** `FAST_MAX_REPLANS` 的语义。replan 是"重新规划"，买回是"同一个计划再试一次"，
  两个概念不要合并；但 `begin_tool_round()` 的预算必须**认**买回轮，否则预算会先拒绝。
  （具体是给 `FAST_MAX_REPLANS` 一个 >0 的默认值、还是新增一类 round 额度，**由你们判断** ——
  外网只要求：**买回轮不能被预算悄悄拒掉，也不能顺带放开真正的 replan。**）
- **最终答案里那个 N** = **实际发出的 dispatch 尝试次数**，
  不是模型轮数，也不是"每个工具分别计"。

**为什么**：模型轮数是内部概念，用户看不懂；"这件事我去试了 3 次"才是人话。

**验收**：断言"试了 N 次"里的 N 等于该轮实际 dispatch 计数；断言买回轮 ≤ 配置值。

---

### 3.2 可重试范围 —— **只有"改了参数就可能成功"的错误才交给模型重试**

**定：**

| 失败类型 | 模型可重试？ | 处理 |
| --- | --- | --- |
| 普通工具**参数错误**（模型写错了） | ✅ **可** | 回喂"字段/期望/实际" → 模型改参数 → 新 signature → 重试 |
| **无数据 / 空结果** | ❌ **不算失败** | 🔴 **空结果是结论，不是错误。** 不许进重试循环，也不许被描述成"查不到" |
| **timeout / connection refused**（对端） | ❌ 模型层不重试 | 交给传输层，见 §3.3 |
| **duplicate**（同参已调过） | ❌ | 去重本来就是防这个 |
| **权限 / access 拒绝** | ❌ **fail closed** | |
| **`policy_denied`** | ❌ **fail closed** | 措辞是"这是配置里的**故意限制**"，**不是"查不到"、不是"技术上可重试"** |
| **budget 耗尽** | ❌ | 正常收尾 |
| **incident task-bound Contract 失败** | ❌ **不由 Fast 层重试** | 🔴 它有自己的 preflight/harness 路径，**Fast 的重试不许绕过它** |
| **未分类内部异常** | ⚠️ **最多一次** | 且必须标成**我们的缺陷**，不伪装成 gap |

🔴 **这张表的原则只有一句：能被模型的下一次输出改变结果的，才值得再花一轮。**
其余的重试，只是把同一个失败重演一遍，还多烧一次模型调用。

**验收**：为表里前四类各写一条断言，确认第五、六、八类**不产生**第二次 dispatch。

---

### 3.3 同参 retry 与去重共存 —— **不要开洞，把传输重试留在传输层**

**定：不允许模型发起同参重试。** `canonical_signature` 去重规则**不动**。

**理由三条：**

1. 传输时效失败（`connection_refused` / `timeout`）是**传输层**的事。
   让模型再走一整轮去重发同一个请求，是拿一次模型调用去做一件重试循环该做的事。
2. 去重是个好设计。**为传输失败在去重上开例外，等于给"无限重试同一个调用"留了入口** ——
   而这正是你们 §6-3 担心的那件事。
3. 🔴 **你们已经在做了**：截图二那句
   `portal MCP SSE unreachable: connection_refused [after 2 attempt(s)]`
   —— **`after 2 attempt(s)` 就是传输层重试**。这条路已经存在，不需要在模型层再造一条。

**所以**：传输层重试（次数/退避由你们定）→ 全部失败后，**作为一条"对端不可达"的终局失败**
交给模型，模型**不再重试它**，而是在答案里说清"这条证据拿不到，原因是对端连接被拒"。

**验收**：断言同 `(tool, args)` 不产生第二次 dispatch；断言传输层耗尽重试后的失败**不进入**买回循环。

---

### 3.4 legacy 临时入口 —— **不对用户开放**

**定：**

- ❌ **不做面向用户的 legacy 入口**，`normalize_mode()` 的"未知/空 → fast"**保持不变**（理由见 §2.1）。
- ✅ 开一个**非用户面**的强制开关（名字你们定，例如 `SDLC_ASK_FORCE_LEGACY`），**默认关**：
  - 位置在 `normalize_mode()` **之后**做覆盖，**不参与归一化逻辑**
    —— 这样它不可能被一个空值或拼错的 mode 意外触发；
  - **不出现在 UI**，不进 `/api/chat` 的 body 契约；
  - 唯一用途：**双路径评测**（§2.2）和排障。
- 用户面的修复就是 §3.1–3.3，不走回退。

**验收**：断言开关默认关时，任何 HTTP/SSE 入口（含空值、未知值、`"ask"`）都到达 fast；
断言开关开启时两条 transport（普通 POST 与 SSE）行为一致。

---

### 3.5 评测基线存储

**定：**

- **不覆盖**：每次运行写一个**新文件**到 ignored 目录（例如 `scratch/evals/`），
  文件名含 `{mode}-{commit}-{UTC 时间戳}-{序号}`。
  **不要再往 `evals/last_run.json` 上覆盖** —— 那正是"盒子上只剩 4/4"的成因。
- **元数据必须记**：mode、model id、commit、UTC 时间、resolved case 集合、
  每 case 的 pass/fail ＋ **断言级明细**。
- **答案正文只留本地 ignored 文件**；tracked 文档/回报里只放 **case id 和计数**。
  （这条你们一直做得很好，继续。）
- 🔴 **每 mode 至少 3 次**，产出两个集合：**稳定失败** 和 **波动失败**（§2.2b）。

**验收**：断言运行不覆盖既有文件；断言 tracked 产物里不含答案正文。

---

### 3.6 「一份账」的 source of truth

**定：**

1. **权威 = 执行时写下的 runtime events**
   （`incident_contract_dispatch` / `incident_runtime_entry` / `incident_mcp_summary` 那一族）。
   理由：**它们是执行的副产品，不是事后重建的。**
   凡是"事后根据别的东西推出来的"，都不能当权威。
2. `run_steps` 和 safe debug trace **都是它的投影**，只许复制字段，**不许重构语义**。
   → 所以 **run-step whitelist / renderer allowlist 要把
   `contract_triggered`、`runtime_entered`、MCP attempted/executed/failed/suppressed 放进去**
   （你们指出这些字段目前不在白名单里，所以持久化之后重建不出来）。
3. 🔴🔴 **unknown 的表示，逐层定死：**
   - 内部/JSON：`null`（**不是** `false`，**不是** `0`）
   - API：字段保留，值为 `null`
   - UI：渲染 `—`
   - 🔴 **禁止 `bool(missing)` 和 `int(missing or 0)`。**
     你们自己指出 `agent_debug_trace.py` 的 diagnosis projection 就是这么写的
     —— **那两个表达式就是这个 bug 的本体**，不是它的症状。
4. 所有新增字段 **additive**。

**验收**：
- 断言：字段不存在时，三层各自是 `null` / `null` / `—`，**连续三层都不降级为 `否`/`0`**；
- 端到端：一次 **runtime 已进入、MCP 对端失败** 的 run，
  面板归因指向**对端**，**不出现"Harness 未批准"**；
- 断言：session reload 之后的视图与执行时视图一致。

---

### 3.7 terminal 契约

**定：**

| terminal event | `run.status` | 可 resume | 谁能解开 |
| --- | --- | --- | --- |
| `done` | `completed` | 否 | — |
| `cancelled` | `cancelled` | 否 | 用户（已取消） |
| `budget_exhausted` | `budget_exhausted` | 否 | 用户（再问一次 / 放宽预算） |
| **`paused`**（新） | `paused` | **是** | 按 `pause_reason` 分 |
| **`blocked`**（新） | `blocked` | 否 | 按 `primary_cause` 分 |

**`paused` 和 `blocked` 的分界只有一条：这一轮还能不能继续。**

- `user_input_required` → **`paused`**（拿到回答就能接着跑）
- `policy_denied` / `internal_contract_conflict` → **`blocked`**（这一轮到此为止）

**不可违反：**

1. 🔴 **`paused` 绝不许发成 `done`。**
   这条 `harness-borrowings-implement-zh.md` §B.4-2 已经定过
   （「pause 理由必须显式，绝不许伪装成 completed」），**现在是它的第二个实例**。
2. **兼容**：旧消费端遇到未知 terminal → **当作终结处理并显示原文 reason**，
   **不许当成失败，也不许当成 done**。
3. **additive**：`paused` / `blocked` 事件**仍然携带**
   `answer` / `tool_trace` / `usage` / `citations` 等既有字段。
4. **三段渲染**（`why_stopped` / `who_can_close_it` / 第三段命名你们定）
   要在**答案位置**渲染，不是只进 debug 面板。
   `who_can_close_it` / `resolution_owner` **已经在 gap/closure 结构里了**，只差提上来。

**验收**：断言 `run.status == "paused"` 的 run 的 terminal type **不是** `done`；
断言三类停止的 `who_can_close_it` **三条各不相同**（用户 / 我们 / 配置拥有者）。

---

### 3.8 `invalid_proposal` 的可修复反馈

**定：**

1. 回喂内容**三件**：
   - **哪个字段**（proposal 里的字段路径）
   - **期望什么**（类型 / 格式 / 枚举 —— **来自 schema**，不是模型也不是人现编的）
   - **实际拿到什么**：只给**值的形状**（类型、长度、是否为空），🔴 **不给值本身**
2. 🔴 **不许把 rejection code 原样丢给模型**（内部密态）。
   要有一张**代码里的常量映射表**：rejection code → 一句只含 schema 层信息的话。
   **映射表是代码，不是模型生成的。**
3. 🔴 **信息不可得时，说"具体字段未知"，绝不许编一个期望值。**
   —— 这是这个项目六轮里最贵的那条教训：
   外网在示例 JSON 里编过值、给字段安过含义（`55` 说成"存不下引用"，实为"扫过且干净"）。
   **一个编出来的期望值，会让模型自信地改成另一个错的参数。**
4. 第二次调用**必须重新走完整的 preflight ＋ harness ＋ approval ＋ budget ＋ allow-list**。

**🔴 安全论证（这是唯一可能被挡下来的地方）：**
把 `invalid_proposal` 回喂给模型**不扩大任何权限**。
它发生在 `action_within_harness_decision()` **之前** —— 是对一个 *proposal* 的校验，
**此刻没有任何真实 MCP / 生产动作发生**，ceiling、approval、budget 三道闸门**一条都没有动**。
**"不回喂"换来的不是安全，只是一次白白浪费的往返。**

**验收**：
- 断言回喂文本包含字段名，**且在信息不可得时包含"未知"而不是一个具体值**；
- 断言回喂文本**不含** rejection code 原文；
- 断言第二次调用**完整经过** preflight/harness/approval/budget/allow-list（不能走捷径）。

---

## 4. 一条新发现 —— 本轮**不修**，但要记一笔

你们探针 7 量出来的：

| 路径 | 首 token（秒） | 总耗时（秒） | 运行事实 |
| --- | --- | --- | --- |
| legacy compat Ask | 3.287 | 3.788 | **0 tool calls** |
| 新 Ask（fast） | 4.266 | 5.116 | 1 tool call |
| 新 Agent（deep） | **45.159** | 45.183 | **0 tool calls、8 model calls**；`done` 但 `run.status=paused`、`stop_reason=user_input_required` |

问题不是"Agent 慢"（它首字天然晚，这是设计）。问题是这一行的**组合**：

🔴 **对"什么是MDC"这样一个基础定义问题，Agent 花了 45 秒、8 次模型调用、
一个工具都没调，最后停下来问用户。**

**8 次模型调用 ＋ 0 次工具调用 = 这 8 轮里没有任何一轮产出了可执行的任务。**

**本轮不修**（不在业主这次要解决的范围内，而且 §3 那八条落地后这里的表现可能就变了）。
但**记一笔**，并且顺手回一个数就够：

> 那 8 次模型调用里，**每一次的角色是什么**（planner / executor / synthesizer 各几次）？
> 以及 `stop_reason=user_input_required` **具体在问什么**（问题文本的**类型**即可，不要正文）？

如果答案是"8 次都是 planner"，那是编排在原地打转；
如果是"planner 出了计划、executor 拿不到工具"，那和 §3.2 是同一个病。
**一句话就能分开，所以顺手问一下。**

---

## 5. 不要重复施工的地方

你们 §4.5 指出来的，外网确认：

- **HKT disclosure 在当前工作树很可能已经做完了**
  （`webapp/time_policy_presenter.py` ＋ `append_notice()` ＋ 未跟踪的
  `tests/test_time_policy_presenter.py`）。
  **不要重复施工。** 只需确认一件事：
  🔴 **测试断言的是最终 terminal answer 的文本，还是只断言了 state/debug 标记？**
  只断言标记 = 这条没做完（"记了但没显示"和没记是一样的）。
- **工作树是多人并行的**（`agent.py`、`app.js` 已修改；
  `agent_loop.py`、`agent_debug_trace.py`、`time_policy_presenter.py` 未跟踪）。
  **施工前先核对归属，不要 reset / restore / stash 别人的改动。**
  这条是你们自己提的，外网完全同意 —— **并行踩踏比慢一天贵得多。**

---

## 6. 外网这一轮又错了两条（连同上一轮六条，一并记账）

| # | 外网写的 | 你们查到的 | 结论 |
| --- | --- | --- | --- |
| 7 | 止血 = 把 `investigation_mode` 改成不传 | 浏览器**始终**传 `"ask"`，`normalize_mode()` 把 `"ask"`/空/未知**全部**归一为 `fast` | ❌ **§2.7 作废**，改为 §3.4 的非用户面开关 |
| 8 | "改一个判据就行" | 还有 `FAST_MAX_REPLANS=0`、同参去重、以及**一条已经断言了当前行为的测试** | ❌ **是四重约束**，见 §2.3 |

加上上一轮的六条，**外网八轮里的每一次缺陷都是同一个形状：对内网环境下断言**
（名字 → 响应形状 → 值格式 → 控制流 → 参数传递 → **这次是预算与去重语义**）。

**所以这份文档里凡是"定什么 / 验收"的都是规格；凡是提到你们的取值、行号、默认值的，一律以真机为准。
对不上先回报，不要顺手适配。**

---

## 7. 建议的开工顺序

| | 做什么 | 依赖 | 大小 |
| --- | --- | --- | --- |
| **0** | 🔴 裁决 §0 那条 decision 计数矛盾（一句话）＋ 核对 commit hash | 无 | **一句话** |
| **1** | 填 §2.2(a) 那四格：legacy compat 是否等价于老 Ask 的四条能力面 | 无 | **静态核对，最便宜** |
| **2** | §3.6 一份账 ＋ §3.7 terminal 契约 | 无 | 中 —— **它让后面每一步都能被看见** |
| **3** | §3.1–3.3 Fast 买回一轮（四重约束一起改，测试拆成两条） | 无 | 中 |
| **4** | §3.8 ＋ §3.2 Agent 的 `invalid_proposal` 回喂 | 3 的重试机制 | 中（优先级由 §0 决定） |
| **5** | §3.4 非用户面开关 ＋ §3.5 基线存储 ＋ §2.2(b) 三次重复采样 | 4 | 小 |
| **6** | 确认 §5 的 HKT disclosure 断言到最终文本 | 无 | **一条断言** |

**2 和 3 可以并行。0 和 1 是几句话，先做。**

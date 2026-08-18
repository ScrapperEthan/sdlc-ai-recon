# Agent 模式修复 —— 六期定案（RUNBOOK-83 回报之后）

> 这份取代内网《Ask 能力不退化、共享回答契约与 Agent 增强修复计划》的**排期与优先级**部分；
> 那份文档的问题分析、Answer Packet 骨架、测试矩阵继续有效。
> schema 定稿见 [`agent-ask-parity-and-answer-packet-zh.md`](agent-ask-parity-and-answer-packet-zh.md) §3–§4。
>
> 日期：2026-08-17　依据：`RUNBOOK-83` 送回（HEAD `8eb525f`，1637 tests 全绿）
>
> **业主已拍板四件事**，本文档据此收敛：
>
> | 问题 | 决定 |
> | --- | --- |
> | 范围 | **直接开六个 phase**（不做"先五处小改"） |
> | 产地表谁维护 | **内网维护**；外网只出注意事项（spec §5.1） |
> | `blocked` 时给什么 | **必须交出已做的事 ＋ 已拿到的答案 ＋ 为什么 block**（§4） |
> | 不退化的口径 | 外网设计，见 §5 |

---

## 0. 回报改了什么（这一节决定了下面的顺序）

**外网上一轮的主根因判断是错的，内网的实测把它证伪了。** 更正记录在
[spec §2.2 / §11](agent-ask-parity-and-answer-packet-zh.md)。四条实测：

1. **真实机制 = claim 生成后被引用校验闸门拒掉**
   （`draft_claims=1`、`gate_accepted=0`、`gate_rejected=1`，
   原因 `one or more citations failed verification`）。
   不是"证据全落 gap"，也不是"claim 从来没生成"。
2. 🔴 **`webapp_data/agent_turns/` 文件数 = 0。** B2.2 标 `done`，检查点一个没落盘。
   → **历史那一轮无法复盘**，本轮所有诊断（含外网那条错的）都是猜的。
3. **路由这一条在当前代码里可能已经是对的**：分类器实测
   `alert_diagnostic=true` ＋ `alert_structured_exception=true` → `incident_required=true`；
   且 `incident_required=true` 时 planner 会校验计划含 `incident_investigate`，
   `incident_investigate` 的兜底集合是空数组（没有弱替代）。
4. **闸门那一片是唯一被四个场景独立确认的缺陷区**：`facts=3, claims=[]` 判 `valid=true`；
   空 repair 胜出且无标记；被拒 claim 不转 gap；无 `blocked` 态；
   **`answer_status` 这个概念根本不存在**；前端顶栏显示执行状态。

**所以六期的顺序按证据重排：内网原本的 Phase 5（Answer Gate）提到最前，
原本的 Phase 1（共享路由）后移。**

> **一条方法论上的结论，比这次的 bug 更值钱**：外网这一轮之所以判错，
> 是因为**唯一能证明机理的东西（台账）没落盘**。所以 P0 的第一件事不是修答案，
> 是**让一次跑可以被复盘** —— 否则下一次还是猜。

---

## 1. 六期定案

| 期 | 名字 | 为什么排这个位置 | 硬阻塞 |
| --- | --- | --- | --- |
| **P0** | 可复盘：台账落盘 ＋ 拒绝原因 telemetry | 没有它，后面五期的每次验收都只能靠"看起来对了" | 无 |
| **P1** | Answer Gate ＋ 答案状态 ＋ blocked 渲染 | **唯一被四个场景实测确认**的缺陷区；直接产出"结论：无" | 无 |
| **P2** | 引用校验：从"全拒"改成"降级 ＋ 说明" | 实测的真实拒绝原因就在这里 | 需 P0 的 telemetry 定位到底哪条引用失败 |
| **P3** | Answer Packet ＋ Claim Ledger | `answer_packet.py` 尚不存在；P1/P2 的结构化前提 | schema 先定稿（spec §3–§4） |
| **P4** | 共享 Intent Router（布尔 → 三态） | 可能已经不是本次的病因，但 `unknown ≠ false` 这条契约仍要立 | 需先确认 2026-08-17 那一轮跑在哪个 commit |
| **P5** | Baseline Answer Pipeline ＋ 能力护栏 | 最大的工程量，也是"agent ⊇ ask"这个承诺的落点 | 需 P3 的 Packet |
| **P6** | 前端 ＋ 不退化评测 ＋ 灰度清理 | 评测的断言依赖 P3 的 Packet 字段 | 需 P3 |

**特性开关砍到两个**（内网原方案六个 = 2⁶ 种组合，灰度期间没人说得清线上跑的是哪一种）：

```
SDLC_AGENT_ANSWER_STATUS_ENABLED     # P1 + P2 + P6 的渲染与状态
SDLC_AGENT_ANSWER_PACKET_ENABLED     # P3 + P5 的结构化链路
```

P0 无开关（只增加落盘和 telemetry，不改行为）。P4 复用现有 `incident_required` 的入口，
三态是**替换**不是并存 —— 两套路由并存是下一次翻车。

---

## 2. P0 —— 可复盘（第一件事，独立提交）

**问题**：`webapp_data/agent_turns/` 文件数 0。
**不做的后果**：**这一轮已经付过一次学费了** —— 外网基于一条无法验证的推断写了 700 行 spec，
方向错了一半。下一次 agent 模式出问题，还是只能看最终答案猜机理，
而"猜错的诊断"会直接变成"改错的代码"。还有两个直接损失：刷新页面 = 两分钟白等，
以及**已经打出去的生产 sweep 白打**（对面是同事的服务器）。
**做完的效果**：任何一轮都能事后回答"第几个任务拿到什么才拐的弯"。

### 要做的

1. **查清检查点为什么没落盘。** B2.2 标 `done` 但目录是空的 ——
   是没接线、TTL 太短、写失败被静默吞掉（B2.2 是全项目唯一 fail-OPEN 的地方，
   `notice` 发出来了吗），还是路径不同。**先回报结论，再改。**
2. **台账新增 `attempted` 计数**（内网回报明确说"当前台账没有 `attempted` 字段"）：
   每个工具返回后 `attempted / as_evidence / as_fact / as_gap / as_assumption` 五个数。
   没有这五个数，"证据去哪了"永远只能靠重放猜。
3. **拒绝原因 telemetry 落盘**：每条 claim 的 `accepted | rejected | downgraded`
   ＋ 拒绝码 ＋ **具体是哪一条引用/哪一个字段导致的**。
   现在只有一句 `one or more citations failed verification` ——
   **"one or more" 是这次查不下去的直接原因**，必须逐条。
4. 🔴 **闸门不许放松**：落盘的仍然只能是台账摘要 ＋ 证据指针 ＋ 分类结果。
   **原始工具返回包、未脱敏 payload、真实 alarm name 一个都不许进检查点**
   （灌一个带 PII 的假 packet，断言落盘文件里搜不到它 —— 这条单测本来就该有）。

### 验收

- 跑一轮 agent 模式后，`webapp_data/agent_turns/` 里有文件，刷新页面能回放。
- 能从单次会话明确判断是**路由漏选 / 权限阻断 / 执行失败 / 证据归一化丢失 / 引用校验拒绝 / 输出闸门清空**——
  六选一，不用重放、不用猜。
- 带 PII 的假 packet 灌进去，落盘文件里搜不到。

---

## 3. P1 —— Answer Gate ＋ 答案状态

四个实测场景就是四条验收。**当前实现里没有 `answer_status` 这个概念**，这是本期的核心新增。

| 实测的现状 | 改成 |
| --- | --- |
| `facts=3, claims=[]` → `valid=true`，渲染"确定性结论无 ＋ 已确认事实无" | `answer_status=partial`，**Evidence 必须渲染出来** |
| 空 repair 按"rejected 数没增加"胜出，吃掉初稿 2 条已支持 claim，无标记 | 初稿胜出；记 `empty_repair_erased_supported_facts` |
| 3 accepted / 2 rejected → 保留 3 条，但**被拒的 2 条不转 gap** | 被拒的必须转成 gap，带拒绝原因 |
| 全工具不可用 → 没有 `blocked` 态 | `answer_status=blocked` ＋ §4 的四段渲染 |
| 前端顶栏显示 `state.status / run.status` | **顶栏优先显示 `answer_status`**；执行状态降为次要 |

### `answer_status` 的判定（**纯代码算，模型不许写**，spec §4.6）

> **2026-08-18 更正**：本节原来写的是"按 `accepted_claims` / `evidence` 计数"的公式，
> 它和 §4 渲染契约里"blocked 也可能保留辅助 Claim/Evidence"**自相矛盾** ——
> 内网 §17.2 第 4 条抓到了，改法采纳他们的：**按 required deliverable 覆盖度推导**。

```
complete : 所有 required deliverable 都已闭合
partial  : 部分 required deliverable 闭合
blocked  : 零个 required deliverable 闭合，且有明确阻塞原因
           （能力全被拒 / 预算耗尽 / 窗口拒绝 / 引用全不可核）
```

`blocked` 和 `partial` **都可以携带辅助 claim 和 evidence** —— 例如用户问"要不要重发"，
这一条没闭合（blocked），但"这个告警对应哪个仓库"已经确认，那条辅助 claim 照常输出。

🔴 **两条护栏，缺一不可：**

1. **"deliverable 闭合"的定义只有一种：被一条 accepted claim 闭合，或被一条显式 gap 闭合。**
   **绝不能由 task 的 `done_when` 判定。** 否则就是内网自己 §2.3 诊断的那个缺陷原样重演 ——
   三个本地 task 各自 `done_when` 都满足 → 系统进入 `completed` → 而用户真正要的维度从未执行。
   **"任务做完了"和"问题回答了"是两件事，这是整份计划的起点，不能在状态推导这里又合回去。**
2. **保留 `evidence > 0 && accepted_claims == 0 → status != complete` 作为冗余断言。**
   它现在不是主判据了，但它是**唯一被真机复现过的那个失败形状**的直接守卫 ——
   主判据换了实现，这条独立断言要留着（成本一行，收益是这次的 bug 不会以别的路径回来）。

### repair 质量比较（不许只比 rejected 数）

至少比六项：accepted 数、已覆盖的 deliverable 数、baseline claim 是否还在、
qualifier 是否还在、是否新增了不可核引用、是否把 complete 降成 empty。
**零拒绝零接受不优于部分接受部分拒绝。**

### 一句必须删掉的默认文案

> 「当前没有通过证据出口闸门的确定性结论。」

它是最坏的一种回答：**既不说做了什么，也不说为什么**。
删掉之后没有替代文案 —— 走 §4 的四段渲染。

---

## 4. blocked / partial 的渲染契约（业主决定，硬要求）

**`blocked` 和 `partial` 都必须交出四段，缺一段就是不合格。**

```
## 结论
- （accepted claim 有几条就写几条。blocked 不等于全空 —— 只有真的零 accepted 才没有这一节）
- 一条都没有时，写"目前不能给出确定性结论"，并且**紧接着回答为什么**，不许单独成段

## 已经做了什么
- 执行了哪些能力 / 哪几个任务、几次工具调用、耗时；哪些任务 done / failed / skipped
- （这一节的数据全部来自台账，不需要模型生成）

## 已经拿到什么
- 每条 Evidence，带出身（environment / evidence_grade / as_of）
- 被拒的 claim：原文 ＋ 拒绝码 ＋ **具体哪一条引用/哪个字段不合格**
- 🔴 **有 Evidence 就必须出现这一节。"查到了但不能下结论"和"什么都没查到"是两个不同的答案**

## 为什么停在这里 / 谁能解开
- 逐条 gap：`cause`（封闭词表）＋ `cause_field`（是哪个字段这么说的）＋ `who_can_close`
- 明确是哪一层拒的：路由 / 权限 / 预算 / 引用校验 / 窗口拒绝 / 产地表缺行
- 用户**现在立刻能做的那一件事**（例：「这个 03:15 是 HKT 还是 UTC」）
```

三条禁令：

1. **不许用"无"覆盖状态里实际存在的内容**（空章节可以省略，但不能写"无"）。
2. **不许把"没能形成结论"说成"没有影响 / 没有异常 / 日志是干净的"** ——
   这是 ask 提示词里已经写死的规矩，agent 模式同样适用。
3. **不许只报状态字。** 前端显示 `blocked` 但正文只有一句话，等于没答。

---

## 5. 不退化的口径 —— 设计（业主让外网定）

### 5.1 先说 opencode 那部分

opencode（终端 AI 编码 agent）里和这件事同构的机制是 **mode**：
`build` / `plan` 等模式**不是两条流水线**，而是同一个 agent 循环上的
**工具权限 ＋ 提示词覆盖**（plan 模式把写文件那几个工具关掉）。
会话、消息存储、渲染全部共用一份。

> ⚠️ 这一段是外网凭对 opencode 的一般了解写的，**离线、没有对着源码逐行核过**。
> 要当依据用的话，让内网或外网任一侧核一遍再引用。

**能直接借的一条**：**模式即策略，不是第二套系统。**
这和外网自己 spec §4 写的"一个循环两套策略"是同一句话，而这次退化的根源恰恰是
**agent 长成了平行链路** —— 自己的 planner、自己的 synthesizer、自己的答案渲染。
所以 P5 的验收要有一条：**ask 和 agent 的最终 Markdown 由同一个 renderer 产出**，
不是两段各自维护的拼字符串代码。

**不能借的**：opencode 的模式差异是"能不能写文件"这种**离散权限**，
一眼能看出退化没退化；我们的差异是"答得好不好"，**必须自己定量**。下面是定量方案。

### 5.2 比较的单位是 claim，不是文本

🔴 **绝对不要做文本相似度或 LLM 裁判。**

- 文本比对会把"换了个说法"报成退化 → **一个总是红的测试等于没有测试**
  （RUNBOOK-66 的教训：报 14/20 实为 19/20，五个红全是断言写错的）。
- LLM 裁判会让"退化了没有"这个问题本身变成第二个不确定源。
  能代码判的一律代码判 —— 这是项目硬规矩。

**做法**：同一个 question，同权限、同数据、同时间点跑两个模式，各得一个 Answer Packet，
按下面六根轴逐轴出**判定**，**六轴全部通过才算不退化**。

| 轴 | 从 Packet 取什么 | 通过条件 | 什么叫退化 |
| --- | --- | --- | --- |
| **能力** | `executed_capabilities` ＋ Intent Contract 的 `required` | ① agent 满足 contract 的 required；② agent 的实际工具链 ⊇ ask 的实际工具链 | ① 判红；② **只判黄，不判红**（见下） |
| **结论** | `claim_key` 集合（`subject / predicate / object`，spec §4.7） | agent ⊇ ask；缺的必须带 `superseded_by` ＋ reason ＋ 新证据 id | **静默**少一条 |
| **强度** | 每个共同 `claim_key` 的 `environment` ＋ `evidence_grade` | 🔴 **不许更弱，也不许更强** | 更弱＝丢证据；**更强＝过度声称**（把 snapshot 说成 production），后者更危险 |
| **未知** | `gap.cause` 集合 | agent 的 gap 不得凭空消失；消失必须伴随一条闭合它的新 claim | gap 静默蒸发 |
| **视图** | `views[].kind` | agent ⊇ ask | 丢图（内网自己承认现在会丢） |
| **状态** | `answer_status` | 有 evidence 时不得渲染成"无" | 本次那一轮 |

> **2026-08-18 补充（采纳内网 §17.2 第 6 条）**：**`required` 只能来自 Intent Contract，
> 不能拿"ask 实际调过什么"倒推。** 理由：ask 可能顺手调了辅助工具，
> 把它固化成硬性要求会让这个测试变成噪音，而**一个总是红的测试等于没有测试**。
> 所以能力轴出**两个判定**：契约合规判红；实际工具链差异判黄。
> 🔴 黄的处理方式很关键：**黄 = 去修 Intent Contract，不是去改 agent。**
> "ask 调了它、agent 没调、而且答案确实更差"这件事说明的是**契约漏标了一条 required**，
> 这正是契约需要被修正的信号 —— 把它判红只会逼人把噪音断言注释掉。

### 5.3 四条设计要点（这是"怎么定口径"这件事本身的答案）

1. **允许变，但必须留痕。** 断言的不是"不许变"，是**"变了必须有账"**：
   agent 可以删 claim、降 confidence、改措辞，前提是有 `superseded_by` ＋ 原因 ＋ 新证据 id。
   守的是**可审计性**，不是一致性 —— 后者会把 agent 锁死在 ask 的水平上，那就没必要做 agent 了。
2. **双向断言强度。** 只查"变弱"是半个测试。这个项目翻车史里"说强了"占多数
   （0% 说成关掉了、55 说成存不下、名字推断说成代码证据）。
3. **基线要冻结，而且 ask 漂了先修 ask。**
   每条 case 存一份 `evals/golden/<case>.ask.json`，只存可比字段（六根轴用到的），
   **不存原文、不存原始 packet**。ask 的输出变了 → 先判断是 ask 修好了还是 ask 漂了，
   再决定更新 golden。**现有 39 条在 `mode=ask` 下逐条不变**这条红线不动。
4. **输出一张逐轴表，不是一个分数。**
   分数会掩盖"六个能力都在、但全被降了一档"这种情况 —— 而那正是最需要看见的一种退化。

### 5.4 覆盖面

内网 §12.1 那十类能力各至少一条（精确代码定位 / repo 影响 / message flow /
Use Case 路由 / 业务主数据 / UAT named query / inline 架构视图 / incident impact /
生产调查 / 用户纠正后的追问），每类至少覆盖：有完整证据、只有部分证据、工具无结果、
工具不可用、多环境证据混合、用户表达有歧义。

**先挑三条能跑通的立起来**（建议：精确代码定位、repo 影响、incident impact），
剩下七类随 P5 补 —— 一次要十类会拖死 P1。

---

## 6. 各期与内网原方案的对应关系

| 内网原 Phase | 本方案 | 变化 |
| --- | --- | --- |
| Phase 0 回归样例与可观测性 | **P0** | 内容换了：重点从"造 fixture"变成"**让检查点真的落盘** ＋ attempted 计数 ＋ 逐条拒绝原因"。fixture 随 P6 评测一起做 |
| Phase 1 共享 Intent Router | **P4**（后移） | 探针 5 显示当前代码对这条告警**已经**判 `incident_required=true`。契约仍要立（`unknown ≠ false`），但它不是本次病因，别再当 P0 |
| Phase 2 共享 Baseline Answer Pipeline | **P5** | 保留。加一条验收：**同一个工具结果只执行和归一化一次**（不许"先跑一遍 ask 再规划"，那是延迟和生产查询翻倍） |
| Phase 3 Baseline Capability Guard | **P5 的一部分** | 缩小：planner 已经会校验 `incident_investigate`、其兜底集合已是空数组。剩下的是**推广到所有 required 能力** ＋ 三个校验码 |
| Phase 4 Answer Packet ＋ Ledger | **P3** | 保留。接缝已知：`evidence_normalizer.normalize(name, result, task_id, payload, call_context)` ＋ `tool_subset.TOOL_METADATA` |
| Phase 5 Answer Gate / 合并 / Renderer | **P1 ＋ P2**（提前） | 拆成两期：答案状态（P1）和引用校验降级（P2）。**这是唯一被实测确认的缺陷区** |
| Phase 6 前端 / 灰度 / 清理 | **P6** | 保留。开关从六个砍到两个 |

---

## 7. P2 单独说一句：引用校验不该"全拒"

实测的拒绝原因是 `one or more citations failed verification`，而且真机上：

- **`read_file` 返回裸字符串**（135 字符），不是 `{path, line, lines}` 对象；
- **`search_code` 返回裸行列表**；
- 即**唯二能产出可核引用的工具，返回的都是非结构化文本**。

所以 P2 的三件事：

1. **先定位，别先改。** P0 的逐条 telemetry 上线后，回报**具体哪一条引用、哪一项校验失败**
   （路径不存在？行号超范围？后缀不在白名单？根本没解析出 `path:line`？）。
2. **一条引用不合格 ≠ 整条 claim 作废 —— 但也不是一律降级保留。**

   > **2026-08-18 更正**：本节原来写的"引用不可核 → 一律降级保留"和
   > [spec §4.2](agent-ask-parity-and-answer-packet-zh.md) 那张表**自相矛盾** ——
   > 那张表明写着 `code_location` 类结论**必须**有 `verified: true` 的行号引用。
   > 内网 §17.2 第 5 条抓到了，改法采纳他们的：**按 claim 类型分档处理**。

   | claim 类型 | 引用不可核时 | 为什么 |
   | --- | --- | --- |
   | `code_location` | **拒绝该 claim**，转成 Evidence ＋ 一条 gap | 没有可核行号就不能声称精确位置（spec §4.2、ask 提示词第 8 条） |
   | `count` / `impact_set` / `routing` | **降级为带限定的 claim** ＋ gap | 数字和集合来自工具的字段，不依赖行号；限定说清"未能核实引用" |
   | `operational_decision` / `mechanism` | **拒绝**（这两类本来就要求生产或代码证据） | 引用是它唯一的落地点 |
   | `ownership` / `status` | 降级 ＋ gap | 出处是数据集，不是文件行 |

   🔴 **无论落到哪一档，都不许静默消失**：被拒的 claim 必须以
   Evidence ＋ `cause: citation_unverified` 的 gap 形式出现在答案里 ——
   探针 4 场景 3 实测"被拒的 2 条不转 gap"，那条缺陷在这里同样适用。
   **分档决定"能不能当结论说"，不决定"要不要告诉用户"。**
3. 🔴 **顺手补一个已知缺口**：引用后缀白名单**漏了 `.js` / `.groovy`**（2026-08-07 实测），
   命中这两种后缀时引用守卫**被整个跳过**。这条与本次可能无关，但同一个模块，一起修。

---

## 8. 全期通用检查清单

- [ ] ask 模式行为没变（现有 39 条 `mode=ask` 逐条不变）
- [ ] 新增可调项进 `config/*.json` ＋ `.local.json` 覆盖，**没有硬编码进 Python**
- [ ] 没有把他们环境的名字/形状/格式写进代码
- [ ] 一个名字只有一个绑定（from-import 只拿常量，patch 打在拥有者模块上）
- [ ] 没碰 `webapp/llm.py` facade、`incident_investigator` 出口脱敏闸门、`db_readonly` 的 `caller_policy`
- [ ] 检查点 / session 里没有原始工具返回包（灌 PII 假 packet 断言搜不到）
- [ ] `answer_status` / `evidence_grade` / `rank` 由**代码**算，模型输出里出现 `_derived` 键 → 整包拒收
- [ ] 不许跨环境比 `rank`
- [ ] 没有引入第三方依赖
- [ ] 新代码能在 `LLM_MOCK=1` 下离线跑通
- [ ] **每期做完，仓库可运行、测试全绿**（当前基线 1637）

---

## 8.5 内网 §17.2 的九条"未原样采纳"—— 外网逐条回应（2026-08-18）

**九条全部认可**，其中两条抓到了本文档真实的自相矛盾，已按他们的改法改在正文里。

| # | 内网的处理 | 外网 | 落在哪 |
| --- | --- | --- | --- |
| 1 | 不再把"Router boolean 必然返回 false"写成已证实的历史第一根因 | ✅ 认可，本文档 §0.3 / §6 本来就是这个口径 | — |
| 2 | 旧推断改成"工具级 provenance 粒度过粗，有过度评级风险" | ✅ 认可，即 spec §2.2 的更正 | — |
| 3 | 完整 Provenance JSON 不当最终配置，只吸收匹配规则 / 粒度 / 代表性映射 | ✅ 认可，与业主"产地表内网维护"的决定一致 | spec §5.1 |
| 4 | `answer_status` 改按 required deliverable 覆盖度推导 | ✅ **他们抓到了矛盾**：原公式与"blocked 也可保留辅助 Claim"冲突。已改，**附两条护栏** | §3 |
| 5 | 引用不可核按 claim 类型分档，不是一律降级保留 | ✅ **他们抓到了第二处矛盾**：原写法与 spec §4.2「`code_location` 必须有可核行号」冲突。已改，**附一条底线** | §7 |
| 6 | `required` 只能来自 Intent Contract，parity 同时比契约与实际工具链 | ✅ 认可，**补了黄灯的处理方式** | §5.2 |
| 7 | opencode 类比不作为设计证据写入正文，只留可独立成立的原则 | ✅ 认可，**而且这条比外网做得对**：未核验的外部类比不该成为规格的承重墙 | — |
| 8 | Ledger（P3）与 lineage UI（P6）分开，后者不作前置阻塞 | ✅ 认可，与本文档排期一致 | — |
| 9 | 开关六个收敛为两个 | ✅ 认可（本就是外网建议） | §1 |

三条附加条件已写进正文，此处只列指针：**§3 的两条护栏**（deliverable 闭合只能由
accepted claim 或显式 gap 判定，绝不能由 `done_when` 判定；保留计数不变式作冗余断言）、
**§7 的底线**（分档决定"能不能当结论说"，不决定"要不要告诉用户"）、
**§5.2 的黄灯语义**（工具链差异 = 去修契约，不是去改 agent）。

---

## 9. 还需要内网回报的两件事（不阻塞 P0，但阻塞 P4）

1. **2026-08-17 那一轮跑在哪个 commit 上？** 分类器现在判 `true`，
   分析文档里是 `false` —— 是代码已经改了，还是文档的数字来自更早的状态？
   这决定 P4 是"修一个还在的 bug"还是"给一个已经好了的地方立契约"。
2. **检查点为什么没落盘？**（P0 第 1 项）先回报结论再改。

---

## 10. 一句话对外说法

> agent 模式现在的毛病不是"查得不够"，是**查到了却不肯说**。
> 六期改完之后，它对任何一个问题的答案都由三段构成：
> **已经确认的直接说、只能部分确认的写清限定、没确认的如实列出并且说明谁能补上** ——
> 而且**做了什么、拿到什么、为什么停在这里，永远交出去**，不再有一句
> "没有通过证据出口闸门的确定性结论"就把两分钟的工作抹平。

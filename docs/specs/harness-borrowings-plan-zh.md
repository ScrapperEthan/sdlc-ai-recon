# Harness 借鉴 —— PLAN（RUNBOOK-84 回报之后）

> **这是 plan，不是 implement md。** 确认没问题之后才写实现文档。
>
> 依据：[`RUNBOOK-84-harness-borrowings-recon.md`](../../RUNBOOK-84-harness-borrowings-recon.md)
> 的内网回报（2026-08-18，`1668 passed`）。
>
> **结论先说：七条里砍掉四条，留下三条 ＋ 一条小的补充。**
> 被砍的四条全是"机制类"借鉴，被砍的理由全部来自内网的实测数字，
> 不是外网改了主意。留下来的三条里有两条是纪律，一条是真能力。

---

## 0. 先纠正一条：外网上一轮把 spill 排在第一位，排错了

上一份 RUNBOOK 里我写"spill 最值得做"，理由是"切掉的部分永久没了，
模型会把 361 条里的 90 条当成全部报出去"。

**回头核我们自己的代码，这个理由有一半不成立。**
`context_budget.shrink_tool_result()` 现在已经做了三件事：

- 永远输出**合法 JSON**，不会给模型半截对象；
- 逐路径记 `{"kept": 90, "total": 361}` 的 note —— **模型是知道自己只看到了 90 条的**；
- 实在压不下去时，返回一个显式信封：`_truncated: true` ＋ 一句
  "这是预览不是全部，请缩小查询范围，不要据此下结论"。

也就是说，"把部分当全部"这个具体失败，Ask 模式**已经防住了**。
spill 真正能补的只剩一件事：**知道被切了，但拿不回来**。
这件事的价值比我上一轮说的小得多，而它的代价（见 §1.4）比我上一轮以为的大得多。

---

## 1. 砍掉的四条，各自是被哪个数字杀死的

### 1.1 compaction —— 砍

| 内网实测 | 数字 |
| --- | --- |
| 真实会话数 / 总轮数 | 110 / 124 |
| 最长会话的 user turn 数 | **5**（`HISTORY_MAX_ROUNDS` 是 10） |
| 每轮估算 token | p50 1099 / p95 3341 / max 4297（预算 128000） |
| 每会话轮数分布 | `{1: 104, 2: 2, 3: 1, 4: 2, 5: 1}` |

**110 个会话里 104 个只有一轮。** 历史根本没到会掉的地步 ——
`fit_history` 的丢弃分支在真实流量里几乎不触发。

**而且 agent 模式已经有 compaction 了**：内网回报里
`AGENT_COMPACTION_ENABLED=true`，阈值是 history 用量 80% 或至少 1 轮 dropped。
再从 Harness 抄一套是重复建设。

**重新提出的条件**：等 §2.2 的预算台账落盘之后，
真实的 `dropped > 0` 比例连续超过某个阈值（建议 10%），或者出现单会话 > 15 轮的用法。

### 1.2 重复调用打断器 —— 砍

| 内网实测 | 数字 |
| --- | --- |
| 已持久化的 turn / 工具调用 | 124 / **1086** |
| 一个 turn 内同名同参的**最长连续段** | **1** |
| 连续 ≥3 次的 turn 占比 | **0 / 124** |
| fail-closed 拒绝后原样重发 | 0 次（"没观察到"，不是"不可能"） |

1086 次调用里一次重复都没有。而且内网指出了结构性原因：
agent 模式的 `state.tool_cache` 对同签名调用直接返回 cached，
**根本走不到第二次 dispatch**（`incident_investigate` 被刻意排除在 cache 外，
但样本里也没观察到它重跑）。

也就是说，我们已经有了一个比 Harness 那个提醒器更强的机制 —— 它是拦截，不是劝导。

**重新提出的条件**：`incident_investigate`（唯一不进 cache 的工具）
被观察到同参连跑 ≥3 次。

### 1.3 通用 per-tool 超时框架 —— 砍，缩成一条待触发的规则

| 内网实测 | 数字 |
| --- | --- |
| `log.read` | n=21，p50 682ms / p95 1303ms / max 3406ms |
| `log.list_apps` | n=1，3254ms |
| `log.search_files` | n=1，300ms |
| 超过 10 秒的操作 | **0 个** |
| `tools.dispatch()` | 同步阻塞 |
| 可复用的通用超时机制 | 没有（只有 `mcp_client` 的传输层和 provider 的 HTTP timeout） |

没有慢工具，而且 `dispatch()` 是同步阻塞的 ——
在 Python 里做"协作式超时"需要引入线程，
**为一个还没发生的问题引入并发，代价远大于收益。**

保留成一条规则写进文档，不写代码：
**将来某个工具的实测 p95 超过 10 秒时，只给那个工具加一个覆盖值，
不做通用框架。**

### 1.4 spill —— 砍（当前形态），有两个硬阻塞

不是"不好"，是**在这台机器上做不成、也没被授权做**。

**阻塞一：Windows 上没有 POSIX 私有权限语义。**

内网按 RUNBOOK 跑的探测直接失败：

```text
PermissionError: [Errno 13] Permission denied: ...\webapp_data\spill_probe_...\probe.txt
PermissionError: [WinError 5] Access is denied: ...\webapp_data\spill_probe_...
```

根目录的对照组倒是成功了，但结果更糟：

```text
exclusive-create OK
mode: 0o100666            <-- 不是 0600
same_machine_nonowner_read_allow_ace=True
```

上游那套"私有 0700 目录 ＋ `0o600` 独占创建"是 spill 隐私性的**全部依据**。
在这台机器上，模式位是假的，而且 ACL 里存在**同机非 owner 可读**的 allow ACE。
照搬只会得到一个**看起来私密、实际不是**的目录 —— 这比不做更危险。

**阻塞二：没有"工具结果全文落盘"的业主授权。**
现有的 raw retention 明确是 UAT 开关且当前关闭。内网的结论是：
授权之前，新 spill 必须按 fail-closed 处理。

**再加一条：原本指望的"一半是现成的"也没有。**
本机没有 `webapp_data/agent_turns/`；checkpoint store 里存的是
经 `_bounded_payload()`（`max_tokens=3500`、`string_cap=2000`）**脱敏且有界**的结果，
不是原文。spill 要从零做。

**重新提出的条件**（三条**全部**满足）：
① 业主批准工具结果全文留存，并给出保留期；
② 先完成 Windows ACL 最小权限设计并实测同机非 owner **读不到**；
③ §2.2 的台账显示截断确实在造成答案缺陷。

> **顺带记一条给未来的自己**：`incident_investigator._finish()` 是先
> `redaction.sanitize_packet()`、再 alarm fingerprint、最后才 yield ——
> 所以 `dispatch_events()` 交出来的那份**已经过闸**。这对将来任何"把工具结果写到别处"
> 的设计都是好消息。但这是从内网的文字描述读出来的，
> **将来真要动手时必须先写一条断言把它钉死**，不能靠这段话。

---

## 2. 留下来的：三条 ＋ 一条可选

### 2.1 【P1】`ask_user_question` —— 唯一一条真能力，也是证据最强的一条

**它防的是哪类已经发生过的缺陷**：不知道的时候模型**编一个**而不是停下来问。
我们被内网抓过的错，一多半是这个形状 ——
示例 JSON 里编了值、给字段安了含义（`55` 说成"存不下引用"实为"扫过且干净"）、
`hkl` 读成 `hk1`。而这些**恰恰都是业主一句话能定的**
（`send_mode=0` 至今 903 行 pending、244 行报不报异常、alias 表）。

**新证据**：内网跑了两个合成的"未知语义"用例，用真 provider，
结果是 **0/2** —— 既没承认未知、也没索取证据、也没做显式假设。
样本很小（内网自己也这么说了），但方向明确：
**现在 prompt 里那句"先给假设再澄清"不能当作"模型会老实澄清"的依据。**

**形态：`pause → 下一个 HTTP turn resume`，不是同一连接阻塞。**
内网确认：当前 SSE 不能在一个 turn 里挂起等浏览器输入；
但 `done` 带结构化 `pending_question`、下一轮用 `resume_from_run_id / session` 绑回答，
**能做，且贴合现有的 checkpoint ＋ pause/resume 架构**。agent 模式已经能在安全点
checkpoint / pause / 之后 resume。owner 隔离（session / checkpoint / approval / credential）
已经是 fail-closed 的，问题只对发起的那个 browser 可见。

**挂在哪**（外网的建议，内网可改）：

| 位置 | 改什么 |
| --- | --- |
| `webapp/tools.py` | 新增第 18 个模型可见工具的 schema ＋ 一个 dispatch 分支 |
| Ask 循环 / agent 循环 | 收到该工具调用 → 不继续迭代，走"带问题结束"的路径 |
| `done` 事件 | 增加 `pending_question` 字段（结构化，见下） |
| checkpoint / session | 记 pause reason = 等人回答（**不许伪装成 completed** —— 内网明确提的） |
| `webapp/static/app.js` | 渲染选项卡片；回答作为下一轮输入并带上绑定 id |

**问题的形状**（照抄上游，因为它已经被验证过好用）：
一次可以问多个问题；每个带**稳定 id**（答案原样回传）、问题文本、可选短标题、
可选选项列表（`label` ＋ `description`）、多选开关；**推荐项放第一个，label 后加 `(Recommended)`**。
答案固定形状 `{"answers":[{"id","selected":[...],"custom"}]}`，
`custom` 在多选时是补充、单选时是覆盖。

**开关**：一个，`SDLC_ASK_USER_QUESTION`，**默认关**。

**不做什么**：
- 不做同一连接内阻塞等待；
- 不做多轮追问链（一次 pause 只问一批）；
- 不把它接进任何自动化/定时路径（只有真人在的会话才允许提问）；
- 不因为它新增任何读权限 —— 这个工具**不读任何东西**。

🔴 **需要内网/业主先确认的一件事**：内网回报"新增模型可见工具 = 有条件，
须由安全/业主确认**模型可读范围与暂存边界**"。
那条限制是针对 spill 的（它要新增读能力）。`ask_user_question` **没有读能力、
不碰任何数据源**，所以外网认为它不落在同一条限制里 —— **但这要你们确认**。
另外要确认一个预算问题：agent 模式有 `AGENT_MAX_TOOLS_PER_TASK=4` 和
**3000-token 的 schema 预算**，第 18 个工具的 schema 塞不塞得下。

### 2.2 【P2】把 `Budget.report()` 落进每轮记录 —— 让被砍的三条以后能用数据重议

**这不是 Harness 的借鉴，是这次回报暴露出来的一个洞。**

内网这句话是整份回报里最重要的一句：

> `Budget.report()` 没有跨 chat session 持久化，故无法给出真实的 `dropped > 0`
> 比例或分布；**不能编造。**

他们做对了（拒绝编数字）。但结果是：**"要不要 compaction"这个问题，
我们现在只能靠会话轮数去侧面推断，而不能直接回答。**
而这正是我们自己的教训 ——「他们的复盘推翻了我的根因，而本该显示真相的台账从来没被写过」。

**做什么**：每轮把 `budget.report()`（以及 agent 侧对应的用量）作为**计数**
写进已有的 turn 记录里。要的是数字，不是内容：
各 lane 的分配/占用、dropped 轮数、有没有触发 `_truncated` 信封、
触发的是哪个工具（工具名可以，参数不要）。

**为什么值得单独做**：它是 §1.1 / §1.2 / §1.4 **三条被砍项的重新提出条件**的前提。
没有它，半年后这三个问题会以一模一样的形式再问一次，
而我们又只能再发一份 RUNBOOK 去测。

**开关**：复用现有的持久化开关，不新增。
**不做什么**：不记任何工具结果内容、不记参数值、不改 `Budget` 的行为（只读它的报告）。

### 2.3 【P3】「模型眼里长什么样」文档段 —— 内网已答"部分接"

内网的口径（照他们的话）：
**对本轮新增的模型可见工具、出口闸门和控制事件**，接纳固定三小节；
**不扩展成一次性补齐 45 个私有 module 的文档工程。**

外网完全同意这个范围。所以本轮实际适用面很小：
只有 §2.1 的 `ask_user_question` 一个新工具 ＋ 它的 `pending_question` 事件。

三小节固定为：
1. **模型看到的原文** —— 是那段文本本身，不是对它的描述；分场景写；
2. **token 影响** —— 固定成本？数据相关？等人的时候花不花 token？
3. **前缀影响** —— 变了会不会打破可复用的请求前缀。

**不做什么**：不回溯改造已有 17 个工具的文档；不新建每模块 README。

### 2.4 【P4】每模块一条运行时不变量 —— 内网已答"部分接"

内网口径：**对本轮新增的 borrowing module**，要求可运行的 invariant，
或以 `No runtime invariant:` 写明**针对性**原因并让 verify 检查；
**不追溯改造所有旧 module。**

一个本轮特有的约束：**内网没有 CI**（`ci_workflow_files: []`），
所以 verify **必须挂在 pytest 里**，作为一个测试用例跑，不能是一个要人记得手动跑的脚本。

上游那条边界值得照抄：**检查只能断言"事件流或可变数据的关系"，
不能断言"某个服务/方法存在"** —— 后者是类型/导入的事，写成不变量是凑数。

**不做什么**：不给 45 个既有 module 补；不引入任何注册表框架
（本轮只有一两个新模块，一个函数 ＋ 一个 pytest 用例就够）。

### 2.5 【P5，可选，随时可砍】长列表的 offset

**这条不是 Harness 借鉴，是 §0 里 spill 那个目标剩下的残渣。**

`shrink_tool_result` 已经告诉模型"给了你 90 条，总共 361 条"，
但多数工具**没有办法拿第 91–361 条** —— 信封里那句"缩小查询范围"
在"我就是要全部列出来"这种问题上没有出路。

我们自己代码里已经有对的样板：`retriever/usecase_catalog.py` 和
`retriever/usecase_consistency.py` 都是 `offset` ＋ `returned` ＋ `truncated` 的分页；
而 `webapp/tools.py` 里 `_arch_impact` / 依赖块是 `items[:40]` / `[:60]` ＋ 一个 bool，
**没有 offset**。所以这条是"把已有模式铺到剩下几个工具"，不是新设计。

**为什么标成可选**：证据薄。内网 checkpoint 样本里
`serialized_result_chars` 是 p50 1615 / max 10500，
6 条 tool_cache 里只有 1 条带截断标记。**这个量级不足以证明它在造成实际的错答案。**
等 §2.2 的台账跑一段时间，用真实的截断频次再决定。

**不做什么**：不改 `shrink_tool_result` 的行为；不加游标状态（纯 offset，无服务端状态）。

---

## 3. 阶段

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| **阶段 0** | §2.2 预算台账落盘 | 无。**先做这个** —— 它最小，且是后面所有重议的前提 |
| **阶段 1** | §2.1 `ask_user_question`（工具 ＋ `pending_question` ＋ pause reason ＋ 前端卡片） | 需要先确认 §2.1 那两个问题（安全口径、schema 预算） |
| **阶段 2** | §2.3 ＋ §2.4，只覆盖阶段 1 新增的面 | 依赖阶段 1 落地 |
| **待触发** | §2.5、以及 §1 里四条各自的"重新提出的条件" | 依赖阶段 0 的数据积累 |

阶段 0 和阶段 1 之间没有代码依赖，如果 §2.1 的确认要等业主，阶段 0 可以先跑。

---

## 4. 需要内网/业主先答的三件事（阻塞阶段 1，不阻塞阶段 0）

1. **`ask_user_question` 是否落在"新增模型可见工具需安全/业主确认"那条限制里？**
   外网的判断：不落在 —— 它不读任何数据源、不新增任何读权限、
   只把一个问题交给已经在会话里的那个人。**但这是你们的判断，不是我们的。**
2. **agent 模式的 3000-token schema 预算，塞得下第 18 个工具吗？**
   塞不下的话，是提预算，还是把这个工具只对 Ask 模式开放（agent 模式用别的方式暂停）。
3. **agent 模式的 `_bounded_payload()` 有没有带 `kept` / `total` 这类 note？**
   Ask 模式的 `shrink_tool_result` 有（见 §0）。如果 agent 侧没有，
   那"模型知不知道自己只看到了一部分"这件事**两个模式不一致** ——
   这比 §2.5 那条 offset 重要得多，会被提到阶段 1。

---

## 5. 这一轮确定下来、以后不用再测的事实

- **`copilot_responses` 严格要求 tool-call/result 成对**：把 `tool_result.content`
  换成短文本 → OK；**整条删掉 → HTTP 400**（`No tool output found for function call`）。
  **将来任何上下文剪枝，只许替换内容，不许删除条目。**
- 注册的 provider 包有三个（`copilot_responses` / `github_copilot_direct` / `openai_chat`），
  本机真实路由并跑通的是 `copilot_responses`。
- Ask 与 agent **共用同一对工具入口**（`tools.dispatch` / `tools.dispatch_events`），
  但**工具结果的消费路径不同**：Ask 走 `fit_tool_result`，
  agent 走 `_bounded_payload()` ＋ `context_pack.build_fitted()`。
  → 任何要作用于"工具结果"的改动，挂在 dispatch 层能覆盖两边，挂在预算层不能。
- 循环上限：Ask `MAX_TOOL_ITERS=8`；agent `AGENT_MAX_MODEL_CALLS=24`、
  `DEEP_MAX_REPLANS=5`、`DEEP_MAX_TOOL_CALLS=12`、`AGENT_MAX_TOOLS_PER_TASK=4`。
- 模型可见工具现在是 **17** 个（不是当年合并后的 13）。
- checkpoint TTL 24 小时；`webapp_data/` 当前进程可写；
  server 是单个 `ThreadingHTTPServer` 进程 ＋ 多请求线程，**没有多实例协调层**
  （将来真部署成多实例，任何依赖本地文件的设计都要重新验）。
- 全量已持久化：122 个 answer / 505 次模型调用 / 6,489,564 token
  （约 4.14 calls、53k token 每个 answer）。**没有找到配额上限的本地记录。**
- 这台 Windows 机器上 **POSIX 模式位没有隐私语义**（`0o100666`），
  且存在同机非 owner 可读的 allow ACE。
  **任何"写到本地磁盘就算私密"的设计，前提都不成立。**

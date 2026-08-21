# 工具调用可点击 trace —— 外网已实现版本（照这份改）

> 业主原话（2026-08-21）：**「升级 ask/agent mode 之后，tools 的调用变得很难 trace」**，
> 要的是：**前端的 tools 记录可以点，点开看到这次调用的输入和输出（包括报错；参数校验失败的话是哪个参数）。**
>
> 这一份**不是**方向建议 —— 外网已经在本仓库把它写完并跑通了（`webapp/` + `webapp/static/`，
> 1544 个测试通过，含 37 个新测试）。内网 codex 按这份 + 代码本身来调整。
>
> 三条老规矩仍然适用：
> 1. **对不上先回报，不要顺手适配。** 这份文档里凡是写成问句的，都不是外网的结论。
> 2. **原样回传**字段名 / 枚举值 / 错误文本模板。
> 3. 🔴 不要把真实 alarm name / app 名 / 未脱敏 payload 贴进回报。

---

## 0. 这次修的到底是什么

一句话：**「失败被记下来了，但没记下它是什么」** —— 同一个缺陷出现在两个地方。

| 地方 | 升级前的样子 | 后果 |
| --- | --- | --- |
| 浏览器 | chip 上只有**工具名** | 「调成功了」「参数写错被拦下」「压根没跑」三种结果**长得一模一样** |
| 模型 | `{"error": "'repo'"}`（裸 `str(KeyError)`） | 模型只知道有个 `'repo'`，不知道是缺参、类型错，还是我们自己挂了 |
| 模型（更糟的一种） | tool-call 的 JSON 坏了 → `args = {}` → 照常 dispatch → 报「缺字段」 | 🔴 **模型把那个字段又原样发一遍**（它本来就没错），一个死循环白烧整轮预算 |

第三行是本次最值得抄的一条：**JSON 坏掉和字段缺失是两件事，混成一件会造出一个自己解不开的死锁。**

---

## 1. 一次调用一条账（record 契约）

`webapp/tool_trace.py::record()` / `finish()` 产出，**同一个对象**同时：

- 进 `tool_start` / `tool_end` 事件（前端实时画）
- 进 `done` 事件的 `tool_trace`（前端最终重画）
- 进 session store（刷新页面后原样重放）

🔴 **只有这一份。前端面板显示的输出，就是当轮 `role:"tool"` 那条消息的字符串本身**，
不是拿 result 再渲染一次。理由见 RUNBOOK-85 §1.5：截图二左右两半互相矛盾，
根因就是**两个面板读的不是同一本账**。

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `tool` | 工具名 | 位置和名字都没动（`evals/run.py` 读的就是它） |
| `call_id` | provider 给的 tool-call id | |
| `seq` | 本轮第几次调用（1 起） | 前端 chip 靠它把 running 换成完成态 |
| `iteration` | 第几个工具轮 | |
| `attempt` | **这个工具**本轮第几次被调 | 改完参数重试时，肉眼可见是"第 2 次"而不是重复 |
| `lane` | `tools` / `subagent` | 预算 lane。**被拦下的调用一律记 `tools`**（什么都没跑，不许花 subagent 额度） |
| `status` | `running` / `ok` / `error` | |
| `failure_class` | §2 的封闭枚举，或 `null` | |
| `who_can_close` | `assistant` / `peer` / `config_owner` / `us` | 🔴 **没有 `user`**，见 §3 |
| `dispatched` | **工具到底跑了没有** | 见 §4 —— 这一条直接对应截图二那个 bug |
| `args` | 解析后的参数 | |
| `arguments_raw` | 模型原样发出的 arguments 串 | **只有 JSON 解析失败时才有**（此时 `args` 没有意义） |
| `invalid[]` | `{field, problem, expected, actual_type}` | 致命：**没有 dispatch** |
| `notes[]` | 同结构 | 非致命：**照常 dispatch 了**，只是记一笔 |
| `message` | 工具自己说的那句话 | 工具没说就是**空字符串** —— 不编一句"工具报了失败"（它可能根本没跑） |
| `duration_ms` | | |
| `output.text` | **喂给模型的那一段**（截断至 `SDLC_TRACE_OUTPUT_CHARS`，默认 4000） | |
| `output.model_chars` | 模型实际收到多少字符 | |
| `output.result_chars` | 工具**原始**结果多少字符；序列化不了就是 `null` | 🔴 `null` 走 §5 的 unknown 三层 |
| `output.shown_chars` | 面板里这次显示了多少 | |

面板底下那行就是这三个数：
`工具返回 24,310 字符 → 模型收到 12,240 字符（上下文预算按结构裁剪）→ 这里显示 4,000 字符`。
**少了这一行，一个被砍过的结果读起来和完整结果一模一样。**

---

## 2. `failure_class` —— 封闭枚举，确定性代码赋值

枚举本身原样沿用 `ask-fast-retry-plan-zh.md` §2.2 —— **没加新值，也没改语义**：

```
bad_call_syntax  bad_arguments  empty_result  unavailable
refused          duplicate      contract_only internal_error
```

外网这一版**自动赋值的只有三个**，其余留给工具自己声明：

| 什么时候 | 赋成 | dispatch 了吗 |
| --- | --- | --- |
| tool-call 的 arguments 不是合法 JSON / 不是 JSON object | `bad_call_syntax` | **否** |
| 工具名不在 `TOOLS` 里 | 由 `dispatch` 自己声明 `bad_arguments` | 是（**故意的**，见下） |
| 必填参数缺失 / 是 `null` / 是空白字符串 | `bad_arguments`（`missing` / `empty`） | **否** |
| schema 要标量却给了 dict/list（反之亦然）；要数字却给了非数字字符串 | `bad_arguments`（`wrong_type`） | **否** |
| dispatch 抛异常 | `internal_error`（带 `error_type`，如 `KeyError`） | 是 |
| 工具结果里**自己声明**了 `failure_class` | 声明值 | 是 |
| 工具结果 `ok is False` 或有 `error`，但没声明 | 🔴 `internal_error` | 是 |
| 声明了一个我们不认识的值 | 🔴 `internal_error`（不信任） | 是 |

🔴 **最后三行是这张表的重点：未能分类的一律 `internal_error`，绝不落 `bad_arguments`。**
落错的代价不是"标签难看"，是**把模型派去改一个根本没错的参数** —— §0 那个死锁的一般形式。

🔴 **工具名不在 `TOOLS` 里，外网故意不在前面拦。**
`dispatch()` 还给 CLI / MCP 路由着十个合并前的旧名字，前面一拦就把一条今天能用的路打断了 ——
为的却是拦一个模型根本没见过的名字。所以让它照常进 `dispatch`，由 `dispatch` 的兜底返回
**自己声明** `bad_arguments`（模型编的名字 → 模型自己改；不许说成"我们内部错误"，
那种说法模型只能以放弃回应）。同时 record 里会留一条 `notes: no_schema`：
**没有 schema 可比对，这次的参数就是没校验过 —— 说出来，不假装检查过。**

工具侧的迁移只改了返回值，没有新依赖（`webapp/tools.py` 里 11 处），
例：`{"ok": False, "failure_class": "bad_arguments", "error": "unknown group: ..."}`。
有一个测试守着漂移：**`tools.py` 里出现的每个 `failure_class` 字面量都必须在枚举里**。

### 2.1 🔴 不许收紧的那一半（no-regression）

只有**「这个值绝不可能成立」**的类型问题才致命。**工具今天已经能容忍的，一律照常 dispatch，只记 note：**

- `limit: "50"`（工具本来就 `int(...)`）→ `notes: loose_type`，**值不动、行为不变**；
- 工具没有的参数名 → `notes: unknown_field`（今天本来就被忽略），面板会告诉人
  "你传了 `limit`，`list_repos` 没有这个参数"。

**不做任何自动纠正/强转。** 收紧这一半 = 把今天能跑的调用变成失败，
而这一轮的验收底线是 `ask-no-regression-bar-and-agent-fixes-zh.md` 说的**"不能更差"**。

---

## 3. 回喂给模型的内容（安全字段白名单）

被拦下的调用不会有结果，模型收到的是**结构化的那一份**：

```json
{"ok": false, "failure_class": "bad_arguments", "attempt": 2,
 "who_can_close": "assistant",
 "invalid_arguments": [{"field": "repo", "problem": "missing",
                        "expected": "string", "actual_type": "absent"}],
 "guidance": "Fix the named argument(s) and call the tool again. ..."}
```

- `field` 是**公开名字**（schema 里就有，模型本来就看得见）；
- `expected` **只来自 schema**。schema 没写就是 `"unknown"`，面板显示 `—`。
  🔴 **编一个期望值，会让模型自信地把参数改成另一个错的** —— 外网在这上面栽过（把 `55` 说成"存不下引用"）；
- `actual_type` 是**形状不是值**：`absent` / `null` / `str(len=17)` / `str(empty)` / `list(len=3)` / `object(keys=2)`。
  参数里可能是业主粘的告警原文、客户标识、生产主机名，**值一律不回喂**；
- `who_can_close` **四个值里没有 `user`**：
  🔴 **用户补不了我们自己的接线 bug**，把 `internal_error` 说成"需要你提供 X"比什么都不说更糟。

### 3.1 ⚠️ 这里外网**故意偏离**了 §2.3，请内网自己裁决

`ask-fast-retry-plan-zh.md` §2.3 写的是"模型永远看不到自由文本的原始 error"。
外网这版**保留了** `internal_error` 的 `detail`（即 `str(e)`），但**先过一遍 `redaction.redact()`**。

理由：老 Ask 的自愈**就是靠这句话**（模型看见 `'I0141x'` 才知道要换 id）。
把它删掉 = 亲手拆掉那条业主唯一夸过的循环。
而 §2.3 的**意图**是禁止**生产数据**外流，不是禁止模型看见它自己刚写的参数。

**内网若要严格执行 §2.3**：请至少保留 `failure_class` + `error_type`，
否则自愈能力会退化 —— 请回报你们的选择，外网这边不替你们定。

---

## 4. `dispatched` —— 这一条直接对应截图二

截图二里屏幕说「Harness 未批准进入 Incident runtime」，而左边写着 `connection_refused`。
**「没跑」和「跑了但失败」是两个事实**，混成一个，用户就被指向错误的那扇门。

所以每条 record 都带 `dispatched`，面板固定渲染三行（失败时）：

```
为什么失败    参数不对
工具跑了吗    没跑 —— 这次调用在发出前就被拦下了
工具自己说的  —
谁能解开      助手自己改参数再调一次（本轮就能修）
```

🔴 **内网的 safe trace / run_steps 面板必须读同一本账。**
`ask-fast-retry-plan-zh.md` §4.2 已经定过：投影**只许复制字段，不许重构语义**。
如果你们的 `Blocked` 步骤现在"既没有输入字段也没有输出字段"，那正是这条 record 要填的位置。

### 4.1 内网侧的映射（**建议，不是断言**）

你们的 `HarnessDecision` 是七值，和这里的 `failure_class` 不是同一层
（一个是"提案要不要放行"，一个是"这次调用发生了什么"）。外网的建议映射：

| HarnessDecision | → `failure_class` | `who_can_close` |
| --- | --- | --- |
| `invalid_proposal` | `bad_arguments` | `assistant` **（回喂，见 PLAN §1.3：这发生在真实动作之前，不放大任何权限）** |
| `internal_contract_conflict` | `internal_error` | `us` 🔴 **绝不显示成需要用户补的 gap** |
| `user_input_required` | —（不是失败，是**问人**） | 你们若要落一个值，请另开一类并告诉外网 |
| `policy_denied` | `refused` | `config_owner` |
| `approval_required` | 你们定 | |

**`user_input_required` 是唯一一个外网没有对应物的** —— 外网这条路上没有"停下来问人"的形态。
这一格请你们自己定，并把结论回报。

---

## 5. unknown 的三层（沿用，别退回）

| 层 | 表示 |
| --- | --- |
| 内部 / JSON | `null` |
| API | 字段**保留**，值为 `null` |
| UI | `—` |

🔴 **禁止 `bool(missing)` / `int(missing or 0)`。**
前端也守住了同一条：升级前存下来的老 record（只有 `tool` + `args`）**不显示成"成功"**，
显示为 **`结果没有记录（升级前的旧记录）`**，chip 是灰的。
**「不知道」被渲染成「没有发生」就是这个 bug 的本体。**

---

## 6. 验收（可直接抄成断言）

外网 `tests/test_tool_trace.py` 里 37 条，最该复制的是这些：

1. 缺必填参数 → **dispatch 没被调用**，`invalid[0].field` 是那个参数名，`actual_type == "absent"`；
2. 同一轮里模型改对参数后**第二次调用成功** → `status` 序列是 `["error", "ok"]`（**自愈没被拆掉**）；
3. arguments 是坏 JSON → `bad_call_syntax`，`invalid` **为空**，回喂内容里**没有** `invalid_arguments`，
   `arguments_raw` 原样留着；
4. dispatch 抛异常 → `internal_error` + `error_type`，**回喂里没有 traceback**，且 `who_can_close == "us"`；
5. 异常消息里带邮箱 → 回喂里**看不到**那个邮箱（过 redact）；
6. 工具声明了不认识的 `failure_class` → 降级成 `internal_error`；
7. `output.text` **就是**模型收到的那条 tool message；结果被裁剪时 `model_chars < result_chars`；
8. 序列化不了的结果 → `result_chars is None`（**不是 0**）；
9. `tool_start` 的 record 是 `running`，`tool_end` 的同 `seq` 且是终态；
10. 被拦下的 `incident_investigate` → `lane == "tools"`（**没花 subagent 额度**）；
11. `"50"` 传给 integer 参数 → **照常 dispatch**，只进 `notes`；
12. 前端资产里 `FAILURE_LABEL` 的键**等于**枚举全集（漏一个就是面板上一个看不懂的英文单词）；
13. 合并前的旧工具名（`consumers` 等）**照旧路由到 handler**，只多一条 `notes: no_schema`。

---

## 7. 外网这一版**没做**的（内网别以为有）

1. **重试额度**（PLAN §3 的 buy-back）没做。外网靠的是原本就有的 `MAX_TOOL_ITERS=8` 外层循环；
   你们那边 `FAST_MAX_REPLANS=0`，**光把错误分类做对不会自动买回一轮** —— 那是 P1 的另一半。
2. `duplicate` / `unavailable` / `refused` / `empty_result` **外网不自动赋值**，只接受工具声明。
   （不猜：把对端 5xx 猜成 `unavailable` 需要知道对端语义，外网这边没有。）
3. `_fallback_tool_calls`（纯文本 provider 那条路）里，**名字不在 `TOOLS` 的调用仍然是被静默丢弃的**，
   丢掉之后这一轮会被当成"模型直接作答"。这是一个**已知缺口**，本次没动，因为改它要动控制流。
   如果你们那边也有这条 fallback 路径，值得单独看一眼。
4. session store 的体积：每次调用最多多存 4000 字符（`SDLC_TRACE_OUTPUT_CHARS`）。
   外网这边 603 个会话 = 172 KB，够用；你们那边如果是数据库或量级更大，**这个默认值请自己定**。

---

## 8. 改到的文件（外网）

```
webapp/tool_trace.py     新增 —— 校验 / 分类 / 账本条目 / 回喂 payload
webapp/agent.py          工具循环：解析→校验→（拦下或 dispatch）→分类→记账；事件带 record
webapp/tools.py          11 处显式错误声明 failure_class（其余不动）
webapp/config.py         SDLC_TRACE_OUTPUT_CHARS
webapp/static/app.js     chip 变按钮 + 详情面板；两个近似重复的渲染函数合成一个
webapp/static/app.css    chip 三态 + 详情面板样式
tests/test_tool_trace.py 37 条
```

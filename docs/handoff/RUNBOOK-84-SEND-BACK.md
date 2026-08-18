# RUNBOOK-84 SEND-BACK

- 日期：2026-08-18
- 已拉取：`3af9601 -> 17eb977`；本次新增 `RUNBOOK-84-harness-borrowings-recon.md`。
- 本次未修改产品代码、配置、索引或业务数据；仅新增本回传文件。
- 脱敏：未记录真实仓库名、告警名、原始日志、payload、人员信息或自由文本。
- 回归：`python -m pytest -q tests -p no:cacheprovider` → `1668 passed in 203.59s`。

## 探针 1

### 命令输出

```json
{
  "ask": {
    "max_replans": 0,
    "max_tool_calls": 3,
    "max_tool_iters": 8
  },
  "agent": {
    "max_model_calls": 24,
    "max_replans": 5,
    "max_resumes": 5,
    "max_tasks": 8,
    "max_tool_calls": 12
  },
  "model_visible_tools": {
    "metadata_count": 17,
    "subagent_tools_count": 1,
    "tools_schema_count": 17
  }
}
```

```json
{
  "checkpoints": {
    "compaction_artifact_count": 0,
    "count": 3,
    "tool_cache": {
      "entries": 6,
      "entries_with_truncation_marker": 1,
      "result_types": {
        "NoneType": 1,
        "dict": 4,
        "list": 1
      }
    },
    "serialized_result_chars": {
      "max": 10500,
      "p50": 1615,
      "p95": 10500
    },
    "tool_trace_field_present_checkpoints": 0
  },
  "sessions": {
    "count": 110,
    "role_counts": {
      "assistant": 124,
      "user": 124
    }
  }
}
```

### 回答

1. **工具事实**：Ask 与 Agent 都经过同一对入口。普通工具走 `tools.dispatch()`；`incident_investigate` 走 `tools.dispatch_events()`。Agent 的对应调用在 `webapp/agent_loop.py`，不是另一套工具执行器。
2. **上下文控制**：两边都复用 `context_budget.Budget()` 和 `fit_history()`。但 Agent 不是真接复用 Ask 的 `fit_tool_result()` 分支；它将模型/结果经过 Agent state、`_bounded_payload()` 与 `context_pack.build_fitted()` 组装。因此是同一预算模块，不同的工具结果消费路径。
3. **模型调用上限**：Ask 的循环上限仍是 `MAX_TOOL_ITERS=8`；Agent 的等待循环上限是 `AGENT_MAX_MODEL_CALLS=24`，并另有 `DEEP_MAX_REPLANS=5`、`DEEP_MAX_TOOL_CALLS=12`。
4. **检查点内容**：本机没有 `webapp_data/agent_turns/`；Agent 检查点在 `webapp_data` 的 checkpoint store，其 `state.tool_cache` 保存的是脱敏且有界的结果，不是原始全文：非 incident 结果会经 `_bounded_payload()`，上限为 `max_tokens=3500`、`string_cap=2000`，JSON 失败时只存 `preview + _truncated`。因此 spill 的可寻址原文不能复用为现成能力。

## 探针 2

### 命令输出

```json
{
  "compaction": {
    "enabled": true,
    "history_usage": 0.8,
    "min_dropped": 1
  },
  "model_visible_tools": {
    "metadata_count": 17,
    "subagent_tools_count": 1,
    "tools_schema_count": 17
  },
  "retention": {
    "checkpoint_ttl_hours": 24
  },
  "storage": {
    "checkpoint_store_exists": true,
    "raw_retention_enabled": false,
    "session_store_exists": true,
    "webapp_data_current_process_writable": true
  },
  "timeouts": {
    "mcp_retry_attempts": 2,
    "mcp_timeout_seconds": 60
  }
}
```

按 runbook 操作在 `webapp_data` 的新子目录测试，首次写入即失败：

```text
Traceback (most recent call last):
  File "<stdin>", line 6, in <module>
PermissionError: [Errno 13] Permission denied: '...\\webapp_data\\spill_probe_...\\probe.txt'

During handling of the above exception, another exception occurred:

PermissionError: [WinError 5] Access is denied: '...\\webapp_data\\spill_probe_...'
```

已删除该空探测目录。根目录的对照独占创建输出如下：

```text
exclusive-create OK
mode: 0o100666 size: 1024
```

```text
same_machine_nonowner_read_allow_ace=True
access_rule_count=7
```

### 回答

#### 2a 可回装性

- 新增 `read_spill / rep_spill`：有条件。当前总工具数为 17；Agent 每 task 已有 `AGENT_MAX_TOOLS_PER_TASK=4` 和 3000-token schema budget，Ask 没有同样的全局 schema 计数上限。新增前须由安全/业主确认模型可读范围与暂存边界。
- `read_file` 增加第二允许：不可以。现有 mirror realpath 边界是正确硬约束，不应为 spill 放宽。

#### 2b 需审许可与位置

- 合规结论：未找到对“所有工具结果全文落盘”的业主授权。现有 raw retention 明确是 UAT 开关且当前为关闭；因此新 spill 在授权前应按“只限 UAT / 未确认即 fail closed”处理。
- 暂存/清理：已确认的 checkpoint TTL 是 24 小时；没有找到可套用到新 spill 的业主暂存/删除规则，需业主决定。
- `webapp_data/`：当前进程可写；chat/session 与 checkpoint 都是本地 JSON 持久文件，重启后仍会重新加载。未进行重启破坏性测试。
- 并发模型：本地 server 是一个 `ThreadingHTTPServer` 进程、多个请求线程；未发现 worker/多实例协调层。部署为多实例时，同一 owner 的后续请求是否落到另一实例尚未验证。

#### 2c 脱敏边界

- 生产 incident 的出口闸门是 `webapp/incident_investigator.py::_finish()`；它调用 `redaction.sanitize_packet()`，再执行 alarm fingerprint，最后才 yield terminal result。所以相对 `tools.dispatch_events()` 的结果返回，二者都在之后。
- `redaction.py` 的 PII 闸门就是 `sanitize_packet()`，同样在之前。
- 未发现除 `incident_investigate` 之外，会从生产 MCP 取原始日志再从 `tools.dispatch` 原样返回的工具。`db_query` 自身也在出口调用 `sanitize_packet()`；`incident_impact` 是本地 artifact 拼装结果，不连生产。这不等于所有 mirror 代码/本地检索结果都适合无脱敏落盘：spill 仍必须只写“模型本来已经看到且已通过通用出口边界”的文本。

#### 2d Windows

- 子目录 probe 的失败和根目录对照成功表明：不能把 POSIX `0700/0600` 当作此 Windows 机器的隐私保证；根目录文件的 mode 实际为 `0o100666`。
- ACL 检查存在同机非 owner 可读的 allow ACE。若做 spill，必须先锁定 Windows ACL 的最小权限设计；不能照搬上游权限位方案。

## 探针 3

### 命令输出

```json
{
  "checkpoints": {
    "compaction_artifact_count": 0,
    "count": 3
  },
  "sessions": {
    "context_loss_keyword_candidates": {
      "assistant_context_loss_candidate": 4,
      "user_context_complaint_candidate": 13
    },
    "count": 110,
    "per_turn_estimated_tokens": {
      "bins": {
        "0-256": 14,
        "1025-4096": 61,
        "257-1024": 46,
        "4097-8192": 3
      },
      "count": 124,
      "max": 4297,
      "p50": 1099,
      "p95": 3341
    },
    "turns": {
      "longest_session_user_turns": 5,
      "per_session_histogram": {
        "1": 104,
        "2": 2,
        "3": 1,
        "4": 2,
        "5": 1
      },
      "total": 124
    }
  }
}
```

真实 provider 的最小调用（未调用生产工具）：

```json
{"provider":"copilot_responses","probe":"minimal_live_provider_call"}
{"result":"OK","message_keys":["_copilot_usage","_usage","content","role"]}
```

成对性 probe：

```json
{"provider":"copilot_responses","probe":"tool_pair_shape"}
{"case":"short_result_replaced","result":"OK","message_keys":["_copilot_usage","_usage","content","role"],"has_tool_calls":false}
{"case":"tool_result_removed","result":"ERROR","type":"RuntimeError","message":"copilot-api HTTP 400: ... No tool output found for function call runbook84_call_11 ... code=invalid_request_body ..."}
```

```json
{
  "agent_usage": {
    "answers": 2,
    "calls": {
      "max": 8,
      "mean": 6.5,
      "p50": 5,
      "p95": 8
    },
    "tokens": {
      "max": 75403,
      "mean": 55662.0,
      "p50": 35921,
      "p95": 75403
    }
  },
  "all_persisted_answers": {
    "answers": 122,
    "model_calls": 505,
    "total_tokens": 6489564
  }
}
```

### 回答

#### 3a 现在是否跨历史

- `Budget.report()` 没有跨 chat session 持久化，故无法给出真实的 `dropped > 0` 比例或分布；不能编造。
- 按直接统计，110 个真实会话共 124 轮，最长仅 5 个 user turn，低于 `HISTORY_MAX_ROUNDS=10`；checkpoint 中也没有 compaction artifact。现有证据不支持把 compaction 列为高优先级。
- 关键词仅能筛出候选，不能替代人工语义判断；在不回传原会话文本的前提下，不能确认存在用户实际因上下文丢失投诉。

#### 3b provider 严格性

- 注册 provider 包：`copilot_responses`、`github_copilot_direct`、`openai_chat`。本机实际成功路由并完成真实调用的是 `copilot_responses`。
- 对该 provider，保留 `tool_call`，把对应 `tool_result.content` 改为短文本：OK。
- 删除对应 `tool_result`，保留 `tool_call`：HTTP 400，错误如上原文。因此 compaction/剪枝不得拆开 tool-call 对。

#### 3c 成本

- 已持久化的 Agent 样本只有 2 个 answer；中位为 5 次模型调用 / 35,921 token；样本太小，不能把它当容量承诺。全量持久化 answer 的均值约为 4.14 calls、53,193 tokens。
- 有 usage 计数，但未找到 Copilot/个人/日/月配额上限及当前可用余额的本地记录。
- 额外总结调用只能在“历史确有压力、且未触及 `max_model_calls`”时接受；当前代码已是 `AGENT_COMPACTION_ENABLED=true`，压力阈值 80% 或至少 1 轮 dropped 才触发，不能改成每轮固定增加一次。

## 探针 4

### 命令输出

```json
{
  "server": "ThreadingHTTPServer",
  "agent_pause_resume": true,
  "checkpoint_owner_scoped": true,
  "current_ask_user_question_tool": false,
  "current_pending_question_field": false
}
```

当前 prompt 的真实 provider 合成未知语义 probe（不含生产工具或业务数据）：

```json
{"provider":"copilot_responses","probe":"synthetic_unknown_semantics","cases":2}
{"case":1,"classification":{"acknowledges_unknown":false,"asks_or_requests_evidence":false,"makes_explicit_assumption":false},"response_chars":78,"usage_total_tokens":6102}
{"case":2,"classification":{"acknowledges_unknown":false,"asks_or_requests_evidence":false,"makes_explicit_assumption":false},"response_chars":157,"usage_total_tokens":6215}
```

### 回答

1. SSE 协议可以增加 `ask` event，但现状没有该 event 或卡片 UI；需要同时改事件协议、`app.js` 渲染、会话/检查点持久化及测试。
2. 不能在当前 HTTP/SSE turn 中挂起、等待浏览器输入、再继续同一个连接；请求输入只在新 HTTP 请求进入时可得。
3. `done` 携带结构化 `pending_question`，下一轮以 `resume_from_run_id / session` 绑定回答：能做，且符合当前 checkpoint + pause/resume 架构；但尚未实现问题 schema。
4. Agent 已能在安全点 checkpoint、pause、之后在新请求中 resume/replan；引入“问人”需把它作为明确 pause reason，不能伪装为 completed。
5. owner 已用于 session、checkpoint、approval 与 credential 的 fail-closed 隔离；按当前代码，问题可以对发起 browser 可见。
6. 未找到可直接运行的 22 条未知枚举/字段含义 eval。两条真实组合成的未知语义 probe 均未表现出承认未知、索取证据或显式假设（粗略 0/2 主动澄清）。这不是 22 条正式评估，但是足以说明现有 prompt 不能作为“模型会老实澄清”的依据。

## 探针 5

### 命令输出

```json
{
  "ge_3_turns": 0,
  "longest": {
    "length": 1,
    "tool": "hubs"
  },
  "runs_by_turn_max_consecutive_identical": {
    "0": 11,
    "1": 113
  },
  "same_call_immediately_after_failed_or_refused": 0,
  "status_counts": {
    "cached": 2,
    "completed": 1084
  },
  "tool_trace_calls": 1086,
  "tool_trace_turns": 124
}
```

```json
{
  "direct_tool_end_timings": {},
  "elapsed_ms_without_safe_operation_name": 0,
  "subagent_operation_timings": {
    "log.list_apps": {
      "max_ms": 3254,
      "n": 1,
      "p50_ms": 3254,
      "p95_ms": 3254
    },
    "log.read": {
      "max_ms": 3406,
      "n": 21,
      "p50_ms": 682,
      "p95_ms": 1303
    },
    "log.search_files": {
      "max_ms": 300,
      "n": 1,
      "p50_ms": 300,
      "p95_ms": 300
    }
  }
}
```

### 回答

#### 5a 循环

- 124 个已持久化 turn、1086 次工具调用中，最长同名同参连续段为 1；`>=3` 为 `0 / 124 turn`。现有证据不支持优先新增重复调用打断器。

#### 5b 合法重试

- `MCP_RETRY_ATTEMPTS=2` 是 `mcp_client.call()` 内部的运输层重试，模型不可见。
- 本机真实 trace 只有 `completed / cached`，没有已记录的 fail-closed 拒绝后原样重试；结论为“没观察到”，不是“永不可能”。
- Agent 重规划理论上可再次发出同一工作；但非 `incident_investigate` 的同签名调用会命中 `state.tool_cache`，返回 cached 而非再次 dispatch。`incident_investigate` 被刻意排除在该 cache 外；当前样本未观察到其同步重跑。
- 未观察到任何同参连续 3 次仍属正常的工具。

#### 5c 每工具超时

- 可回传的持久化样本仅覆盖上表 23 个 subagent operation；未见 `>10s`。它不足以给全部 17 个模型工具建立 p50/p95。
- `tools.dispatch()` 是同步阻塞调用。
- 除 `mcp_client` 的 transport timeout/retry、provider HTTP timeout 与 HTTP server 的请求线程外，未发现可复用的通用 per-tool timeout 执行机制。
- 因此当前证据倾向于：不要普遍用 per-tool timeout 框架；若以后真实 telemetry 显示慢工具，再只对有证据的工具增加覆盖值。

## 探针 6

### 命令输出

```json
{
  "ci_workflow_files": [],
  "docs_specs_count": 48,
  "docs_specs_with_agent_or_tool_in_name": 11,
  "webapp_adjacent_module_docs": [],
  "webapp_python_module_count": 45
}
```

```text
python -m pytest -q tests -p no:cacheprovider
1668 passed in 203.59s (0:03:23)
```

### 回答

- 除本地 pytest 外，当前工作树未发现 CI workflow。
- `webapp/` 有 45 个 Python module，没有相邻的独立 module 文档；现有 48 份 specs 是功能/架构级文档，不是每模块一份。
- **6a：部分接。** 对本轮新增的模型可见工具、出口闸门和控制事件，接纳固定 “Model Experience” 三小节；不将其扩展为一次性补齐 45 个私有 module 的文档工程。
- **6b：部分接。** 对本轮新增 borrowing module，要求可运行 invariant，或以 `No runtime invariant:` 写明针对性原因并让 verify 检查；不在本轮追溯性改造所有旧 module。

## 给外网写 plan 的关键事实

1. spill 没有现成的“原文+locator”可复用；checkpoint 只保存脱敏且有界的 cache。
2. spill 的最大阻塞不是读取接口，而是 Windows ACL/落盘授权：当前目录没有 POSIX 私有权限语义，并且没有“全文工具结果”留存授权。
3. compaction 的真实会话压力证据很弱；历史最长只有 5 turn，正式 dropped telemetry 又未落盘。不要为 Harness 再新增一套 compaction，优先考虑缺损/仅保留现有条件式实现。
4. `copilot_responses` 严格要求 tool-call/result 成对；可把 result 缩成短文本，但不能删除。
5. 当前真实 trace 没有重复调用循环，且可用时延样本均低于 10 秒；重复打断器和通用 per-tool timeout 都没有数据支撑。
6. `ask_user_question` 适合做“pause → 下一 HTTP turn resume”；不适合同一 SSE 连接阻塞等人。

# RUNBOOK-69 — 回应 LogDream 恢复后的 live 验证（P0 语义闸门 + caller policy）

回应 `MCP-LOGDREAM-RECONNECT-EXTERNAL-HANDOFF-20260804.md`。

## 0. 先说最重要的一句

**你们抓的这条是这个功能能出的最严重的缺陷**，而且它不会以报错的形式出现：

> keyword 没命中 → 服务端静默回文件尾部 → 我们把那几行标成"关键词命中 N 行" →
> 模型拿着几行跟事故毫无关系的**真实生产日志**，给出一个自信的、错误的根因。

不是"查不到"，是"查到了错的东西还很确定"。P0 已修，并且**修法不依赖你们**。

另外：`config/mcp_tools.json` 里的 source key 一直写的是 **`hk1`（数字一）**——
RUNBOOK-60 早就告诉过我是 `hkl`（字母 L），我只改了代码里的默认值，**没改这个committed 配置**。
盒子用的是本地 gitignored 配置所以一直没暴露。已改。这是同一类错误的第二次。

---

## 1. P0 已修：`log.read` 语义闸门

新增 `incident_parse.validate_log_read_semantics(out, requested_mode, requested_keyword)`，
investigator 和 MCP console **共用同一个函数**——你们担心的"控制台看得出降级、产品 caller
照旧当命中"这个裂缝，从结构上就没有了。

两道独立检查，**第二道是决定性的**：

| 检查 | 看什么 | 依赖你们吗 |
| --- | --- | --- |
| 1 | `retrieval_method` 字段（字段名可在 config 里改） | 是 |
| 2 | **本地逐行复核关键词是否真的出现在返回里** | **否** |

第二道是我坚持要有的。它不依赖你们的字段名、词表，也不依赖服务端诚实。
就算某天 `retrieval_method` 消失、改名、或者报了个假值，**没有本地确认的行仍然不算命中**。

### 结果分成六档（你们建议的那套）

```
keyword_match       本地确认过，唯一能叫「命中」的
time_context        backtrack 回来的时间上下文，一行都不含关键词
tail_context        尾部内容，不含关键词
no_match            真的搜了，真的没有
semantic_downgrade  我们要 keyword，服务端回了 tail —— 工具契约问题，不是发现
unreadable          返回体我们读不懂 —— 我们的解析问题，不是空日志
```

### 具体行为

- **降级**：0 条 evidence；`not_investigated` 里明写
  `DOWNGRADED the keyword read to tail ... NOT 'no errors in the log'`；
  那几行**一个字都不会进 packet**（有测试断言原文不出现在序列化后的 packet 里）。
- **上下文**：0 条 evidence；写明 `Context is not a hit — this keyword has NOT been confirmed
  present in this file`。
- **混合返回**：只有本地确认的行进 evidence。以前 2 命中 + 3 上下文会报 `lines_seen: 5`，
  现在报 2，异常类也只从那 2 行提。
- **`queries_executed`** 按你们要求拆成两个事实：调用**成功**了（保留），
  以及 `evidence_accepted: false` + `read_outcome` + `retrieval_method`。
- **一个反向情况我特意放行**：如果 tail 回来的行**真的含**关键词，那就是命中。
  被拒绝的是"未经确认的行"，不是"经由意外路径到达的已确认行"。

### 测试

`tests/test_incident_read_semantics.py`，17 项，覆盖你们 §2.5 那 7 条。
其中最该看的是 `test_6_...is_not_reported_as_clean`：它断言降级后 packet 里**不含那几行原文**，
以及 `not_investigated` 里出现 `NOT 'no errors in the log'`。

### 一个副产品：你们的 probe 也暴露了我的测试在编码这个 bug

老的 fixture `DIRTY_LOG` 里**一行都没有 `CPUUtilization`**，而 investigator 照样报"命中 6 行"，
测试还是绿的。修完之后 34 个测试红了——**它们一直在断言这个缺陷**。
fixture 已改成"关键词搜索返回的行确实含关键词"，不含关键词的情况单独一个文件。

---

## 2. P2 已做：caller_policy / semantic_warnings **是闸门，不是文案**

你们那句"这些字段必须由 engine 真正执行，不能只用于 UI 文案"——照做了。

`mcp_registry.build_call(..., caller=)` 新增一道 policy 闸门，在白名单和 deny 之后：

| policy | 产品调查链 | MCP console | 说明 |
| --- | --- | --- | --- |
| `enabled` | ✅ | ✅ | 默认 |
| `manual_only` | ❌ **引擎抛 NotAllowed** | ✅ | 已映射，人工可查，永不进证据链 |
| `disabled` | ❌ | ❌ | 只为让工具名能和 tools/list 对账 |

`caller` 默认是 `product`（严格档）——**忘记传的调用方拿到的是严格答案**，不是宽松的。

已按你们 §5 的结论设成 `manual_only` 的三个：

- `log.investigate` —— candidate_files 是 seed 不是 allow-list（传 1 个读了 4 个），
  keyword 不消费，`lines` 是每文件上限。控制不住文件边界和预算。
- `log.browse` —— 收任意 path，等于绕过 `list_apps → search_files` 那两步验证。
- `aws.parse_alert` —— 会把整段告警文字当 alarm name（RUNBOOK-64）。

实测（stub，`caller='product'` vs `caller='console'`）：三个操作产品侧全部
`NotAllowed`，控制台侧照常返回。

### 顶栏和面板改了语义

不再是一个 `15/15`。现在是三层：

```
接线 15/15  ·  产品已接 11/15  ·  证据可信 10/15
调用开关 开 · 手动调用 开 · 仅人工诊断 3 · 2 条有语义告警
接线 ≠ 可信：接线只证明工具名和参数名对上了，不证明远端遵守参数语义。
```

`log.read` 的行头挂 **语义告警** 徽章（wired + called，但**不算** evidence-safe）；
`log.investigate` / `log.browse` / `aws.parse_alert` 挂 **仅人工诊断**。
手动调用 `log.read` 时结果区多两个徽章：`降级成 tail` 和 `本地确认 0/2 行`。

---

## 3. 我改了 `config/mcp_tools.json` —— 冲突了以你们的为准

你们说本轮已经增量更新过这个文件。我这边也动了它，所以 pull 会冲突。
**这个文件是你们的，冲突取你们那份**，然后只把下面几处补上（如果你们没有）：

| 改动 | 为什么 |
| --- | --- |
| `sources.hk1` → **`hkl`** + 两个 description（你们给的原文） | committed 版一直是错的数字一 |
| `log.list_apps._note` 里 98/93 → **97/92** + 只认 `entry_type=dir` | 你们本轮复核的数 |
| `log.read.semantic_warnings: ["keyword_falls_back_to_tail"]` + `_semantic_note` | 服务端改成"没命中返回空"之后，删掉这条即可 |
| `log.browse` / `log.investigate` / `aws.parse_alert` 加 `caller_policy: "manual_only"` | 见上 |
| `operations._README` 说明这三个新字段 | 都可留空，留空就用代码默认值 |

**不填这些字段代码也是对的**——默认值在 `mcp_registry._CALLER_POLICY` /
`_SEMANTIC_WARNINGS` / `incident_parse.TAIL_METHODS`。配置只用来覆盖。

---

## 4. 请你们做的

### A. 自动测试

```bash
git pull
python -m pytest tests -q
```

期望 **1266 passed**。新增 `tests/test_incident_read_semantics.py`（17）+
`tests/test_mcp_console.py` 的 `CallerPolicyTests` / `ReadSemanticsInTheConsoleTests`。

### B. 真机复验 P0（就是你们 §7 P3 那组探针）

`SDLC_MCP_ENABLED=1`，用**不可能命中的合成 keyword** 对一个已确认文件跑一次日志调查。
期望：

1. `evidence` 为空；
2. `not_investigated` 里出现 `DOWNGRADED ... NOT 'no errors in the log'`；
3. `queries_executed` 里那条 `evidence_accepted: false`、`read_outcome: semantic_downgrade`、
   `retrieval_method: tail`；
4. **那几行 tail 内容不在 packet 里的任何地方**（这条最重要，请 grep 一次确认）；
5. 换一个**真的会命中**的 keyword 再跑一次，确认 evidence 回来了 —— 闸门不能把功能关死。

### C. 面板

点顶栏 → 确认三层数字、`log.read` 的语义告警徽章、三个 `仅人工诊断` 徽章。
手动调一次 `log.read` 用不可能命中的 keyword → 结果区应出现 `降级成 tail` + `本地确认 0/N 行`。

### D. 请回答 / 请转给 MCP owner

1. 你们 §3.4 给 MCP owner 的两个方案（strict 模式 / 改默认），有回音了告诉我 ——
   `log.investigate` 一旦有 `strict_candidate_files` + `strict_keyword` + `global_max_lines`，
   我这边把 `caller_policy` 从 `manual_only` 改成 `enabled` 就是一行配置。
2. `retrieval_method` 除了 `tail` / `alert_time_backtrack`，还有别的取值吗？
   有的话写进 `log.read.request.tail_methods` / `context_methods`（都是列表，可覆盖默认）。
3. keyword 是**大小写敏感**的吗？我本地复核用的是 casefold 子串。
   如果服务端是大小写敏感的精确匹配，我这边会比它宽 —— 宽的方向是安全的（宽只会让
   "本地确认"更容易过，而它必须真的出现在行里才算），但值得对齐。

---

## 5. MDC scope 的措辞已按你们的修正改了

eval case 改名 `scope-mdc-membership-is-business-confirmed` →
**`scope-mdc-membership-is-explicitly-sourced`**，措辞改成：

> 当前 primary MDC scope 是**显式来源的并集**：24 个 `amet-mdc-*` 名称族成员
> （`via=amet-mdc-prefix`）+ 26 个业务表 `mdc_common` 标记成员。
> RUNBOOK-47 那 2 个 graph-adjacent 候选**没有计入**这 50。

断言的是"每个成员说得出是哪个来源把它放进来的"，**数字不断言**（会漂）。
新的 `must_not_mention` 包含「全部经业务确认」「都是业务表确认」——
我第一版就是这么写的，你们指出它把 24 个的出处说错了。

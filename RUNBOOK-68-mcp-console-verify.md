# RUNBOOK-68 — 前端 MCP 面板（目录 + 手动调用）验收

顶栏那颗 `MCP 15/15` 从**只能看**变成**能点**。点开是一个面板：三台服务器、15 个操作、
每条是干什么的、参数在他们那边叫什么、哪些还没填；可以**手动调用一次**，也可以把问题
**交给聊天里的 AI**。

## 为什么这轮的重点是"没有多开一条路"

手动调用在结构上就是**第二条 生产数据 → 浏览器 的路径**，前四轮（RB-61/62/63/64）修的
全是那道闸门。所以它没有新开一条，而是复用同一条：

| 性质 | 怎么保证的 |
| --- | --- |
| 只能调抽象操作名，**永远不能点名工具** | 走 `mcp_client.call` → `mcp_registry.build_call`；`mcp_console` **没有任何参数**能表达一个 tool 名 |
| deny 名单照旧生效 | 同上，一行都没改 |
| **所有**返回一律脱敏 | 不看操作类型。面板上那个「可能含正文 / 元数据」的标记**只决定提示力度，不决定脱不脱** —— 那是我们对你们数据的猜测，猜错只能损失一个提示，不能损失一次脱敏 |
| 原文不进聊天历史 | 原文进 `incident_raw_store`（按会话隔离、有 TTL、有条数上限、可一键 purge），和调查员的「查看原文」是同一个存储。「让 AI 分析这次结果」送进聊天的是**脱敏后**的文本 |
| 地址不外泄 | 目录接口不返回任何 endpoint，只返回「配没配」和「该配哪个环境变量」 |

### 顺带修掉的一个真缺陷

写测试时发现：`mcp_client.TransportError` 的文档写着"消息里永不含 endpoint URL"，
但**握手之后**的 6 个 raise 点全都把 `self.url` 拼进了消息 —— JSON-RPC 错误、SSE 提前关闭、
超过字节上限、回包不是 JSON。那段文本会落进 `not_investigated`，而 `not_investigated`
是**会写进 `chat_sessions.json` 并渲染到浏览器**的。也就是说：一个特意不进 git 的地址，
一直在往截图里跑。已改成只带**服务器名**，并加了回归测试
（`test_no_transport_error_message_carries_the_endpoint`）。

---

## 一、自动测试（不需要连生产）

```bash
git pull
python -m pytest tests -q
```

期望 **1219 passed**。新增 `tests/test_mcp_console.py`（27 项），其中要盯的是否定式那几条：

- `test_metadata_operation_is_redacted_too` —— 分类是猜的，脱敏不能依赖它
- `test_our_endpoint_never_appears_in_a_message_we_compose` —— 上面那个缺陷的回归
- `test_a_url_in_their_response_body_is_left_alone` —— 反过来：**日志正文里的 URL 是证据，不许剥**
- `test_raw_text_goes_to_the_owner_scoped_store_not_the_result`
- `test_empty_argument_is_not_sent_as_an_empty_string` —— 空输入框 = 不发这个参数，不是发空串

---

## 二、盒子上的验收（需要 MCP）

启动方式不变。开关：

```
SDLC_MCP_ENABLED=1        # 照旧，没有它面板只能看不能调
SDLC_MCP_CONSOLE=0        # 可选：只看不给手动调，聊天里的调查员不受影响
```

### A. 目录（`SDLC_MCP_ENABLED` 关着也该能看）

点顶栏 `MCP …` → 面板应列出 **logdream / cloudwatch / portal**，15 个操作，
每条有一句中文说明、状态徽章、和你们配置里的真实工具名。

要确认的两点：
1. **每条操作下面那句话对不对。** 那是我写的「我们为什么调它」，不是你们工具的说明。
   写错了直接在 `config/mcp_tools.json` 里给那条操作加 `"purpose": "…"` 覆盖掉，不用改代码。
2. **`hk1/hkp3` 的 `description` 至今是 `"?"`**（`servers.logdream.sources`）。
   面板会照实显示。哪类日志在哪个 source —— 这个只有你们能填。

### B. 实时核对（点面板右上角「实时核对」）

会对每台 enabled 的服务器调一次 `tools/list`，然后：
- 报告我们声明的工具名里**哪些他们那边不存在**（改名 / 抄的是旧 list）
- 报告他们有、我们没接的
- 把**他们自己写的 description** 合并到每条操作下面，标注「他们写的，原样转载」

⚠️ 这一步会开到生产系统的连接。

> 他们的 description 是**外部文本**：只渲染、转义、标来源，**不会进模型 prompt**。

### C. 手动调一次（先挑一个最安全的）

`log.list_apps` → 填 `source` → 「调用」。期望看到：

- 徽章：服务器名 / 工具名 / 耗时 / 脱敏计数
- **返回结构**：一棵只有字段名和类型的树，比如
  `{"entries": {"list[97] of dict": {"name": "str(len=31)", "entry_type": "str(len=3)"}}}`
- **解析结果**：我们按 `config` 里声明的 `response` 形状解出来多少条

**这一行就是 RB-61 那个缺陷的自助诊断**：如果「返回结构」有 97 条而「解析结果」是 0 或者 2，
就是字段名对不上 —— 在 `operations.<op>.response` 里改一行，再点一次「调用」，立刻知道对没对。
不用再跑一整轮调查去读 refusals。

### D. 逐个把 `"?"` 填掉

对每条 `partial` / `unwired` 的操作：面板会把**还没填的参数**显示成灰的、不可输入，并写明
`参数名未填（"?"）`。填进 `config/mcp_tools.json` → 刷新页面 → 那个框就能用了。
填一个能用一个，不用一次填完。

### E. 原文点击穿透（只在 `SDLC_INCIDENT_RAW_LOGS=1` 时）

调完之后结果区会多一个「查看原文」，打开红色抽屉显示**未脱敏**原文。
关掉那个环境变量时这个按钮不出现，也没有任何接口能取回原文 —— 请确认这一点。

### F. 自然语言

- 每条操作下面的「让 AI 用这条去查」：把这条操作的**用途**和你填的参数值组成一个问题发到聊天，
  由 agent 自己决定走哪条路（它通过隔离的调查员访问这几台服务器，不是逐个操作直连）
- 面板底部的自由输入框：直接把问题发到聊天
- 手动调用之后的「让 AI 分析这次结果」：把**脱敏后**的返回贴进聊天让模型解读

请确认最后一条送进聊天的是 `<email:xxxxxx>` 这类标记，**不是原值**。

---

## 三、请回填 / 请判断

1. `servers.logdream.sources.hk1|hkp3` 的 `description`（至今是 `"?"`）
2. 面板里每条操作那句中文说明，哪几条不准 —— 用 `purpose` 覆盖，或者告诉我改代码里的默认值
3. `SDLC_MCP_CONSOLE` 默认是**开**（前提是 `SDLC_MCP_ENABLED` 已开）。理由是它不给出
   聊天路径没有的能力 —— 调查员调的是同一批只读操作 —— 而一个没人能打开的控制台没人会验。
   如果你们认为默认应该是关，告诉我，改一个字符的事。
4. `/api/mcp/call` 是 POST，和现有的 `/api/chat/stream` 等同样**没有 CSRF token**（内网 loopback
   应用的既有姿态）。如果这次要收紧，那是全站一起收，不是单给这一个接口加。

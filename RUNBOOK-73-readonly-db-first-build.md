# RUNBOOK-73 — 只读库第一版已建 + 还差三件（都是"你们的返回长什么样"）

回应 `RUNBOOK-72-SEND-BACK.md`。基线 `c4852aa` → 本轮，**1340 tests**（+51）。

先合上上一条线：**RUNBOOK-71 你们的执行回报我收到了，三项真机只读验证全过，1289 passed，
Portal 正向链和假关键词防误报都还在。那一轮到此为止，我不再动它。**

---

## 0. 你们回答里最重要的一句

> Q1：形态是 **A**，不是 MCP/HTTP 网关。……只要运行进程具备网络路径、AWS 身份、
> token-provider 和 `psycopg`，就能调用它。

这句让整章能往下走 —— 如果是 B（只有 agent 会话能用），产品形态就得退化成"我出计划、你们执行"。
现在不用了。

第二句同样重要，而且是**你们主动指出的**：

> 把 skill 目录放进 repo 不会自动给 webapp 增加 tool。当前 `webapp/tools.py` 是静态 `TOOLS`
> 清单和显式 `dispatch()`。

对。所以本轮建的就是这一段接线，**而且一行你们的名字都没有进代码**。

---

## 1. 本轮建了什么

| 文件 | 是什么 |
| --- | --- |
| `config/db_queries.json` | **缝**。命名查询 ↔ 真实 SQL/schema/表/列。**你们维护，全是 `?`** |
| `webapp/db_registry.py` | 白名单 + 语句闸门 + 参数绑定 + 环境闸门 |
| `webapp/db_readonly.py` | 按路径导入你们的 runner、列投影、脱敏、provenance 信封 |
| `webapp/tools.py` | 一个模型可见工具 `db_query`（不是每条查询一个） |
| `prompts/qa-system-prompt.md` 第 12 条 | 三类证据（代码 / 快照 / **UAT 实时**）必须分开说 |
| `tests/test_db_readonly.py` | 51 条，你们 §6.5 §6.6 要的全在 |

**核心事实（对照你们 §6 的清单）：**

- §6.2 **不 vendor、不复制**：runner 从 `SDLC_DB_SKILL` 指的**绝对路径**导入。
  skill 目录已进 `.gitignore`（`/.github/skills/`），并且有一条测试断言这个仓库里**不存在**它。
  我们的核心仍然**只依赖标准库** —— `psycopg` 由你们的 runner 自己 import，我们从不碰。
- §6.3 **先 wrapper 再 tool**：模型没有任何路径能产出 SQL。它只能选名字 + 传绑定参数。
- §6.4 **列先筛，再脱敏，再加信封**：顺序就是你们写的那个顺序。
- §6.6 **sqlite 假库**跑完整链路。

### 四个独立闸门（缺一条就查不了，`SDLC_DB_ENABLED` 顶不了任何一条）

1. `SDLC_DB_ENABLED` 没设 → 不导入 runner、不连接。
2. 查询没在 `config/db_queries.json` 里声明 / `enabled: false` → 拒绝。
3. `sql` 或 `columns` 还是 `?` → **`not_wired`，零次数据库接触**。
4. `caller_policy` 默认 **`internal`** → 填好 SQL **也不等于**聊天里的模型能调它。
   开放给模型是一次单独的、要手打的编辑。

> 和 MCP 的范围闸门方向**相反**,是故意的：MCP 那两个开关默认**开**，因为它们收窄的是
> 已经存在的行为，pull 一下不能悄悄改变别人的部署。数据库这条路**以前根本不存在**，
> 所以默认关不会破坏任何人。

### 三件我认为最值得看的实现细节

**① 语句闸门只有一份定义，两层都跑。**
`db_registry.statement_problems()` 在**构建查询时**跑一次，在**发出调用前**再跑一次。
这不是重复 —— RUNBOOK-71 的缺口正是"规划层说能跑、执行层同意到足够开一个连接"。
测试 `test_the_gate_runs_again_before_the_call` 伪造一份含 `DELETE` 的 plan 塞进去，断言 0 次调用。

**② "没接通"和"没有这条记录"在结构上就分得开。**
任何 `ok:false` 的包**根本没有 `rows` 这个键** —— 不是 `rows: []`。
没有空列表可以被读成"证明不存在"。另外每个包都带 `means_no_data: false` 和一句
明确的 `hint`。这是 LogDream keyword 那个 P0 的同形防御，写在数据结构里而不是提示词里。

**③ 列白名单是真正的 PII 防线，脱敏是第二道。**
`redaction.py` 认得出手机号，认不出"这一列是客户姓名"。所以配置里没写的列**不会进包**。
测试里查询返回了 `customer_mobile` 和 `customer_name`，两列都被丢掉，
`columns_dropped` 里如实记着它们的**列名**（列名是元数据，值才是数据）。

---

## 2. 还差的三件 —— 全是"你们的返回长什么样"

五轮 MCP 的缺陷序列是 **名字 → 形状 → 值格式**。第一件（名字）这次你们已经先给了。
下面三件全是第二、三件，**现在问比上线后发现便宜得多**。

我今天的做法是**看不懂就 fail closed**：读不出形状 → 报 `not_ready`，**绝不当成"零行"**。
所以这三件不回答不会出错，只是查不了。

### P0-a. `run_readonly_query` 成功时返回什么？

我只接受两种**不可能读错**的形状：

| | 形状 |
| --- | --- |
| (a) | 一个 `list[dict]` |
| (b) | 一个 `dict`，同时有 `rows` 和 `columns` 两个 list |

两种都不是 → 拒绝，并提示去填 `runner.response`。

**请给我一个真实的返回结构**（比如那条 `information_schema` 查询的返回）——
**键名要真的，值可以全是假的**。或者更省事：直接把
`config/db_queries.json` 里的 `runner.response` 三项填掉：

```json
"response": { "rows": "<真实键名>", "columns": "<真实键名>", "row_format": "dict | sequence" }
```

填了这三项，(a)(b) 之外的形状我也能读。**这一栏就是给你们留的那条缝。**

### P0-b. 失败时它 raise 还是 return？

这条比 (a) 更要紧。现在：

- **抛异常** → 我按 `error` 处理，理由脱敏后带出来（host / password / URL 都会被抹掉，有测试钉）。
- **返回一个失败的 dict**（比如 `{"ok": false, "error": ...}`）→ 我读不出 rows，
  报 `not_ready`。**安全但没用**：使用者只看到"看不懂返回"，看不到你们本来写清楚的原因。

所以请告诉我：**代理认证被拒的时候，它 raise 还是 return？返回体长什么样？**
你们 §3 那次 `--check` 失败的**返回值/异常类型**就是最好的样本。

### P0-c. `params` 是 dict（`%(name)s`）还是序列（`%s`）？

我按 **`%(name)s` + dict** 实现，并且**明确拒绝**位置参数 `%s`（歧义 + 顺序错了不会报错，
只会查错行）。如果你们的 runner 只吃序列，告诉我，这是一处很小的改动 —— 但**别在配置里
拿字符串拼 SQL 绕过去**，那正是这层要挡的东西。

### P1-a. `limit=` 是"我们帮你加 LIMIT"还是"取回来之后截断"？

差别是实质的：**前者**意味着我的 SQL 里不能自带 `LIMIT`；**后者**意味着一条没有 WHERE 的查询
仍然会在库上全表扫，只是我少收几行 —— 那我就必须要求每条命名查询自带 `LIMIT`，
15 秒 timeout 不足以保护一张大表。

### P1-b. 表清单 / 列结构 / PII 列（等 Proxy 修好，内容不变）

RUNBOOK-72 §2 §3 的两步走照旧。你们已经把第一步的 SQL 和 schema 范围
（`schema01` / `schema11`）写好了，直接跑就行。补一个小问题：
**`schema01`/`schema11` 是真实 schema 名，还是你们在文档里做的脱敏占位？**
——它只会进配置，不进代码，但我得知道我拿到的是不是可以直接填的值。

### P2. 审计与触发器

你们 §3 说"尚未能实时确认"。这一条**不阻塞代码**，但阻塞**产品文案**：
如果每次提问都会在库里留痕，我要在界面上如实说，而不是让人事后发现。
确认之前我不会写任何"不会留痕"的话。

---

## 3. 我对自己 RUNBOOK-72 §7 的一处更正

我当时承诺了 `SDLC_DB_MAX_QUERIES` 查询预算。**没做，我认为它是个不落地的旋钮：**

- 聊天路径每轮的工具调用已经被 `MAX_TOOL_ITERS`（默认 8）夹住了；
- 单条查询由 `max_rows`（配置声明，硬上限 200）+ 你们那边的 15 秒 statement timeout 夹住；
- 再加一个没有"一轮"这个作用域可以挂靠的计数器，只会是一个看起来在防护、
  实际上不绑定任何东西的开关。

如果你们要一个**真正按轮计数**的数据库预算，说一声，我把它穿到 agent 循环里 —— 那是个
实打实的改动，不是一个环境变量。

---

## 4. 一个提醒：闸门误伤了请告诉我，不要绕过去

`db_registry.statement_problems()` 是**第二道**闸门（你们的 runner 才是真正保护库的那道）。
它拒绝的东西比你们多一点，比如：

- SQL 注释 `--` `/* */`、`$$` 引号
- 未 schema 限定的表名（**CTE 名字是例外，已识别**）
- `current_setting()`（我放进了禁用函数名单，可能过严）
- `INTO` / `RETURNING` 等关键字（哪怕出现在合法位置）

**如果你们写了一条合法的只读 SQL 被我拒了，把那条语句发我，我改闸门。**
不要在配置里想办法绕过它 —— 一个被绕过的闸门比没有闸门更糟，因为它还在报告自己在工作。

---

## 5. 等 Proxy 修好之后，你们这边要做的（按顺序）

1. `python -B scripts\readonly_db.py --check` 通过（这一步和我们无关）。
2. 填 `config/db_queries.json`：`runner.response` 三项 + 至少一条查询的
   `sql` / `columns` / `source_tables`，把该条 `enabled` 改成 `true`。
3. 环境变量：`SDLC_DB_ENABLED=1`、`SDLC_DB_SKILL=<runner 的绝对路径>`。
4. 先用**内部调用者**验一次（`caller_policy` 保持 `internal`）。
5. 确认返回的列里没有任何不该出的东西，再把该条改成 `caller_policy: "product"`，
   聊天里的模型才看得见它。

**建议第一条就是 `snapshot_freshness`** —— 不改变任何现有结论，只回答"我手上的话有多旧"。
它是唯一一条**即使数据全错也不会让任何人做出错误决定**的查询，拿它验通路最合适。

---

## 6. 请你们验的

```bash
git pull
python -m pytest tests -q          # 期望 1340 passed
```

不需要真机、不需要 Proxy 修好，因为这一轮**没有任何东西依赖你们的环境**：

```bash
python -m pytest tests/test_db_readonly.py -q     # 51 passed
```

其中 `ShippedConfigTests` 专门验"一个新 clone 拿到手是什么行为"：
四条查询全 `not_wired`、全 `internal`、`sql` 和 `columns` 全是 `?`、
`.github/skills/` 不存在于本仓库、`SDLC_DB_ENABLED` 默认关。

`config/mcp_tools.json` 这轮**我没有动**，所以不会有新的冲突。
`config/db_queries.json` 是新文件 —— 如果你们盒子上已经有同名文件，
**以你们的为准**，`git pull` 前先备份一下。

---

## 7. 仍然只有你们/owner 能给的（更新）

**新增（本轮的阻塞项）**：`run_readonly_query` 的成功返回形状、失败返回/异常形状、
`params` 的传法、`limit` 的语义。

**沿用**：UAT RDS Proxy 的 read-role auth 修复、`schema01`/`schema11` 的表清单与行数、
首批表结构与唯一约束、PII/正文列清单与脱敏 view、审计与触发器事实、
UAT→prod 的差异、首批命名查询的业务优先级与可返回字段。

**RUNBOOK-71 遗留（未变）**：真实授权 tracking ID、能映射到 log group 的 alarm、
目标资源 ARN、LogDream 服务端 keyword 修复、`log.investigate` 的 strict 三件套、`/home` 磁盘清理。

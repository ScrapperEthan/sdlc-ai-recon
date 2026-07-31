# RUNBOOK-61 —— 三个 blocker 已修 + 返回体形状进配置（外网 2026-07-31）

> 对应内网 `UAT-GATE-AND-DIFF-20260731.md` 里 "Reproduced blockers in the external-owned engine" 三条。
> 全部在 `webapp/incident_investigator.py`（外网所有）。**你要求的四个回归测试都已写并通过（外网 901 tests）。**
>
> 跑之前：`git pull --ff-only origin master`。本轮**没有动** `config/mcp_tools.json`、
> `AGENTS.md`、`mdc_sheet_schema.json` —— 你保留的那三个本地差异不会冲突。
>
> **这份 runbook 里没有任何需要业主拍板的问题。** 上一版里三处"请确认"已经由外网定了（见 §二），
> 剩下的全部改成**你直接跑、直接回报数字**的检查。

---

## 一、三条 blocker 的修法

三条其实是**同一个根因**：他们的返回体是 JSON，外网当日志文本读了。

### 1. `log.read` 把结构化 JSON 当文本切行（2 行报成 11 行）

`_evidence_from_text()` 直接 `raw.splitlines()`，切的是 **JSON 源码的行**。下游全部建立在这个数字上 ——
`lines_seen`、异常类抽取、脱敏摘录、以及开了留存时写进 `incident_raw.json` 的"原文"，全是 JSON 片段。

**改法**：新增 `extract_log_lines()`，规则三条：

- 能解析成 JSON → **只按结构读**，取声明的行字段；
- 解析不出 JSON（纯日志文本）→ 才 `splitlines()`（legacy，保留）；
- 是 JSON 但**没有一个已知字段对得上** → **fail closed**：不产证据、不计数、不留存，
  在 `not_investigated` 里写明"查询本身是成功的，是我们的解析器读不懂这个结构"。

`mcp_client` 本来就带回了 `structuredContent`，现在优先用它。返回体自带行数（`line_count` 之类）时，
作为 `lines_reported_by_server` **并排放在旁边**，不一致给一句说明 ——
不做二选一，因为"他们说 200 我们读到 50"要么是截断要么是字段读错，两种都得说出来。

`_evidence_from_text` 改名 `_evidence_from_lines`，**只收已经解出来的行**，从签名上杜绝再有人把整个返回体丢进来切一遍。

### 2. 缺时区没有 fail-closed

`plan()` 记了 refusal，但 `ok` 只看有没有识别出服务，于是照样往下跑 `list_apps → search_files → read`。
**拒绝理由报出来了，请求也照发了。**

**改法**：`out["ok"] = bool(targets or use_cases) and bool(window)`。
识别出服务是**必要不充分**：真实 read 工具的参数是一个"告警时刻"，没有时区就没有诚实的查询可发。
现在缺时区 → 计划不可运行 → **`mcp_client.call` 零次**。
refusal 文案加了 `BLOCKING:` 前缀，前端标签也区分开（"缺时区" vs "读不出服务"），免得把人指向错误的问题。
`webapp/tools.py` 和 `prompts/qa-system-prompt.md` 同步改成 **hard stop**：模型必须回问时区，**不许描述任何日志内容**。

### 3. `log.list_apps` 用正则切整个 JSON body

`re.split(r"[\s,\[\]\"'{}:]+", text)` —— 你的合成输入切出来是
`README.txt, cslSmsDeli, dir, entries, entry_type, file, name` 七个"应用名"。

**改法**：新增 `extract_app_names()`，规则同上（JSON → 只按结构；非 JSON → legacy 文本；读不懂 → fail closed，
该 source 整个不查）。目录判定按你写的：`entries[*].name` 且 `entries[*].entry_type == "dir"`。

顺带按同一条规则收紧 `log.search_files`：JSON body 不再回落到正则捞 `.log` 子串。

---

## 二、外网已经拍板的三件事（不需要你回答，只需要跑检查验证）

### 决定 1：kind 判定 = **这批 entries 出现过 kind 字段时才强制**，不是无条件

**为什么这么定**：如果某个 source 返回的 entries 根本不带 `entry_type`，无条件强制会把**全部应用**判掉 ——
"一个应用都验证不了" → 一条日志都不查 → 那本身就是一次静默停摆，
正是这个功能最该避免的失败方式。你合成的那份（dir + file 都带 `entry_type`）走的是强制分支，
结果就是只留 `cslSmsDeli`，**和你写的要求逐字一致**。

没带 kind 字段时不是静默放行：`not_investigated` 里会写
"no entry-type field was present, so files could not be told apart from apps"。

**要无条件强制的话不用改代码**，在配置里显式声明 `kind` 即可（§三）。
**检查 3 会直接量出来现在走的是哪个分支。**

### 决定 2：不再向业主索取返回体样本，改成**盒子自己把形状打出来**

新增 `describe_shape()` / `describe_response()`：把返回体变成**只有字段名、类型、长度的骨架**，
**任何值都不出现**（字符串只显示 `str(len=42)`），字段名本身也过一遍脱敏
（万一有人用账号做 key）。整个报告还会再过一次出口闸门。

所以"你们的工具到底返回什么"这个问题，现在**在内网就能回答，不用有人读 JSON，也不用把响应贴出来**。
**检查 2 就是这个。**

### 决定 3：对外统一报 **460** 个仓库

你的映射报告 total 是 **456**，RUNBOOK-50 在盒子上实测的 roster 是 **460**。
外网这边**统一用 460**（有实测依据），不再讨论。
**检查 5 会把差的那 4 个 id 列出来**，好让映射脚本的输入清单更新一下 —— 纯机械对账，不需要谁做判断。

---

## 三、新增的旋钮：返回体字段名交给内网维护 ⭐

**这是这轮真正的修法。** 返回体长什么样是**你们的环境**，所以和参数名走同一个缝：
写在 `config/mcp_tools.json` 里，**代码零改动、不用等外网推**。

在任意 operation 下加一个 `response` 块即可（**可选**；不写就用代码内置的候选字段）：

```jsonc
"log.read": {
  "server": "logdream",
  "tool": "read_logdream_log",
  "args": { "...": "..." },
  "response": {
    "lines":     "data.rows",        // 日志行在哪。支持点号路径，嵌套不用改代码
    "line_text": ["msg", "content"], // 行对象里正文在哪个键（行本身是字符串时不用管）
    "count":     "total_lines"       // 他们自己报的行数（可选）
  }
},
"log.list_apps": {
  "response": {
    "entries":    "entries",      // 应用清单在哪
    "name":       "name",         // 应用名在哪个键
    "kind":       "entry_type",   // 显式声明 = 无条件强制（见决定 1）
    "kind_value": "dir"           // 只认这个值
  }
}
```

三个约定：

- 值可以是**一个字符串**或**一组候选**（按顺序试）；
- 值写 **`null`** = "这个服务器没有这个字段"，该项判定关闭；
- 值可以是**点号路径**（`data.entries`），返回体再包一层也不用改代码。

内置默认值在 `webapp/incident_investigator.py` 的 `RESPONSE_SHAPES`，**只是兜底，配置永远优先**。
真实字段名和默认不一样时，现象是"fail closed + 明确说找了哪些字段名"，不会是编出来的数字 ——
照着报错里那句话填 `response` 即可。

---

## 四、你要求的四个回归测试（已写，`tests/test_incident_investigator.py`）

| 你的要求 | 测试 |
| --- | --- |
| 结构化两行 `log.read` → `lines_seen == 2` | `test_a_structured_two_line_response_counts_two_lines_not_eleven` + 端到端 `test_a_structured_read_reports_the_real_line_count_in_the_packet` |
| 缺时区 → 拒绝且 `mcp_client.call` **零调用** | `test_a_missing_timezone_makes_zero_mcp_calls`（断言 `self.calls == []`，不是"变少"） |
| 结构化 `log.list_apps` 只接受目录名 | `test_structured_list_apps_accepts_only_directory_names`（逐个断言那七个 token 都不在结果里） |
| 畸形/未知 JSON fail closed，不回落到任意 JSON token | `test_an_unreadable_json_body_fails_closed_rather_than_being_split`、`test_an_unreadable_app_listing_yields_no_names_rather_than_json_tokens`、`test_search_files_does_not_fall_back_to_regex_on_a_json_body` |

另外加了：`structuredContent` 优先于 `text`、legacy 字符串列表仍接受、
解析失败**不得**被报成"没查到"、`response` 旋钮可覆盖（含点号路径）、
以及形状探针本身的泄漏测试（值和"当 key 用的账号"都不得出现）。

---

## 五、盒子上请跑的检查

> 检查 1 和 4 是**纯离线**的（不发任何请求）。
> 检查 2、3 会**真实调用 LogDream**（只读，各 1–3 次），所以脚本里**在进程内临时**打开
> `SDLC_MCP_ENABLED`，**不写进环境变量、不改配置**，进程结束就没了。
> PowerShell 下同样有效（是 Python 进程内设的，不是 shell 变量）。

### 检查 1 —— 全量测试（离线）

```bash
python -m pytest tests -q
```

**回报**：通过数。外网这边是 **901**（我最初写的 909 是笔误）。

### 检查 2 ⭐⭐ —— 把三个工具的真实返回体形状打出来（这是本轮最重要的一项）

```bash
python - <<'PY'
import json, os
os.environ["SDLC_MCP_ENABLED"] = "1"          # 仅本进程，不落盘
from webapp import incident_investigator as inv, mcp_client

def show(tag, out, op):
    print("=" * 70)
    print(tag)
    print(json.dumps(inv.describe_response(out, op), ensure_ascii=False, indent=1))

app = file_name = None
for source in inv.log_sources():
    try:
        out = mcp_client.call("log.list_apps", {"source": source})
    except Exception as e:
        print("=" * 70); print("log.list_apps /", source, "-> 调用失败:", e); continue
    show("log.list_apps / source=%s" % source, out, "log.list_apps")
    names, _note, _err = inv.extract_app_names(out.get("text") or "", out.get("structured"))
    if names and app is None:
        app, app_source = names[0], source

if app:
    out = mcp_client.call("log.search_files", {"app": app, "source": app_source})
    show("log.search_files / app=%s source=%s" % (app, app_source), out, "log.search_files")
    picked = inv.select_log_files(out.get("text") or "", structured=out.get("structured"))
    if picked:
        out = mcp_client.call("log.read", {"app": app, "source": app_source, "file": picked[0]})
        show("log.read / file=%s" % picked[0], out, "log.read")
    else:
        print("!! search_files 没解析出文件名，log.read 这一跳没跑")
else:
    print("!! 没有任何 source 解析出应用名，后两跳没跑")
PY
```

**把整段输出原样贴回来即可** —— 里面**不含任何日志内容、不含任何值**，只有字段名、类型、长度，
可以安全外发。

**怎么判读（你也可以直接贴给我，我来判）：**

| 看哪一行 | 说明什么 |
| --- | --- |
| `body_is_json: true` + `shape` 里能看到字段树 | 结构化返回，走的是新路径 ✅ |
| `parsed.lines` / `parsed.apps` 是 `null` | **fail closed 了**，`parsed.error` 里会写"找了哪些字段名" → 照着填 §三 的 `response` 块，再跑一遍 |
| `parsed.apps` 数量接近 RB-55 的 **98 / 93** | entries 判定正常 ✅ |
| `parsed.apps` 骤降到个位数 | `entry_type` 强制判过头了 → 把 `response.kind` 设成 `null` 再跑一次，两次数字都回报 |
| `parsed.note` 里出现 `no entry-type field` | 走的是**非强制**分支（决定 1 的另一半）→ 回报这一句，外网据此决定要不要收紧默认值 |
| `carried_structured_content: true` | 他们同时发了 `structuredContent`，我们优先用它 |

🔴 **如果 `parsed.lines` 是一个明显大于真实日志行数的数字（而不是 `null`）**，
说明结构判定没走到、又在切文本了 —— **立刻停下回报，不要开开关。**

### 检查 3 ⭐ —— 哪些仓库真的能解析出 LogDream 应用名（顺便挑演示用的告警）

```bash
python - <<'PY'
import os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv, mcp_client
from retriever import repo_tags

apps = {}
for source in inv.log_sources():
    try:
        out = mcp_client.call("log.list_apps", {"source": source})
        names, _n, _e = inv.extract_app_names(out.get("text") or "", out.get("structured"))
        apps[source] = set(names or [])
        print("%-8s 应用数 %d" % (source, len(names or [])))
    except Exception as e:
        print("%-8s 取不到: %s" % (source, e))

repos = sorted(repo_tags.load())          # {repo_id: tags}
hit = []
for repo in repos:
    for c in inv.app_candidates(repo):
        on = sorted(s for s, a in apps.items() if c["app"] in a)
        if on:
            hit.append((repo, c["app"], ",".join(on)))
            break
print()
print("能解析出应用名的仓库：%d / %d" % (len(hit), len(repos)))
for repo, app, on in hit[:30]:
    print("  %-45s -> %-25s (%s)" % (repo, app, on))
PY
```

**回报**：前两行的应用数、`能解析出应用名的仓库 N / M`，以及前 30 行里**任意一个** repo 名。

**这一项的用途**：那个 repo 名就是**演示卡片该用的告警**。前端起始页现在那张
「生产日志根因」卡片里写的是 `mc-hk-hase-batch-letter-postman-job`，
如果它不在这份命中清单里，把命中清单里的任意一个告诉我，我换掉 ——
或者你直接改 `webapp/static/index.html` 里那一行 `data-prompt` 也行（就一个仓库名）。

### 检查 4 —— 缺时区必须零调用（离线）

```bash
python - <<'PY'
from unittest import mock
from webapp import config, incident_investigator as inv, mcp_client
calls = []
def _spy(op, args=None, **k):
    calls.append(op)
    raise AssertionError("不应该有任何 MCP 调用")
with mock.patch.object(config, "MCP_ENABLED", True), \
     mock.patch.object(mcp_client, "call", _spy):
    p = inv.investigate("prodECS_<填检查 3 里命中的那个仓库名>_service_CPUUtilizationMINOR[80percent]")
print("MCP 调用次数 :", len(calls))
print("plan.ok      :", p["plan"]["ok"])
print("evidence     :", len(p["evidence"]))
print("拒绝理由     :", (p["not_investigated"] or [""])[0][:160])
PY
```

**期望**：调用次数 `0`、`plan.ok` `False`、evidence `0`、拒绝理由以 `BLOCKING:` 开头。

### 检查 5 —— 456 / 460 对账（离线，机械对比）

```bash
python - <<'PY'
import json, os
from retriever import config as rc, repo_tags
ours = set(repo_tags.load())              # {repo_id: tags}
print("我们的 roster :", len(ours))
path = os.path.join(rc.INDEX_DIR, "reports", "LOGDREAM-APP-MAPPING-CANDIDATES.json")
try:
    doc = json.load(open(path, encoding="utf-8-sig"))
except Exception as e:
    print("映射报告读不到:", e); raise SystemExit
# 结构未知，所以把所有看起来像 repo id 的字符串都收上来
found = set()
def walk(n):
    if isinstance(n, str) and n in ours: found.add(n)
    elif isinstance(n, dict):
        for k, v in n.items(): walk(k); walk(v)
    elif isinstance(n, list):
        for v in n: walk(v)
walk(doc)
print("报告里出现的 :", len(found))
print("报告里缺的   :", sorted(ours - found)[:20])
PY
```

**回报**：三行。`报告里缺的` 那几个就是映射脚本输入清单没跟上的部分。

### 检查 6 —— 正常路径跑一次真实调查（带时区）

检查 1–5 都符合预期后，把 `SDLC_MCP_ENABLED=1` 打开跑一次真实聊天，问一句
「**这个告警的日志说明了什么**」（用检查 3 里命中的仓库名，**并带上显式时区**，如 `at 03:15 HKT`）。

回报：

```
evidence 条数      : ___
lines_seen         : ___（和日志里实际行数对得上吗？）
lines_reported_by_server / line_count_note : ___（有就贴出来）
not_investigated   : ___ 条（逐条贴前 160 字）
前端 sub-agent 面板 : 每步有没有 `logdream · log.read` 徽标 + 耗时
```

**判定**：`not_investigated` 里如果出现 `The query SUCCEEDED` 那句，
说明还有一处返回体形状没对上 → 回到检查 2，按 `parsed.error` 填 `response` 块。

### 检查 7 —— 开关默认仍是关

```bash
python -c "
from webapp import config
print('MCP_ENABLED       :', config.MCP_ENABLED)
print('INCIDENT_RAW_LOGS :', config.INCIDENT_RAW_LOGS)
"
```

**期望**：都是 `False`。**检查 1–6 全绿之前不要持久化打开。**

---

## 六、这一轮的教训（记在这里，免得下次再犯）

四轮下来，**每一个被内网抓到的缺陷，都是我在断言你们环境的某件事**：
`hk1`/`hkl`、`from_time` 参数、少了 `search_files` 那一跳、现在是**返回体的形状**。

参数名早就走配置了，**返回体的形状却还硬编码在 Python 里 —— 同一个错误换了个位置**。

所以这轮不只是修 bug：**返回体形状也进配置了**，而且加了 `describe_shape()`，
让"你们的工具返回什么"这个问题以后**在内网自己就能答**，不用再往外贴响应、也不用外网猜。

外网这边保留的只有一条，而且永远是我的：**读不懂就 fail closed，并说出找了什么。**

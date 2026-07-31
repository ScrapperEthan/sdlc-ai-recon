# RUNBOOK-61 —— 三个 blocker 已修：结构化返回体 + 时区闸门（外网 2026-07-31）

> 对应内网 `UAT-GATE-AND-DIFF-20260731.md` 里 "Reproduced blockers in the external-owned engine" 三条。
> 全部在 `webapp/incident_investigator.py`（外网所有）。**你要求的四个回归测试都已写并通过。**
>
> 跑之前：`git pull --ff-only origin master`。本轮**没有动** `config/mcp_tools.json`、
> `AGENTS.md`、`mdc_sheet_schema.json` —— 你保留的那三个本地差异不会冲突。

---

## 一、三条 blocker 的修法

### 1. `log.read` 把结构化 JSON 当成日志文本切行（2 行报成 11 行）

**根因**：`_evidence_from_text()` 直接 `raw.splitlines()`。返回体是 JSON，切的是 **JSON 源码的行**，
不是日志行。而且下游全部建立在这个数字上 —— `lines_seen`、异常类抽取、脱敏摘录、以及开了留存时
存进 `incident_raw.json` 的"原文"，全是 JSON 片段。

**改法**：新增 `extract_log_lines(text, structured=None)`，规则三条：

- 返回体能解析成 JSON → **只按结构读**，取声明的行字段；
- 解析不出 JSON（纯日志文本）→ 才走 `splitlines()`（legacy，保留）；
- 是 JSON 但**没有一个已知字段**能对上 → **fail closed**：不产证据、不计数、不留存，
  在 `not_investigated` 里写明"查询本身是成功的，是我们的解析器读不懂这个结构"。

`mcp_client` 本来就带回了 `structuredContent`，现在会优先用它，其次才解析 `text`。
返回体如果自带行数（`line_count` 之类），会作为 `lines_reported_by_server` **并排放在旁边**，
和我们数出来的不一致时给一句说明 —— 不做二选一，因为"他们说 200 我们读到 50"要么是截断、
要么是字段读错了，两种都得说出来而不是挑一个数字。

`_evidence_from_text` 改名为 `_evidence_from_lines`，**只收已经解出来的行**，
从签名上杜绝"再有人把整个返回体丢进来切一遍"。

### 2. 缺时区没有 fail-closed

**根因**：`plan()` 记了 refusal，但 `ok` 只看有没有识别出服务，于是照样往下跑
`list_apps → search_files → read`。**拒绝理由被报出来了，请求也照发了** —— 两头都占。

**改法**：`out["ok"] = bool(targets or use_cases) and bool(window)`。
识别出服务是**必要不充分**：真实 read 工具的参数是一个"告警时刻"，没有时区就没有诚实的查询可发。
现在缺时区 → 计划不可运行 → **`mcp_client.call` 零次**，直接返回拒绝包。
refusal 文案加了 `BLOCKING:` 前缀，前端那一步的中文标签也区分开了
（"缺时区…请告知是哪个时区" vs "读不出服务或用例"），免得把用户指向错误的问题。

`webapp/tools.py` 的工具说明和 `prompts/qa-system-prompt.md` 同步改成 **hard stop**：
模型看到 BLOCKING 时区拒绝，必须在聊天里回问时区，**不许描述任何日志内容**。

### 3. `log.list_apps` 用正则切整个 JSON body

**根因**：`re.split(r"[\s,\[\]\"'{}:]+", text)` —— 你的合成输入切出来是
`README.txt, cslSmsDeli, dir, entries, entry_type, file, name` 七个"应用名"。

**改法**：新增 `extract_app_names()`，规则和 1 一致（JSON → 只按结构读；非 JSON → legacy 文本；
读不懂 → fail closed，该 source 整个不查）。目录判定按你写的：`entries[*].name`，
且 `entries[*].entry_type == "dir"`。

**这里有一处我和你的原文不完全一样，请确认**：
kind 判定是**当这批 entries 里出现过 kind 字段时才强制**。
如果某个 source 返回的 entries 根本不带 `entry_type`，强制会把**全部应用**判掉 ——
那本身就是一次静默停摆。这种情况现在是**接受并在 note 里写明"没有 entry-type 字段，
无法区分文件和应用"**。你合成的那份（dir + file 都带 entry_type）走的是强制分支，
结果就是只留 `cslSmsDeli`。若你要的是**无条件强制**，在配置里把 `kind` 显式声明即可（见下）。

顺带按同一条规则收紧了 `log.search_files`：JSON body 不再回落到正则捞 `.log` 子串。

---

## 二、新增的旋钮：返回体字段名交给你维护 ⭐

**这是你说的"把某些文件交给内网维护"那部分。** 返回体长什么样是**你的环境**，
所以和参数名走同一个缝：写在 `config/mcp_tools.json` 里，**代码零改动、不用等外网推**。

在任意 operation 下加一个 `response` 块即可（**可选**；不写就用代码内置的候选字段）：

```jsonc
"log.read": {
  "server": "logdream",
  "tool": "read_logdream_log",
  "args": { "...": "..." },
  "response": {
    "lines":     "data.rows",        // 日志行在哪。支持点号路径，嵌套不用改代码
    "line_text": ["msg", "content"], // 行对象里正文在哪个键（行是字符串时不用管）
    "count":     "total_lines"       // 他们自己报的行数（可选）
  }
},
"log.list_apps": {
  "response": {
    "entries":    "entries",      // 应用清单在哪
    "name":       "name",         // 应用名在哪个键
    "kind":       "entry_type",   // 类型字段；显式声明即为无条件强制
    "kind_value": "dir"           // 只认这个值
  }
}
```

三个约定：

- 值可以是**一个字符串**或**一组候选**（按顺序试）；
- 值写 **`null`** = "这个服务器没有这个字段"，该项判定关闭；
- 值可以是**点号路径**（`data.entries`），所以返回体再包一层也不用改代码。

内置默认值在 `webapp/incident_investigator.py` 的 `RESPONSE_SHAPES`。
**默认值只是兜底，配置永远优先。** 如果真实字段名和默认不一样，
现象会是"fail closed + 明确说找了哪些字段名"，不会是编出来的数字 —— 照着报错里那句话填 `response` 即可。

---

## 三、你要求的四个回归测试（已写，`tests/test_incident_investigator.py`）

| 你的要求 | 测试 |
| --- | --- |
| 结构化两行 `log.read` → `lines_seen == 2` | `test_a_structured_two_line_response_counts_two_lines_not_eleven` + 端到端 `test_a_structured_read_reports_the_real_line_count_in_the_packet` |
| 缺时区 → 拒绝且 `mcp_client.call` **零调用** | `test_a_missing_timezone_makes_zero_mcp_calls`（断言 `self.calls == []`，不是"变少"） |
| 结构化 `log.list_apps` 只接受目录名 | `test_structured_list_apps_accepts_only_directory_names`（逐个断言那七个 token 都不在结果里） |
| 畸形/未知 JSON fail closed，不回落到任意 JSON token | `test_an_unreadable_json_body_fails_closed_rather_than_being_split`、`test_an_unreadable_app_listing_yields_no_names_rather_than_json_tokens`、`test_search_files_does_not_fall_back_to_regex_on_a_json_body` |

另外加了：`structuredContent` 优先于 `text`、legacy 字符串列表仍接受、
解析失败**不得**被报成"没查到"（`test_an_unreadable_read_response_is_not_reported_as_nothing_found`）、
以及 `response` 旋钮本身可覆盖（含点号路径）。

外网全量：**894 passed**（investigator 单文件 202）。

---

## 四、盒子上请跑的验证

### 检查 1 —— 全量测试

```bash
python -m pytest tests -q
```

**回报**：通过数。预期 894（你那边如果 tests 目录有本地新增文件会更多）。

### 检查 2 ⭐ —— 用你的真实返回体喂解析器（不发请求，纯离线）

把你合成或抓到的**真实** `log.read` / `log.list_apps` 返回体存成两个文件，然后：

```bash
python -c "
from webapp import incident_investigator as inv
read_body = open('read_sample.json', encoding='utf-8').read()
apps_body = open('apps_sample.json', encoding='utf-8').read()
lines, reported, err = inv.extract_log_lines(read_body)
print('log.read      lines =', None if lines is None else len(lines), '| server said', reported)
print('              error =', err[:200])
names, note, err2 = inv.extract_app_names(apps_body)
print('log.list_apps apps  =', None if names is None else len(names))
print('              sample=', (names or [])[:5])
print('              note  =', note)
print('              error =', err2[:200])
"
```

**判定**：
- `lines` 必须等于返回体里**真实的日志行数**（不是 JSON 源码行数）；
- `apps` 里**不能出现** JSON 字段名（`entries`/`entry_type`/`name`）或文件名（`README.txt`）；
- 任何一个打出 `None` + 一句 error → **按 error 里那句话在 `config/mcp_tools.json` 加 `response` 块**，
  再跑一遍。这一步不需要外网参与。

🔴 **如果 `lines` 是个大于真实行数的数字（而不是 None），立刻停下回报** ——
说明结构判定没走到，又在切文本了。

### 检查 3 ⭐ —— 缺时区必须零调用

```bash
python -c "
from unittest import mock
from webapp import config, incident_investigator as inv, mcp_client
calls = []
def _spy(op, args=None, **k):
    calls.append(op)
    raise AssertionError('不应该有任何 MCP 调用')
with mock.patch.object(config, 'MCP_ENABLED', True), \
     mock.patch.object(mcp_client, 'call', _spy):
    p = inv.investigate('prodECS_<填一个真实仓库名>_service_CPUUtilizationMINOR[80percent]')
print('MCP 调用次数 :', len(calls))
print('plan.ok      :', p['plan']['ok'])
print('evidence     :', len(p['evidence']))
print('拒绝理由     :', (p['not_investigated'] or [''])[0][:160])
"
```

**期望**：调用次数 `0`、`plan.ok` `False`、evidence `0`、拒绝理由以 `BLOCKING:` 开头。

### 检查 4 —— 正常路径仍然通（带时区）

同一条告警加上 `at 03:15 HKT` 跑一次真实调查（`SDLC_MCP_ENABLED=1`），回报：

```
生效 source        : ___
每个 source 的 app 数 : ___ / ___   ← 和 RB-55 的 98/93 比，差很多说明 entries 判定过严
app 解析结果       : <repo> → <app>
evidence 条数      : ___
lines_seen         : ___（和日志里实际行数对得上吗？）
lines_reported_by_server / line_count_note : ___（有就贴出来）
not_investigated   : ___ 条（逐条贴前 160 字）
```

**判定**：`app 数`应接近 98/93。如果骤降到个位数，是 `entry_type` 强制判掉了 ——
把 `response.kind` 设成 `null` 再跑一次对比，并回报两次的数字。

### 检查 5 —— 开关仍然默认关

```bash
python -c "
from webapp import config
print('MCP_ENABLED       :', config.MCP_ENABLED)
print('INCIDENT_RAW_LOGS :', config.INCIDENT_RAW_LOGS)
"
```

**期望**：都是 `False`。**检查 1–4 全绿之前不要打开。**

---

## 五、还需要你决定 / 提供的

1. **kind 判定要不要无条件强制**（见 §一.3 那段）。你说一句，或者直接在配置里显式声明 `kind`。
2. **repo → LogDream 应用名映射**：你报的 52 单规则命中 / 404 未匹配 / 总 456。
   代码这边的行为没变，也不打算变：**规则推出来的名字永远是候选**，
   只有在服务器自己的 `list_apps` 里出现过才会被查，否则明确拒绝并说明原因。
   要提高覆盖，只能靠 `config/logdream_app_map.json`（或 `logdream_apps.json`，两个名字都认）。
   **不必全量 456** —— 先给告警最多的 20–30 个就够解锁演示。
3. **顺带一个小对数**：你的映射报告 total 是 **456**，RUNBOOK-50 在盒子上实测的 roster 是 **460**。
   差 4 个，可能只是映射脚本的输入清单旧了。麻烦确认一下用的是哪份 roster ——
   对外报数我们统一用 460。
4. **返回体样本**：如果 §四 检查 2 里有任何一项 fail closed 而你不确定该填什么字段，
   把那段返回体（**去掉客户数据，只要结构**）发我，我把默认值补上，
   这样下一个人不用再填一次配置。

---

## 六、这一轮的教训（记在这里，免得下次再犯）

三轮下来，**每一个被内网抓到的缺陷，都是我在断言你们环境的某件事**：
`hk1`/`hkl`、`from_time` 参数、少了 `search_files` 那一跳、现在是**返回体的形状**。
参数名早就走配置了，返回体的形状却还硬编码在 Python 里 —— 同一个错误换了个位置。

所以这轮不只是修 bug：**返回体形状也进配置了**。
外网这边保留的只有"读不懂就 fail closed 并说出找了什么"，这条永远是我的。

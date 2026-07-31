# RUNBOOK-60 —— MCP 客户端首次真机验证(`webapp/mcp_client.py`)

> **背景:** RUNBOOK-58 把**配置层**填完了(12 ready / 1 partial / 2 disabled)。
> 外部现在补上了**传输层**:`webapp/mcp_client.py`,纯标准库,两种传输
> (CloudWatch = Streamable HTTP,LogDream = 旧式 SSE)。
> 本地 28 个测试全过 —— 但**本地测的是我自己写的假服务器**,只能证明我对协议的理解自洽,
> 不能证明和他们的真实实现一致。**这份 runbook 就是去证伪这一点的。**
>
> **只读。** 全程只调查询类工具。`open_portal_login` 和任何执行类工具由代码硬性拒绝(见检查 4)。
> **回报计数、状态、错误原文。不要回报日志正文、告警正文、客户标识、手机号/邮箱/账号。**

先 `git pull`(需要 `webapp/mcp_client.py`,提交在 `b0aee7a` 之后)。

---

## 前置:打开开关

默认是**关**的。关着的时候 `mcp_client` 里任何代码路径都不会开 socket ——
所以现有功能和现有测试完全不受影响(这是验收标准,不是口头承诺)。

```bash
export SDLC_MCP_ENABLED=1          # Windows: $env:SDLC_MCP_ENABLED="1"
```

三道闸门是独立的:这个开关、`mcp_tools.json` 里的 `enabled`、以及 `url_env` 指向的地址。
三个都得成立才会连上真机。**验证完记得把这个开关关掉**,别留在环境里。

---

## 检查 1 ⭐⭐ 工具名核对 —— 唯一真正关键的一项

这一项在查:**RUNBOOK-58 填进配置的工具名,在真机上到底存不存在。**
配置说"我们允许调什么",只有他们的服务器能说"什么是真的存在的"。
这两者对不上,是注册表自己**永远发现不了**的错。

```bash
python -c "
import json
from webapp import mcp_client
out = mcp_client.status(probe_servers=True)
print('calling_enabled :', out['calling_enabled'])
print('by_state        :', out['by_state'])
for name, p in (out.get('probes') or {}).items():
    print('---', name, 'ok=', p['ok'], 'protocol=', p.get('protocol'))
    print('   server_info :', p.get('server_info'))
    print('   declared    :', len(p['declared']), '  live:', len(p['live']))
    print('   MISSING     :', p['missing'])
    print('   missing ops :', p.get('missing_operations'))
    print('   unused(仅供参考):', len(p['unused']))
    if p['reason']: print('   reason      :', p['reason'])
"
```

**要回报:** 上面每一行。判定:

| 项 | 期望 | 对不上说明什么 |
| --- | --- | --- |
| `logdream` / `cloudwatch` 都 `ok=True` | 是 | `MISSING` 非空 = 配置里的工具名真机上没有(改名了 / 当初的 `tools/list` 过期了)。⚠️**不要自己猜替代名**,把 `MISSING` 和真机 `live` 列表回报回来 |
| `live` 数量 | LogDream **6**、CloudWatch **30** | 和 RB-55 不一致 = 他们那边加/减了工具 |
| `protocol` | 各自协商出的版本号 | 报出来就好,我要知道真机settle在哪个版本 |
| `unused` | 有值正常 | 只是"他们有、我们没接"。**列在这里不给任何调用权限**,其中几个是永远不会接的 |

## 检查 2 —— 每个 server 一次真实只读调用

先 LogDream(旧式 SSE,这条最可能出问题,见检查 3):

```bash
python -c "
from webapp import mcp_client
out = mcp_client.call('log.list_apps')
print('ok            :', out['ok'])
print('tool          :', out['tool'])
print('params_sent   :', out['params_sent'])
print('elapsed_ms    :', out['elapsed_ms'])
print('truncated     :', out['truncated'])
print('text 长度      :', len(out['text']))
print('text 前 200 字 :', out['text'][:200])
"
```

再 CloudWatch(Streamable HTTP)。用一个你手上已知存在的 alarm 名:

```bash
python -c "
from webapp import mcp_client
out = mcp_client.call('aws.get_alarm', {'alarm_name': '<填一个真实 alarm 名>'})
print('ok / tool_reported_error :', out['ok'], out['tool_reported_error'])
print('elapsed_ms               :', out['elapsed_ms'])
print('text 长度                 :', len(out['text']))
print('non_text_blocks          :', out['non_text_blocks'])
"
```

**要回报:** 两组里除 `text 前 200 字` 以外的**全部字段**。
`text` 只回报**长度**;前 200 字**只在它明显不含业务数据时**才贴(比如是个 app 名清单)。
含日志正文或客户标识就**只说长度**。

再故意调一个**不存在的** alarm 名,确认:

- 返回的是 `ok=False` + `tool_reported_error=True`(他们的工具跑了,说没有)
- **不是** `TransportError`(那是"我们根本没连上")

这两件事在事故里导向完全相反的结论,**必须分得开**。回报你看到的是哪一种。

## 检查 3 ⭐ 旧式 SSE 的真实帧格式(我唯一靠猜的地方)

我没见过 LogDream 的实际 SSE 帧。代码按参考实现写的:一个 `event: endpoint` 事件告知
POST 地址,回复走 `event: message`。为稳我还加了一条"按形状识别"的兜底
(非 JSON 且像个路径 = 地址),但**如果检查 2 的 LogDream 那步失败**,请把真实帧抓给我:

```bash
curl -N -H "Accept: text/event-stream" "$SDLC_MCP_LOGDREAM_URL" | head -20
```

**要回报:** 头 20 行的**结构**(`event:` / `data:` 行长什么样)。
data 里如果是 JSON,**只回报它的 key**,不要贴值。地址里的 session id 打码。

## 检查 4 —— 拒绝路径(安全红线,必须跑)

```bash
python -c "
from webapp import mcp_client, mcp_registry
for op in ['danger_probe_not_declared', 'aws.metric_window']:
    try:
        mcp_client.call(op, {'namespace': 'AWS/ECS'} if 'metric' in op else None)
        print(op, '=> 竟然通过了 ⚠️')
    except (mcp_registry.NotAllowed, mcp_registry.NotWired) as e:
        print(op, '=>', type(e).__name__, ':', str(e)[:160])
"
```

**期望:**
- 第一个 → `NotAllowed`(没声明的操作一律拒绝)
- 第二个 → `NotWired`(`aws.metric_window` 的 `namespace` 还是 `"?"`)

**关键点:这两个都必须在"开 socket 之前"就被拒掉。** 本地测试是靠数请求数验的
(refused 的操作必须一个请求都不发)。真机上你能观察到的等价现象:
**报错是瞬间返回的,没有任何网络延迟**。回报是不是瞬间返回。

顺便确认一件事(不用真调,只看代码即可):`webapp/mcp_client.py` 里**没有任何**
接受"工具名"做参数的公开函数 —— 只接受我们的抽象操作名。
这是白名单和禁用名单能成立的前提。

## 检查 5 —— 还剩哪些 `"?"`

```bash
python -c "
from webapp import mcp_registry
r = mcp_registry.readiness()
for name, e in sorted(r.items()):
    if e['state'] != 'ready':
        print('%-32s %-10s %s' % (name, e['state'], e['missing']))
"
```

**要回报:** 全部输出。我需要知道现在到底哪几条不能用、缺哪个参数。

---

## 回报格式

```
0 开关   : SDLC_MCP_ENABLED 已开 / 验证后已关
1 核对   : logdream ok=__ live=__ MISSING=__ protocol=__
           cloudwatch ok=__ live=__ MISSING=__ protocol=__
2 真调   : log.list_apps  -> ok=__ elapsed=__ms text_len=__ truncated=__
           aws.get_alarm  -> ok=__ elapsed=__ms text_len=__
           不存在的 alarm -> 走的是 tool_reported_error 还是 TransportError?
3 SSE帧  : (只有检查 2 的 LogDream 失败时才需要)头 20 行结构
4 拒绝   : 两条都拒掉了吗?是瞬间返回的吗?
5 剩余   : 还有哪几条不是 ready
```

**不要回报:** 日志正文、告警正文、任何客户标识、完整的 tools/list schema、人名。

---

---

## 追加(2026-07-30):调查链三个缺陷已修,开 `SDLC_MCP_ENABLED=1` 前请再跑这一段

你在 pull 前的初检提了三条,**全部确认是我的问题,全部已修**。你先前拦住不让开开关,是对的。

| 你报的 | 状态 | 我改了什么 |
| --- | --- | --- |
| `hk1` 应为 `hkl`(数字 1 vs 小写 L) | ✅ 已修 | 代码默认值改成 `hkl`;更重要的是**不再硬编码** —— source 列表改为从 `servers.logdream.sources` 读(环境词表归你),Python 里只留兜底 |
| `log.list_apps` 没传 `source`,且两个 source 的 app 清单不同 | ✅ 已修 | **按 source 各调一次**并传 `source`;一个 app 只在**它真实存在的 source** 上查;某个 app 只在一边有,会明确写进 `not_investigated` |
| 未分流 MCP 工具错误 | ✅ 已修 | 新增 `_tool_outcome()` 四路分流:传输失败 / **工具报错** / 成功零命中 / 成功有内容。工具报错**禁止进 evidence**,走 `not_investigated`,前端显示 `✖ 工具报错,不作为日志证据` |

⚠️ **有一件事需要你在盒子上做**(我故意没动):`config/mcp_tools.json` 是**你的文件**,
按既定纪律我不在你有本地改动的路径上提交,免得你 pull 冲突。但**已提交的模板里还有两处旧值**:

```
"sources": { "hk1": {...}, "hkp3": {...} }        ← 应为 "hkl"
"log.list_apps": { "args": {} }                    ← 应为 { "source": "source" }
```

你本地那份应该已经是对的(RB-60 就是这么跑通的)。**请确认你本地这两处正确即可**,
不需要动已提交的模板。如果你希望我把模板也改对(会让你 pull 时冲突一次),说一句我就改。

### 追加检查 A —— source 名与 app 清单

```bash
python -c "
from webapp import incident_investigator as inv
print('生效的 source :', inv.log_sources())
print('代码兜底值    :', inv.DEFAULT_LOG_SOURCES)
from webapp import mcp_registry
print('list_apps args:', (mcp_registry.operations().get('log.list_apps') or {}).get('args'))
"
```

**要回报:** 三行。判定:`生效的 source` 必须是真机接受的那两个;
`list_apps args` 里必须有 `source`,否则代码会退化成不传 source(能跑但拿不到权威清单)。

### 追加检查 B ⭐ —— 工具报错不再被当成日志证据

故意用一个**错的 source 名**跑一次调查,确认它被当成"没查"而不是"查到了":

```bash
python -c "
from unittest import mock
from webapp import incident_investigator as inv
with mock.patch.object(inv, 'log_sources', lambda: ('hk1',)):   # 故意用错的名字
    p = inv.investigate('prodECS_<填一个真实仓库名>_service_CPUUtilizationMINOR[80percent] 03:15 HKT')
print('evidence 条数        :', len(p['evidence']))
print('contains_production  :', p['contains_production_data'])
print('not_investigated     :')
for n in p['not_investigated']: print('   -', n[:160])
"
```

**期望:** `evidence 条数 = 0`、`contains_production = False`,
并且 `not_investigated` 里出现 **`REJECTED by LogDream`** 或 **`REPORTED AN ERROR`**。

🔴 **如果 evidence 不是 0,立刻停下回报** —— 那说明分流没生效,错误正文又被包成日志了,
那是这个功能能犯的最严重的错误。

### 追加检查 C —— 正常路径仍然通

用**正确**的 source 跑一次真实调查(和上面检查 2 同一个告警即可),回报:

```
生效 source     : ___
每个 source 的 app 数 : ___ / ___
app 解析结果    : <repo> → <app>,在哪些 source 上核对到
evidence 条数   : ___
not_investigated: ___ 条(逐条贴前 160 字)
```

**判定:** app 数应该接近 RB-55 记的 98 / 93;`app 解析结果` 里的 source 列表要和实际相符。

### 追加检查 D —— 然后才开开关

上面 A/B/C 都符合预期,再把 `SDLC_MCP_ENABLED=1` 打开跑一次真实聊天,
问一句"**这个告警的日志说明了什么**",回报:

- [ ] 前端 sub-agent 面板出现,每步带 `logdream · log.read` 徽标和耗时
- [ ] 有 source 被拒时显示 `✖`,并且答案里**没有**把它说成"查到了日志"
- [ ] 助手明确区分"没查"(`not_investigated`)和"查了没有"
- [ ] 告警不带时区时,助手**先问**"这个 03:15 是 HKT 还是 UTC",而不是自己猜

任何一条不符合 —— 贴原话,那是 prompt 或代码要改。

---

---

## 追加(2026-07-30 第二轮):补上 `log.search_files` 那一跳,并改用真实的时间参数

你第二轮查得更深,发现的是**整条链缺了一跳**,不是几个参数写错。全部照做了。

### 我改了什么(外网侧,已推)

| 你报的 | 已修 |
| --- | --- |
| 缺 `log.search_files` → 没有 `file_name`,而 `read_logdream_log` 必须要 | app 核对成功后**先调 `log.search_files`**,解析候选文件,按「告警日期 → 已知日志类型 → 名字最短」排序,每个 source 取前 2 个,把**真实 `file_name`** 传给 `log.read` |
| 传了不存在的 `from_time`/`to_time` | 改成 **`alert_time` + `mode=alert_time_backtrack` + `backtrack_lines`**;`from_time`/`to_time` 不再出现 |
| 证据里文件名硬编码 `otx_trace.log` | 证据记录**实际读的那个文件名** |
| `queries_run` 记得太早 | 拆成 **`queries_attempted` / `queries_executed` / `queries_failed`**,只有拿到响应才算 executed,被本地拒绝的带 `refused_locally: true` |
| repo → app 映射缺失 | 代码**已经**同时认 `config/logdream_app_map.json` 和 `config/logdream_apps.json`,两种结构都认(`{"repo_to_app": {...}}` 或扁平 `{repo: app}`)。**只差你把文件建起来** |

另外两条我顺手加固的:

- **只发配置真正映射过的参数。** 某个抽象参数在 `mcp_tools.json` 里还是 `"?"` 或没声明 → **不发**。
  所以在你更新配置之前,不会有注定失败的请求打到真机上。
- **`const` 优先于我传的值。** 你在 `const` 里钉死某个参数(比如 `read_mode`),我不会用自己的值去覆盖它。
  这是给你一个**不需要我改代码**的覆盖通道。

### 需要你做的(内网侧):把这些抽象参数映射到真实参数名

我这边现在会用下面这些**抽象名**。请对着**真实 `tools/list`** 填 `config/mcp_tools.json`,
**具体真实参数名以 tools/list 为准,不要照抄我下面的猜测**:

```jsonc
"log.search_files": {
  "tool": "search_logdream_log_files",
  "args": {
    "app":              "<真实参数名>",
    "source":           "<真实参数名>",
    "keyword":          "<真实参数名>",     // 可选,没有就删掉这一行
    "date_hint":        "<真实参数名>",     // 可选
    "filename_pattern": "<真实参数名>"      // 可选
  }
},
"log.read": {
  "tool": "read_logdream_log",
  "args": {
    "app":             "<真实参数名>",
    "source":          "<真实参数名>",
    "file":            "file_name",        // ← 必需,没有它一条日志都读不了
    "mode":            "read_mode",        // 值我传 alert_time_backtrack
    "keyword":         "<真实参数名>",
    "alert_time":      "alert_time",
    "timezone":        "<真实参数名>",      // 可选
    "max_lines":       "<真实参数名>",      // 可选
    "backtrack_lines": "backtrack_lines"   // 可选
  }
}
```

**左边是我的抽象名(固定,别改),右边是真机参数名(以 tools/list 为准)。**
可选的那几个没有就删掉整行——**删掉比填错好**,因为我只发映射过的参数。

⚠️ 已提交模板里 `log.read` 现在写的是 `from_time`/`to_time`,**那两个真机没有**,请删掉换成
`alert_time` / `backtrack_lines`。

### 还需要你建一个文件

```
config/logdream_app_map.json     （或 config/logdream_apps.json,两个名字我都认）
{ "repo_to_app": { "mc-hk-hase-csl-sms-deli-job": "cslSmsDeli", ... } }
```

**先给告警最多的 20–30 个仓库就够解锁演示**,不必 460 个全填。
没有这个文件不会导致误查(候选名核对不上就拒绝查),但会导致**很多仓库根本查不了日志**。

### 追加检查 E —— 补完之后跑这个

```bash
python -c "
from webapp import incident_investigator as inv, mcp_registry
print('search_files 可用参数:', sorted(inv._usable_args('log.search_files')))
print('read 可用参数        :', sorted(inv._usable_args('log.read')))
print('read 必需项是否齐全  :', [n for n in inv.READ_REQUIRED if n not in inv._usable_args('log.read')] or 'OK')
"
```

**`read 必需项是否齐全` 必须是 `OK`**,否则调查员会在本地就拒绝读取(并明确告诉你缺哪个)。

然后跑一次真实调查,回报:

```
search_files 调用次数 / 每个 source 找到几个候选文件 / 选中了哪几个
log.read 实际发出的参数名(只报 key,不要报值)
queries_attempted / queries_executed / queries_failed 三个计数
evidence 条数,以及每条的 file 字段
```

🔴 **重点看两件事:** `evidence` 里的 `file` 必须是**真实选中的文件名**;
`queries_executed` 必须**只包含真正发出去并拿到响应的**那些。

---

## 附:这一步之后**还差什么**才能真正答事故问题

传输层通了不等于助手会用它。剩下的顺序是:

1. **事故调查员 sub-agent**(外部做)—— 原始日志只在它内存里,只把**脱敏后的结构化证据包**
   交回主 agent。**这一步没做完之前,这些 MCP 操作故意不做成模型可调工具** ——
   否则原始生产日志会直接进 `chat_sessions.json`,那是不可逆的合规问题。
   预算车道 `subagent`(20%)已经留好了。
2. **查询计划生成器**(外部做)—— 告警文本 → 查哪个 app / 哪个文件 / 哪个时间窗 / 搜哪些关键词。
   关键词来自我们的代码图+业务图,**这是通用 AIOps 推不出来的那部分**。
3. **Portal 8094**(对方团队)—— 最大的告警家族走它。它不修好,那一族覆盖不了。

以及**只有业主/业务方能答**的三件事(都不阻塞上面 1 和 2):

- `tbl_use_case_router` 那 4 个厂商显示名(`HTCL` / `HTCL OLD` / `AWS HK SNS` / `AWS SG SNS`)
  分别对应白名单里的谁?`HTCL OLD` 是否已下线?两个 AWS 区域要不要分开算?
  → 确认后填 `config/usecase_columns.json` 的 `validation.vendor_display_aliases`,**代码零改动**。
- `delivery_path` 1–9 的文字对照码表(镜像已查尽,查不到)。
- 两个 SLA 列的单位(ms / s / min)—— RB-54 问题 6 至今没答,现在代码带着"单位未确认"的警告在跑。

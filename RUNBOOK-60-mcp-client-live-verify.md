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

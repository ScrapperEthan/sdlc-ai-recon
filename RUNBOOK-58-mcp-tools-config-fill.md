# RUNBOOK-58 —— 填写 `config/mcp_tools.json`(把 MCP 接进来)

> **这是什么:** 上一轮我把"我们想做什么"和"他们那边具体怎么调"拆开了 ——
> **代码里只写死了左边那一列**(`log.read`、`aws.alarm_history` 这些操作名,**不要改**),
> **右边那一列**(真实工具名、参数名)由你对着真实的 MCP 填。
> 这样以后他们改一个参数名,你改一个字段就行,不用等外部开发、不用改代码。
>
> **谁做:** 内网 Codex。**只改 `config/mcp_tools.json` 这一个文件**,不碰其他任何代码。
> **不需要理解 Python**,这是纯粹的"从 A 抄到 B"的工作。

---

## 第一步:对三个端点各拿一次 `tools/list`

RUNBOOK-55 已经连过一次了,这次要拿**完整的参数 schema**,不只是工具名。方式随你选:

- 用你上次连接用的同一段代码/脚本,把 `tools/list` 的**完整返回**存下来(不用截图,存 JSON 文本)
- 三个端点各存一份:`logdream_tools.json` / `cloudwatch_tools.json` / `portal_tools.json`(这几个文件名随意,**不用提交进 git**,只是你自己核对用的草稿)

`tools/list` 的返回大概长这样(每个工具一条):

```json
{
  "name": "read_logdream_log",
  "description": "...",
  "inputSchema": {
    "type": "object",
    "properties": {
      "appName": {"type": "string"},
      "fromTime": {"type": "string", "description": "ISO8601, UTC"},
      "toTime": {"type": "string"},
      "maxLines": {"type": "integer"}
    },
    "required": ["appName"]
  }
}
```

你要抄的就是 `name`(工具名)和 `properties` 里的每个 key(参数名)。

---

## 第二步:打开 `config/mcp_tools.json`,按下面的规则填

打开这个文件,你会看到很多值是 `"?"`。**只改这些 `"?"`,不要改别的**(尤其不要改左边的 key,
比如 `log.read` 这个名字本身、`args` 里 `app`/`file` 这些外部代码写死的字段名)。

### 2.1 填 `servers.*.enabled`

三个 server 各有一个 `enabled` 字段,现在都是 `false`。

- 你**验证完一个 server 的地址/连通性**,就把它的 `enabled` 改成 `true`
- **没验证的不要改**——`false` 时这个 server 完全不会被调用,这是故意的安全默认值
- 地址不写在这个文件里(见下面第 4 步)

### 2.2 填 `operations.*.tool`

每个操作有一个 `tool` 字段,现在是 `"?"`。改成 `tools/list` 里**一字不差**的工具名。

例:

```json
"log.read": {
  "server": "logdream",
  "tool": "read_logdream_log",     ← 从 "?" 改成真实工具名
  ...
}
```

### 2.3 填 `operations.*.args`(最重要,最容易错)

`args` 是一个字典,**左边的 key 不要动**(那是代码里写死的字段名),
**只把右边的值从 `"?"` 改成 `tools/list` 里对应的真实参数名**。

例:代码要传"应用名"和"文件名"这两个字段,`tools/list` 告诉你这两个参数在他们那边分别叫
`appName` 和 `filename`:

```json
"log.read": {
  "tool": "read_logdream_log",
  "args": {
    "app": "appName",       ← 左边 "app" 不要动,右边 "?" 改成 "appName"
    "file": "filename",     ← 左边 "file" 不要动,右边 "?" 改成 "filename"
    "max_lines": "?"        ← 这个参数你还没查到就先留着,不影响别的字段先用
  },
  ...
}
```

**如果某个参数他们那边根本不存在**(比如没有 `max_lines` 这个选项),
把这个 key **整个删掉**(连左边一起删),不要留着 `"?"`。

**如果某个参数是必填的但你还没查清楚**,**先留 `"?"`**——
代码看到 `"?"` 会自己拒绝调用并说清楚缺什么,不会拿错的参数名去瞎调。
**不用一次填完,填一个能用一个。**

### 2.4 填 `operations.*.const`(固定参数)

有些参数**每次调用都传同一个值**,比如 LogDream 要求你显式指定 `source`(hk1 还是 hkp3)。
这类放进 `const`,不放进 `args`:

```json
"log.read": {
  "const": {"source": "hk1"},   ← 每次都传 hk1,不用模型每次决定
  ...
}
```

⚠️ RUNBOOK-56 已确认 **hk1 和 hkp3 都是生产,日志不一样**——按现在的默认设计,
关于日志的每个操作要**各自调两次**(一次 hk1、一次 hkp3),所以 `const` 里的 `source`
你可能需要按"哪类日志在哪边"来定,而不是固定写死一个。**这一条你如果还没查清楚,
就先都填 hk1,标注清楚,我后续接的时候会做成两次调用而不是靠这个字段**——
不确定的时候来问我一句就行,不用自己纠结着卡在这。

### 2.5 特别关注:`log.read` 里的时间和时区(⭐最重要的一格)

RUNBOOK-55/56 已经确认三个时区并存(CloudWatch=UTC / LogDream 默认=Asia/Hong_Kong / 服务器=GMT)。
从 `tools/list` 的 `inputSchema` 里,重点确认并填进 `_note` 或直接告诉我:

1. 时间参数**叫什么名字**(`fromTime`?`startTime`?)
2. **格式是什么**(ISO8601 字符串?epoch 毫秒?)
3. **有没有独立的时区参数**?如果有,填进 `args`;如果没有,在 `_note` 里写清楚
   "这个工具的时间默认按 XXX 时区解释",这样我才知道调用前要不要先把时间换算好

---

## 第三步:验证你改的东西没写错

**不需要装任何额外软件**,项目本来就有测试。在项目根目录跑:

```bash
python -m pytest tests/test_mcp_registry.py -v
```

**全部通过**说明:
- JSON 格式没写错(少个逗号这种)
- 没有意外把某个动作类工具(resend/submit/delete 这些)配置成可调用的
- 每个操作指向的 server 确实存在

如果报错,**把报错信息原样发给我**,不用自己猜怎么改代码。

---

## 第四步:地址和凭证 —— 单独存,不进这个文件

`config/mcp_tools.json` 里**没有真实地址**,只有 `url_env` 指明"这个地址该读哪个环境变量"。

三个环境变量分别是:

```
SDLC_MCP_LOGDREAM_URL
SDLC_MCP_CLOUDWATCH_URL
SDLC_MCP_PORTAL_URL
```

在盒子上运行 webapp 之前,把真实地址设进去(具体设置方式看你们盒子上现有的其他环境变量
怎么配的,跟那个方式保持一致就行)。**这几个值永远不要写进 `config/mcp_tools.json` 或任何
会被提交的文件。**

---

## 一个完整例子(照抄这个格式)

假设 `tools/list` 告诉你 CloudWatch 的 `get_alarm_history` 长这样:

```json
{
  "name": "get_alarm_history",
  "inputSchema": {
    "properties": {
      "alarmName": {"type": "string"},
      "startTime": {"type": "string"},
      "endTime": {"type": "string"}
    },
    "required": ["alarmName"]
  }
}
```

那 `config/mcp_tools.json` 里对应的条目改完应该是:

```json
"aws.alarm_history": {
  "server": "cloudwatch",
  "tool": "get_alarm_history",
  "args": {
    "alarm_name": "alarmName",
    "from_time": "startTime",
    "to_time": "endTime"
  },
  "const": {}
}
```

---

## 不用做的事(明确列出,避免你多做无用功)

- **不用**改 `never_expose` 那一段(那是安全红线,即使你在 `operations` 里配了 resend 类工具,
  它也会被拒绝——这是故意的双重保险)
- **不用**给 Portal(8094)填任何东西——RUNBOOK-55 确认它 404,先跳过,等对方修好再说
- **不用**每个工具都填——先填**用得上的那几个**就行(`log.read` / `log.search_files` /
  `aws.parse_alert` / `aws.alarm_history` / `aws.recent_changes` 优先级最高),
  剩下的以后要用再补
- **不用**跑除 `test_mcp_registry.py` 以外的其他测试(除非你想顺便跑全量 `pytest tests/ -q`
  确认没有连带影响,这个也欢迎,但不是必须)

---

## 回报格式

不需要长篇报告,把改完的 `config/mcp_tools.json` 直接提交推送即可(这个文件在
`AGENTS.md` 分工里归你维护,可以直接推)。如果方便的话简单说一句:

```
填了:log.read / log.search_files / aws.parse_alert / aws.alarm_history / ...
enabled 打开的 server:logdream / cloudwatch(portal 还是 false)
还没查清楚的:log.read 的 max_lines 参数、hk1/hkp3 该怎么分
```

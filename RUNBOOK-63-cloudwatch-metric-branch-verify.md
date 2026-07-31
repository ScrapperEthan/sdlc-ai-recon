# RUNBOOK-63 —— CloudWatch 指标分支已接入（外网 2026-07-31）

> 对应 `AWS-METRIC-WINDOW-EXTERNAL-HANDOFF-20260731.md`。基线 `0693e6a` → 本轮 `c8f1ddf`。
> **交接文档里第 14 节那 30 条测试全部覆盖并通过**；`config/mcp_tools.json` **一个字都没动**。
>
> 跑之前：`git pull --ff-only origin master`。
>
> 外网全量：**981 passed**。你指定的组合 `test_incident_investigator + test_mcp_registry +
> test_mcp_client + test_incident`：**Ran 367 tests, OK**（你那边基线是 303，本轮新增 64 条）。

---

## 一、做了什么

`incident_investigate` 现在**同时跑两条互不影响的分支**：

```
告警文本
├─ LogDream 分支：仓库 → 应用名 → 日志文件 → 关键词  （原有，未变）
└─ CloudWatch 分支：告警名 → get_alarm → 指标身份 → UTC 窗口 → metric_window → 分类摘要
```

按你文档的三条硬规则实现：

### 1. 指标身份**只从告警本身读**，绝不推导

`Namespace` / `MetricName` / `Dimensions` / `Statistic` / `Period` / `EvaluationPeriods`
全部取自 `get_alarm` 响应。缺 `Namespace` 或 `MetricName` → **停，不调 metric**。
`resource` 不会被当成 namespace，仓库名不会被当成 dimension。
理由写进代码注释了：**猜错的身份不会报错，它会把另一个服务的数字放在这次事故的标题下面。**

`Statistic` 和 `Period` 用告警自己的（本次样例是 `Maximum` / `60`），不让模型填默认值。

### 2. 这是**唯一**会换算时刻的地方

CloudWatch 要 UTC，所以这条分支**换算**；LogDream 分支**仍然只重排格式、绝不换算**。
两套规则分开写、分开注释，免得以后有人统一掉。

⚠️**一个你文档里没提、但盒子上会踩的坑**：`zoneinfo` 在**裸 Windows 上没有时区数据库**
（需要 `tzdata` 这个 PyPI 包，而本项目是 stdlib-only 的气隙环境）。
所以实现是：**先试 `zoneinfo`；不可用时退到一张固定偏移表**
（`Asia/Hong_Kong` = UTC+8，香港 1979 年后无夏令时，所以这个偏移是精确值不是近似）；
**两条都不行就拒绝**，不用"差不多的偏移"去换算 —— 换错的窗口会返回错误时段的数据点，
比没有窗口更糟。有专门的测试模拟"导入 zoneinfo 失败"。

### 3. **只有分类结果离开进程**

数据点只在局部变量里参与计算，然后丢弃。packet / session / progress event 里
**没有任何数值**：没有原值、没有平均/最大/最小/最新/差值。留下的只有：

```
points_seen / direction / variability / threshold_relation / data_presence
```

dimension **名字保留**（它标识指标），**值做指纹**（`<dim:34265d>`，同一个值仍然看得出是同一个）。
`AlarmArn`、`AlarmActions`、`StateReasonData` **不进 packet**。

### 4. 两条分支互不拖累

- 告警名读不出来 → 跳过指标分支，**日志调查照跑**；
- 仓库识别不出来 → **指标分支照跑**（它只需要告警名），日志分支记原因；
- LogDream 挂了 → 指标证据仍然产出；
- 记账分开：`cloudwatch_queries.{attempted,executed,failed}`，**不塞进 app/file 的形状**；
- 日志分支那句"查了但没匹配"只按**它自己的**原因判定 —— CloudWatch 的拒绝不会把它顶掉。

### 5. 告警名严格本地提取

按你 §6.3 写的：JSON 只认顶层 `AlarmName` 字符串；文本只认**单独一行**
`^\s*"?AlarmName"?\s*:\s*(...)$`；去成对引号；拒绝含换行；长度上限 255；
**出现两个不同名字就返回空**（那是要问用户的问题，不是掷硬币）。
`aws.parse_alert` 的输出**也要过同一个校验函数**才可用。
新增工具参数 `alarm_name`，优先级：**用户显式 > 严格提取 > 跳过**。
**不用 `list_alarms` 兜底。**

### 6. 顺手修了 `hk1` 文案

`sources_note` 和 `environments.logs` 现在**从 `log_sources()` 动态生成**，不再手写 source 名 ——
手写正是当初 `hk1`/`hkl` 那个错的来源。`webapp/tools.py` 的 drill-down 说明也改成
"读 `plan.sources` 拿实时清单"。

---

## 二、外网做了一个你文档没写的决定，请知悉

**窗口宽度**按你的建议做成显式常量 + 环境变量，并且**在证据里说明它是我们的查询策略、不是 CloudWatch 定义的**：

```
SDLC_INCIDENT_METRIC_MINUTES_BEFORE   默认 15
SDLC_INCIDENT_METRIC_MINUTES_AFTER    默认 15
SDLC_INCIDENT_METRIC_MAX_MINUTES      默认 180   ← 硬上限
```

"告警前"那一侧会被 `Period × EvaluationPeriods` 撑宽到足够覆盖告警实际评估的时长，
**再被 180 分钟截断**。所以：`Period=60 × Eval=5` = 5 分钟 < 15 → 用 15；
`Period=300 × Eval=12` = 60 分钟 → 用 60；`Period=86400 × Eval=30` → 截到 180。
三种情况都有测试。

`metric_window` 的时间格式也做成了配置旋钮
（`operations['aws.metric_window'].request.time_format`，默认 `%Y-%m-%dT%H:%M:%SZ`），
和 `alert_time_format` 一样 —— 免得再来一轮"格式不对"。

---

## 三、盒子上请跑的检查

> 检查 1 离线。检查 2–4 会**真实调用 CloudWatch**（只读，各 2 次）。

### 检查 1 —— 测试

```bash
python -m pytest tests -q
python -B -m unittest tests.test_incident_investigator tests.test_mcp_registry tests.test_mcp_client tests.test_incident -q
```

**期望**：`981 passed`；第二条 `Ran 367 tests, OK`。

### 检查 2 ⭐⭐ —— 真机跑通整条链（本轮核心）

用一个**真实存在的告警名** + **带完整日期和时区**的告警文本：

```bash
python - <<'PY'
import json, os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv
p = inv.investigate(
    "AlarmName: <填一个真实告警名>\n"
    "prodECS_<那个仓库名>_service_CPUUtilizationMINOR[80percent] at 2026-07-30 03:15 HKT",
)
cw = p["plan"]["cloudwatch"]
print("告警名来源  :", cw.get("alarm_name_source"), "| runnable:", cw.get("runnable"))
print("时区换算    :", cw.get("conversion"))
print("窗口        :", p.get("cloudwatch_window"))
print("CW 已执行   :", [(q["operation"], q.get("elapsed_ms")) for q in p["cloudwatch_queries"]["executed"]])
print("CW 失败     :", [(q["operation"], q.get("reason","")[:80]) for q in p["cloudwatch_queries"]["failed"]])
for e in p["evidence"]:
    if e["kind"] == "cloudwatch_metric":
        print("指标证据    :", json.dumps({k: v for k, v in e.items()
              if k in ("namespace","metric","statistic","period_seconds","points_seen","summary","status_code")},
              ensure_ascii=False))
        print("维度        :", e["dimensions"])
print("未查项      :")
for n in p["not_investigated"]:
    print("   -", n[:150])
PY
```

**这段输出可以整段贴回来** —— 里面没有告警名以外的标识、没有 ARN、没有 dimension 原值、没有任何数值。
（**告警名是你填进去的**，贴之前自己决定要不要打码。）

**判定**：

| 看哪一行 | 说明什么 |
| --- | --- |
| `CW 已执行` = `[('aws.get_alarm', …), ('aws.metric_window', …)]` | 整条链通了 ✅ |
| `窗口` 的 `start_utc`/`end_utc` 在**告警时刻附近**、不是"现在" | 时间基准正确 ✅ |
| `时区换算` 里写 `zoneinfo` 还是 `fixed offset table` | **两个都正常**，但请回报是哪一个（盒子有没有 tzdata） |
| `指标证据` 有 `points_seen > 0` | 拿到数据了 ✅ |
| 未查项里出现 `SUCCEEDED but its response could not be read` | 返回体字段名和默认不一样 → 见检查 5 |
| 未查项里出现 `does NOT mean the service was healthy` | 该窗口没有数据点。**这是正常结果**，不是失败 |

🔴 **如果 packet 里出现了任何真实指标数值、平均值、ARN 或 dimension 原值，立刻停下回报** ——
那是这个分支能犯的最严重的错误。

### 检查 3 —— 两条分支互不拖累

```bash
python - <<'PY'
import os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv

# A) 有告警名、没有仓库名 → 指标应该照跑
a = inv.investigate("AlarmName: <真实告警名>\nsomething broke at 2026-07-30 03:15 HKT")
print("A 指标证据 :", any(e["kind"]=="cloudwatch_metric" for e in a["evidence"]),
      "| CW 调用", len(a["cloudwatch_queries"]["executed"]))

# B) 有仓库名、没有告警名 → 日志应该照跑，CW 零调用
b = inv.investigate("prodECS_<那个仓库名>_service_CPUUtilizationMINOR[80percent] at 2026-07-30 03:15 HKT")
print("B 日志证据 :", any(e["kind"]=="log_lines" for e in b["evidence"]),
      "| CW 调用", len(b["cloudwatch_queries"]["executed"]),
      "| 说明:", [n[:60] for n in b["not_investigated"] if "alarm name" in n])
PY
```

**期望**：A 有指标证据、CW 调用 2 次；B 有日志证据、**CW 调用 0 次**且说明里写"没读出唯一告警名"。

### 检查 4 —— 缺时间时两边都零调用

```bash
python - <<'PY'
from unittest import mock
from webapp import config, incident_investigator as inv, mcp_client
calls = []
with mock.patch.object(config, "MCP_ENABLED", True), \
     mock.patch.object(mcp_client, "call",
                       lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                           AssertionError("不应该有调用"))):
    p = inv.investigate("AlarmName: <真实告警名>\nprodECS_<那个仓库名>_service_CPUUtilizationMINOR[80percent]")
print("调用次数 :", len(calls))
print("原因     :", [n[:120] for n in p["not_investigated"]])
PY
```

**期望**：`调用次数 0`，原因里同时点名「缺 DATE / 缺 TIMEZONE」和「指标窗口不从 now() 造」。

### 检查 5 —— 返回体形状对不上时（只在检查 2 报了解析失败才需要）

`aws.metric_window` 的返回体解析也走 §RB-61 那套旋钮。真实字段名和 `Timestamps`/`Values` 不同时：

```jsonc
"aws.metric_window": {
  "response": {
    "timestamps": "Timestamps",   // 举例
    "values":     "Values"
  }
}
```

> ⚠️ 目前 metric 解析是**按你文档 §4.4 写死的 `Timestamps` / `Values`**，因为那是你从真机确认过的。
> 如果真机后来变了，告诉我，我按同一个模式加上 `response` 旋钮 —— **不要自己改 Python**。

### 检查 6 —— 前端

打开问答页，用检查 2 那条告警问「**这个告警的日志和指标说明了什么**」，回报：

- [ ] sub-agent 面板里出现 `alarm_resolve` / `alarm_lookup` / `metric_window` / `metric_evidence` 四步
- [ ] 每步带 `cloudwatch · aws.get_alarm` 之类的徽标 + 耗时
- [ ] 面板里**没有**告警名、没有数值
- [ ] 助手的答案里**没有**把 `points_seen: 0` 说成"系统正常"，也**没有**编造平均值

---

## 四、还没做 / 明确不做

| 项 | 状态 |
| --- | --- |
| `aws.parse_alert` 作为辅助提取器 | **暂不调用**。你 §6 证明它当前不可信，而严格本地提取已经够用，多一次调用只是多一个不确定来源。要接的话代码里已经有 `valid_alarm_name()` 这个统一校验口子，随时可加 |
| `list_alarms` 兜底 | **明确不做**（你 §16 也这么写） |
| `metric_minutes_before/after` 做成工具参数 | **不做**，按你建议留在服务端配置，不让模型任意放大窗口 |
| 多个告警名时自动选一个 | **不做**，返回空并要求用户指定 |

---

## 五、这一轮的记录

前四轮的教训是"**凡是只有内网能观察到的东西 —— 名字、形状、值格式 —— 都不该写死在外网的 Python 里**"。

这一轮交接文档把**真实契约、真实响应结构、真实参数名**全部先给了出来，
所以外网这次没有需要猜的地方 —— 这是四轮以来第一次。
唯一自己判断的是 `zoneinfo` 在气隙 Windows 上不可用这件事，那是外网环境侧的问题，已经写在 §一.2。

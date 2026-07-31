# RUNBOOK-64 —— alarm name 出口指纹化 + 原子写重试加宽（外网 2026-07-31）

> 对应 `RUNBOOK-63-INTRANET-UAT-20260731.md`。基线 `6414418` → 本轮 `af140d6`。
> 你报的**两件事都改完了**，`config/mcp_tools.json` 仍然**一个字没动**。
>
> 外网全量：**1027 passed**。你指定的组合：**Ran 409 tests, OK**（上轮 367）。
>
> 先说结论：**§7 那条我按"严格"那一档处理了**，所以它不再是「功能 ready / 隐私加固未完成」，
> 可以直接签。理由见下。

---

## 一、§7 alarm name 持久化 —— 已按严格档处理

### 你给的两个选项，外网选了严格的那个

你写的是：

> - 如果允许 session 保存用户已经提供的告警文本/告警名：不阻塞功能上线。
> - 如果安全要求是 tool packet 也不得持久化完整 alarm name：当前只能算 功能 ready、隐私加固未完成。

外网直接按**第二档**做了，三个理由：

1. **功能上零成本**。告警名是**用户自己给的**，本来就在对话里；packet 不需要再存一份。
   模型要说"我查的是哪个告警"，从用户那句话里就有。
2. **不做的话 packet 自相矛盾**。你的报告里那句最关键：
   *"检测到的所谓 dimension 原值命中也只发生在该 alarm name 中，因为 alarm name 本身包含了相同的服务标识。"*
   —— 也就是说我们**把同一个标识在 dimension 里打了指纹、又在 alarm name 里原样印出来**。
   这不是"还差一点"，是逻辑上讲不通。
3. **把一个待确认项直接消掉**，比留着让你们再走一轮策略确认便宜。

### 怎么改的

**在 `_finish()` 出口闸门做**，不是在某个字段上做。`_finish` 是所有返回路径（包括所有拒绝路径）
的唯一出口，所以没有哪个分支能绕过去。而且是**整个结构做替换**，不是只清 `plan.cloudwatch.alarm_name`
那一个字段 —— 和 PII 出口闸门同一个道理：**清一个已知字段很容易，未来某条新代码路径把它塞到别处也很容易。**

```
调 aws.get_alarm 时         : 真实名字（局部变量，必须真实，否则功能就废了）
progress event 里           : 本来就没有（你已经验证过）
terminal packet 里          : <alarm:31b6f4>
plan.cloudwatch.alarm_name  : <alarm:31b6f4>
```

指纹**稳定**：同一个告警两次调查得到同一个标记，所以"这两次是同一个告警"仍然看得出来。

另外加了一句给模型看的说明（`alarm_name_note`）：
**告警名是用户告诉你的，按用户写的说；永远不要编，也不要把这个标记当成名字念出来。**

### 一个边界情况

极短的名字（<4 字符）**不做替换** —— 整包子串替换只对真实告警名安全，
一个叫 `cpu` 的名字会把 packet 里所有 "cpu" 都打成指纹。有测试盯着。

### 你要的四条测试都写了（`AlarmNameNeverPersistsTests`）

| 你的要求 | 测试 |
| --- | --- |
| 1. `aws.get_alarm` 仍收到真实抽象参数 `alarm_name` | `test_get_alarm_still_receives_the_real_name` |
| 2. terminal packet / session 不含真实 alarm name | `test_the_terminal_packet_carries_a_fingerprint_not_the_name` |
| 3. progress event 不含真实 alarm name | `test_progress_events_carry_no_alarm_name` |
| 4. 显式参数和 alert-text 提取两种来源都覆盖 | `test_both_ways_in_are_covered_extraction_and_the_explicit_parameter` |

外网另加两条：拒绝路径（一次调用都没有）也照样脱敏、以及同一告警两次指纹一致。

---

## 二、Windows `os.replace` 又中了一次 —— 重试预算加宽

你报的：*"全量测试第一次为 980 passed / 1 failed，唯一失败是 Windows 临时 JSON 的 `os.replace`
瞬时 Access denied；单测重跑通过。"*

上一轮我给的是 **5 次重试 / 共 0.5 秒**。全代码库现在只有 `webapp/atomic_json.py` 一处写临时文件，
所以那就是**预算不够** —— 公司杀软带云查杀时，拿住一个刚关闭的文件超过半秒完全正常。

这轮三处改动：

1. **重试加宽到 8 次 / 共约 1.8 秒**，仍然有界（不会把一个真锁死的文件拖成挂起）。
2. **临时文件名改成"每次写"唯一**（原来是每进程唯一）。原来同一个进程连续两次写会复用同一个临时名，
   于是**杀软还拿着上一次的临时文件时，挡住的是下一次的 `open()`，不是 `replace()`** ——
   同一个写丢失的第二条路径。
3. **`open()` 也加了同样的重试**。杀软恰恰是在我们**关闭文件的瞬间**去扫它的，所以两端都要挡。

顺带收紧：**非权限类的 `OSError`（目录不存在、磁盘满）立刻抛出**，不再白等两秒 ——
那是真问题，不是瞬时锁。

> 如果这轮全量测试**还是**偶发这一条，请把**失败的测试名**贴回来。
> 只有一处代码在写临时文件，所以测试名能直接定位是哪一类调用。

---

## 三、你报告里其余各项，外网的确认

| 你的观察 | 外网确认 |
| --- | --- |
| 最后一个 `refused` 只表示本次 UAT 故意没给 repo，LogDream 分支不可运行，**不是 CloudWatch failure** | ✅ **完全正确，这就是设计意图。** 两条分支独立记账、独立拒绝，正是为了让这种情况一眼能分清 |
| UAT 只在测试脚本内用 `list_alarms`，产品 investigator 没有加 fallback | ✅ 正确，**产品侧永远不会加**（你 §16 也这么写） |
| 三个 live `tools/list` 均无 declared-tool missing | ✅ |
| 服务器和本地 `config/mcp_tools.json` SHA-256 一致 | ✅ 本轮同样没碰 |

---

## 四、盒子上请跑的检查

### 检查 1 —— 测试

```bash
python -m pytest tests -q
python -B -m unittest tests.test_incident_investigator tests.test_mcp_registry tests.test_mcp_client tests.test_incident -q
```

**期望**：`1027 passed`；第二条 `Ran 409 tests, OK`。
**如果还有 `os.replace` 偶发失败：贴测试名。**

### 检查 2 ⭐ —— 真机重跑上次那条链，确认告警名不再落盘

用**同一个真实告警**重跑一次：

```bash
python - <<'PY'
import json, os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv

REAL = "<填上次 UAT 用的那个真实告警名>"
events = list(inv.investigate_events(
    "AlarmName: %s\nprodECS_<那个仓库名>_service_CPUUtilizationMINOR[80percent] "
    "at 2026-07-30 03:15 HKT" % REAL))
packet = events[-1]["packet"]
steps  = json.dumps([e for e in events if e.get("type") == "subagent_step"], ensure_ascii=False)
blob   = json.dumps(packet, ensure_ascii=False)

print("packet 里的 alarm_name :", packet["plan"]["cloudwatch"]["alarm_name"])
print("CW 已执行              :", [q["operation"] for q in packet["cloudwatch_queries"]["executed"]])
print("指标证据               :", [ {k: e[k] for k in ("namespace","metric","points_seen","summary")}
                                    for e in packet["evidence"] if e["kind"]=="cloudwatch_metric" ])
print("真实告警名在 packet 里 :", REAL in blob)
print("真实告警名在 事件流 里 :", REAL in steps)
PY
```

**期望**：

- `packet 里的 alarm_name` 形如 `<alarm:xxxxxx>`
- `CW 已执行` = `['aws.get_alarm', 'aws.metric_window']`（**功能没被脱敏破坏**）
- 最后两行**都是 `False`**

🔴 最后两行只要有一个是 `True`，立刻停下回报。

### 检查 3 —— 会话落盘后再核一次

跑一次**真实聊天**（问「这个告警的日志和指标说明了什么」），然后：

```bash
python -c "
import json
d = open('webapp_data/chat_sessions.json', encoding='utf-8').read()
REAL = '<那个真实告警名>'
print('会话文件里出现真实告警名 :', REAL in d)
print('文件大小 :', len(d))
"
```

**期望**：`False`。

> ⚠️ **一个诚实的边界**：如果**用户自己**在聊天里把告警原文粘进去了，那句**用户消息**当然会被存下来 ——
> 那是用户的输入，不是我们的 packet。这轮解决的是**我们产出的那一份**。
> 如果安全要求连用户输入也不能落盘，那是另一个决定（要在 `session_store` 层做），告诉我我再做。

### 检查 4 —— 开关

```bash
python -c "
from webapp import config
print('MCP_ENABLED       :', config.MCP_ENABLED)
print('INCIDENT_RAW_LOGS :', config.INCIDENT_RAW_LOGS)
"
```

按你们门禁流程决定。外网这边**没有已知遗留问题**了。

---

## 五、记录

这轮值得记的是**你那句观察本身**：

> "检测到的所谓 dimension 原值命中也只发生在该 alarm name 中，因为 alarm name 本身包含了相同的服务标识。"

这不是"又发现一处泄漏"，是**指出了我脱敏方案的一个逻辑漏洞**：
我按**字段类型**决定脱不脱敏（dimension value 脱、alarm name 不脱），
而真实世界里**同一个标识会同时出现在多个字段**。
所以出口闸门现在是**按值**做整包替换，不是按字段 —— 和 PII 那层用的是同一个道理，
只是我上次没把这个道理套到 alarm name 上。

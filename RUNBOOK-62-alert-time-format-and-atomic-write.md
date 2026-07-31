# RUNBOOK-62 —— `alert_time` 格式 blocker 已修 + Windows 原子写（外网 2026-07-31）

> 对应 `RUNBOOK-61-SEND-BACK.md`。**RB-61 的三条 blocker 你们已验证通过，这份只处理送回来的新问题。**
> 跑之前：`git pull --ff-only origin master`。仍然**没有动** `config/mcp_tools.json`、
> `AGENTS.md`、`mdc_sheet_schema.json`。
>
> 你们已经补进配置的 `log.search_files.response.entries=matched_files` **不用改**，
> 那正是这个旋钮设计出来给你们用的方式 —— 一个字段名的差异，一次配置编辑，没有等外网推。

---

## 一、送回来的四项，处理结果

| 你报的 | 归谁 | 处理 |
| --- | --- | --- |
| **新 blocker**：`2026-07-30 03:15 HKT` 原样传给 `alert_time`，真机拒绝 | **外网** | ✅ 已修，见 §二 |
| 全量测试 **901**（不是 909），且 1 个 Windows `os.replace` 偶发失败 | **外网** | ✅ 两条都处理了，见 §三。**909 是我写错了**，实测就是 901；本轮加了测试后是 **917** |
| repo→app 可解析 **51/460** | 双方 | 代码行为不变（推出来的名字永远只是候选）。§五 有一件小事要你回一行 |
| 映射报告只对上 **437/460**，缺 **23** 个 | **内网** | 外网不改。我上次说"预期差 4 个"是错的 —— 那是我从 456/460 硬算的，实测 23 才是真数字。见 §五 |

---

## 二、新 blocker：`alert_time` 的格式（已修）

### 错在哪

外网一直把**告警原文里的时间字符串原样**传过去，理由是"绝不转换时刻"。
方向没错，**但把格式和时刻混为一谈了**：真实工具要的是

```
alert_time = 2026-07-30 03:15:00
timezone   = Asia/Hong_Kong
```

**两个独立参数**，而外网塞了一个 `2026-07-30 03:15 HKT` 进去，真机拒绝。

### 改法

1. **拆开传**：时间戳规范成 `YYYY-MM-DD HH:MM:SS`，时区走自己的参数。
   `retriever/incident.py` 新增 `normalize_stamp()`，注释里写死一句：
   **这是重排格式，不是换算时刻 —— 03:15 还是 03:15。**
2. **格式本身进配置**（又一次同样的教训）：
   `operations['log.read'].request.alert_time_format`，默认 `%Y-%m-%d %H:%M:%S`。
   下次他们改格式，你们改一行配置，不用等外网。

   ```jsonc
   "log.read": {
     "request": { "alert_time_format": "%Y-%m-%dT%H:%M:%SZ" }   // 举例；不写就用默认
   }
   ```
3. **顺带发现并堵上的一个同类缺口**：光有时区还不够，**还得有日期**。
   `at 03:15 HKT` 以前能建出窗口然后发一个真机拒绝的请求；现在它和缺时区一样是 **BLOCKING**，
   零调用。理由和缺时区完全一样：**猜是哪一天，和猜是哪个时区，失败方式一模一样** ——
   查错窗口 → 返回空 → 空被读成"没问题"。
4. **拒绝信息会分别指出缺哪一半**（缺 DATE / 缺 TIMEZONE / 两个都缺），
   模型据此只问缺的那一个，不会在事故中途浪费一轮问错问题。
5. **新增 `alert_time` 工具参数**：用户在聊天里说"是 7 月 30 号凌晨 3:15 香港时间"，
   模型就能补上，不用让人重贴告警。工具说明里明确禁止模型自己算"昨天"或补今天的日期。

### 一个副作用，先说在前面

前端起始页那张「生产日志根因」演示卡片，原来写的是 `at 03:15 HKT`（没有日期）——
**按新规则它会直接拒绝**。已经改成 `at 2026-07-30 03:15 HKT`。
演示前把日期改成日志实际覆盖的那天。

---

## 三、Windows `os.replace` 偶发失败（已修，不是测试问题）

`os.replace` 在 Windows 上**只要别的进程持有目标文件就会 `PermissionError`** ——
在公司电脑上就是杀毒软件或索引服务刚碰过这个文件。

**测试偶发只是便宜的那个症状。贵的那个是应用在跑的时候同一个竞态会丢掉一次写** ——
一条会话、一次路由注册、或一条留存日志。而业主定的规矩是**已持久化的状态绝不能丢**。
所以修在写入侧，不是修测试：

- 新增 `webapp/atomic_json.py`，三个 store（会话 / 原始日志 / LLM 路由）统一走它；
- **临时文件名带 pid** —— 原来固定的 `<store>.tmp` 会让两个进程互相截断对方写到一半的文件；
- **replace 短暂重试**（0.05→0.2s，总共约 0.5 秒），最后仍失败就**抛出**，绝不吞掉；
- 失败时删掉临时文件，不留 `<store>.1234.tmp` 让人以为是备份。

有一条测试断言三个 store 都不再自己调 `os.replace`。

---

## 四、盒子上请跑的检查

### 检查 1 —— 全量测试

```bash
python -m pytest tests -q
```

**期望 917 passed。** 如果还有 `os.replace` 偶发失败，**贴出失败的测试名** ——
那说明还有第四处没走 `atomic_json`。

### 检查 2 ⭐ —— 正确格式真机跑通（这是本轮的核心）

用你们上次跑成功的那个仓库名 + **带完整日期和时区**的告警：

```bash
python - <<'PY'
import json, os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv
p = inv.investigate(
    "prodECS_<上次跑成功的那个仓库名>_service_CPUUtilizationMINOR[80percent] at 2026-07-30 03:15 HKT")
w = p["plan"]["window"] or {}
print("alert_time  :", repr(w.get("alert_time")))
print("timezone    :", repr(w.get("timezone")))
print("at (原文)   :", repr(w.get("at")))
print("evidence    :", len(p["evidence"]))
print("executed    :", len(p["queries_executed"]))
print("failed      :", len(p["queries_failed"]))
for n in p["not_investigated"]:
    print("  未查 :", n[:160])
PY
```

**期望**：
- `alert_time` 恰好是 `'2026-07-30 03:15:00'`（**没有 `HKT`**）
- `timezone` 是 `'Asia/Hong_Kong'`
- `at (原文)` 还留着 `2026-07-30 03:15 HKT`（原文照留，方便核对）
- `evidence` / `executed` 和你们上次手工按正确格式跑出来的一致（上次是 6 / 6）

🔴 如果真机**仍然拒绝**，把它回的错误原文贴回来，并在配置里试
`operations['log.read'].request.alert_time_format`（§二.2）—— 这一项不用等外网。

### 检查 3 —— 缺日期 / 缺时区都必须零调用

```bash
python - <<'PY'
from unittest import mock
from webapp import config, incident_investigator as inv, mcp_client
BASE = "prodECS_<上次跑成功的那个仓库名>_service_CPUUtilizationMINOR[80percent]"
for tag, text in (("完全没有时间", BASE),
                  ("只有时钟没日期", BASE + " at 03:15 HKT"),
                  ("有日期没时区",   BASE + " at 2026-07-30 03:15")):
    calls = []
    with mock.patch.object(config, "MCP_ENABLED", True), \
         mock.patch.object(mcp_client, "call",
                           lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                               AssertionError("不应该有调用"))):
        p = inv.investigate(text)
    print("%-14s 调用 %d 次 | ok=%s | %s" % (
        tag, len(calls), p["plan"]["ok"], (p["not_investigated"] or [""])[0][:110]))
PY
```

**期望**：三行**都是 `调用 0 次`、`ok=False`**，且理由分别点名
「缺 DATE」/「缺 DATE」/「缺 TIMEZONE」（第一行两个都点名）。

### 检查 4 —— 补日期的那条路能走通

```bash
python - <<'PY'
import os
os.environ["SDLC_MCP_ENABLED"] = "1"
from webapp import incident_investigator as inv
p = inv.investigate(
    "prodECS_<上次跑成功的那个仓库名>_service_CPUUtilizationMINOR[80percent] at 03:15 HKT",
    alert_time="2026-07-30 03:15")
w = p["plan"]["window"] or {}
print("alert_time:", repr(w.get("alert_time")), "| source:", w.get("source"))
print("evidence  :", len(p["evidence"]))
PY
```

**期望**：`alert_time` = `'2026-07-30 03:15:00'`，`source` 里出现 `caller-supplied`，能查出证据。
（这条对应"用户在聊天里补一句日期"的真实路径。）

### 检查 5 —— 开关仍默认关

```bash
python -c "
from webapp import config
print('MCP_ENABLED       :', config.MCP_ENABLED)
print('INCIDENT_RAW_LOGS :', config.INCIDENT_RAW_LOGS)
"
```

**期望**：都是 `False`。检查 1–4 全绿后就可以按你们的门禁流程决定要不要持久化打开了 ——
外网这边已经没有已知 blocker。

---

## 五、还剩两件小事（都不需要判断，只要一行回报）

1. **给外网一个能解析出应用名的仓库名**（51 个里随便一个，最好就是你们检查 2 用的那个）。
   用途：前端演示卡片里那条告警要换成它。你们也可以直接改
   `webapp/static/index.html` 里 `id="prompt-logs"` 那一行的 `data-prompt`，就一个仓库名。
2. **映射报告缺的 23 个**：这是映射脚本的输入清单没跟上 460。
   外网不改，也不再讨论 456 —— **对外统一报 460**（RUNBOOK-50 盒子实测）。
   麻烦用当前 roster 重跑一次生成，然后回报新的 `匹配 / 未匹配 / 总数`。

> 顺带：`repo→app 51/460`（约 11%）比 RB-55 按规则估的 ~36% 低不少。这不是缺陷 ——
> 规则推的名字**必须**在服务器自己的清单里出现才算数，对不上就明确拒绝。
> 要提高覆盖只有一条路：`config/logdream_app_map.json`（或 `logdream_apps.json`，两个名字都认）。
> **不必全量**，先补告警最多的 20–30 个就够解锁演示。

---

## 六、这一轮的教训

上一轮我写的是"**参数名早就走配置了，返回体的形状却还硬编码在 Python 里**"。
这一轮被抓到的是**参数的值格式**也硬编码着 —— 同一句话，第三个位置。

而且这次的错更细：我以为"原样传 = 不转换 = 安全"，
但**格式和时刻是两件事**，把时区粘在时间戳里既不是"不转换"，也根本不是他们要的形状。

所以 `alert_time_format` 也进配置了。规则收敛成一句：
**凡是只有你们能观察到的东西 —— 名字、形状、格式 —— 都不该写死在外网的 Python 里。**

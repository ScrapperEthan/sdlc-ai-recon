# RUNBOOK-70 — Portal-only 范围闸门 + 部署收尾

回应 `MCP-INTEGRATION-AUDIT-20260804.md`。基线 `568aff2`，**1284 tests**。

## 0. 你们审计里那句最重要的话

> 当前规则在 hkp3 的 live app 列表上仍可让 **35 个非 Portal repo** 通过候选匹配。

这是一个**很容易犯、很难看见**的分类错误：**app 出现在 live 列表里，只说明它「存在」，
不说明我们「被允许读它」。** 而按命名规则猜出来的名字一旦碰巧存在，
从下游看它和一个确认过的目标**长得一模一样**。

范围现在是一道**单独的、在任何查询之前跑的**闸门。

---

## 1. 两个开关（都在 `servers.logdream`，都**缺省不启用**）

```json
"allowed_apps": ["portal"],
"app_resolution_policy": "explicit_mapping_only"
```

| 开关 | 作用 |
| --- | --- |
| `allowed_apps` | 不在这个列表里的 app **一律不解析** —— 不管它是映射来的还是规则猜的 |
| `app_resolution_policy` | `explicit_mapping_only` = 没有显式映射的 repo **直接 0 候选，不猜**；`mapping_then_rules` = 旧行为（默认） |

**为什么默认是「开放」而收紧写在配置里**：不填 = 和这个功能存在之前一模一样。
否则任何人 pull 一下就把自己的部署悄悄收窄了。**你们环境里的名字一个都没有进我的代码。**

### ⭐ 以后要加 app：两步，都在配置里

1. `config/logdream_app_map.json` 加映射：
   ```json
   {"repo_to_app": {"mc-hk-hase-portal-web": "portal",
                    "mc-hk-hase-xxx-job":    "xxxApp"}}
   ```
2. `servers.logdream.allowed_apps` 加上 `"xxxApp"`

**两步都做完才生效，这是故意的**：映射说「这个 repo 对应哪个 app」，
白名单说「这个 app 我们现在允许读」。两件事分开，才能一个一个放开而不是一次全开。
配置里 `_scope_README` 写了同样的说明，给不在这轮对话里的人看。

### 拒绝时会说清是哪一种

两种情况都产生 0 候选，但**不能混为一谈**——一个是系统在正常工作，一个是缺口：

```
… maps to LogDream app 'cslSmsDeli', which is OUTSIDE the configured scope (portal).
   This is a deliberate restriction …, NOT a missing mapping and NOT an empty log.

… has no entry in the intranet's LogDream app map, and the configured policy is
   'explicit_mapping_only' — so no app name was guessed … A guessed name that happens
   to exist on the server would read as a confirmed target, which is why it is refused.
```

---

## 2. 顺带堵上的一个洞：caller 给的 `sources` 以前直通线路

`sources=[...]` 是**下钻**，不是**覆盖**。以前它原样进查询，所以模型可以点名
一个你们 `query_by_default:false` 关掉的 source，或者干脆编一个。
**关掉的 source 返回的是拒绝，而拒绝被读成「没日志」，「没日志」被读成「没问题」。**

现在：只能**收窄**已配置的集合，越界的记 refusal 并说明配置里有哪些。

---

## 3. 日志文件名也进配置了

`sftp.log` 之前是代码里的字面量。你们说范围内的 `portal` 只有 `exception` 和 `trace`，
所以 `log_files.other` 清空了。**查一个不存在的文件会白花一次查询预算**，
而它返回的拒绝又会被读成「没日志」。

以后加文件：`servers.logdream.log_files.other` 里加一个名字即可，不用改代码。

---

## 4. `hkl` 的拼写 —— 已跟 owner 确认

你们审计 §5.1 写的是 `hk1`（数字一），08-04 那份 RECONNECT handoff 和 owner 都说是
**`hkl`（字母 L）**。**Owner 已确认：是 hkl，之前一直用错了。**

配置里 `hkl` 保留但 `query_by_default:false`（**关掉不是删掉** —— 保留是为了让正确拼写
留在记录里），`hkp3` 是当前唯一在用的。有一条测试断言 `hk1` 不在配置里 ——
这个形近值已经错过两个方向了。

---

## 5. 请你们做的

### A. 自动测试

```bash
git pull
python -m pytest tests -q
```

期望 **1284 passed**。新增 `tests/test_incident_scope.py`（18）。

### B. ⚠️ `config/mcp_tools.json` 又会冲突 —— 取你们的，然后补这几处

这个文件是你们维护的，我这轮又动了它。**冲突取你们那份**，然后确认下面几项在：

| 位置 | 值 |
| --- | --- |
| `servers.logdream.allowed_apps` | `["portal"]` |
| `servers.logdream.app_resolution_policy` | `"explicit_mapping_only"` |
| `servers.logdream.sources` | `hkl`（字母 L，`query_by_default:false`）+ `hkp3`（true） |
| `servers.logdream.log_files.other` | `[]` |
| `servers.logdream._scope_README` | 加 app 的两步说明 |

**这些字段一个都不填，代码也能跑**（默认 = 旧行为）。填了才收紧。

### C. 范围闸门真机验收

1. 用一个**范围外**的 repo（比如某个 sms deli job）跑一次日志调查 →
   期望 `targets[0].out_of_scope: true`、`app_note` 里出现
   `explicit_mapping_only` 或 `OUTSIDE the configured scope`、**0 次 log 查询**。
2. 用 `mc-hk-hase-portal-web` 跑一次 → 期望正常解析到 `portal` 并真的查到日志。
3. 传 `sources=["hkl"]` 下钻 → 期望被拒绝，`sources` 仍是 `["hkp3"]`，refusal 里点名 `hkl`。
4. 确认 `plan.log_files` 是 `["otx_trace.log", "exception.log"]`，没有 `sftp.log`。

### D. 部署那条（你们 §6 P0）

RUNBOOK-69 的语义防线 + 这轮的范围闸门都在 `568aff2`。你们说部署实例：

- 缺 `incident_plan.py` / `incident_parse.py` / `redaction.py` / `mcp_console.py` /
  `static/app.js` / `static/app.css`；
- `server.py` 有内网 Token/LLM 定制，**不能整文件覆盖**。

`server.py` 这轮我只加了两处，都容易手工合：
- `STATIC_FILES` 常量 + `do_GET` 里那个 `elif path in STATIC_FILES:` 分支（静态资源）；
- `/api/mcp/catalog`（GET）和 `/api/mcp/call`（POST，记得加进 `allowed` 列表）。

其余六个文件是**新增**，直接放进去即可，没有和你们定制冲突的地方。

部署后请重跑那个合成 keyword 探针，期望：

```
evidence_count = 0
outcome        = semantic_downgrade
```

（审计里部署实例是 `evidence_count=1`，那就是没上语义防线。）

---

## 6. 仍然只有你们/owner 能给的

| 事 | 卡在谁 |
| --- | --- |
| 一条**真实授权**的 tracking ID（SMS 或 Email） | Portal owner。目前只有 synthetic `not_found`，正向 shape 没验过 |
| 一个能映射到 log group 的 alarm/resource | CloudWatch owner。`aws.query_logs` 缺正向样本 |
| 一个告警目标资源的明确 ARN | CloudWatch owner。`aws.resource_tags` 按安全规则零调用 |
| keyword 无命中返回空（而不是回落 tail） | LogDream MCP owner |
| `log.investigate` 的 strict candidate / strict keyword / global budget | LogDream MCP owner。给了我就把 `caller_policy` 从 `manual_only` 改成 `enabled`，一行配置 |
| `/home` 100% 磁盘清理 | 运维。**别删未知目录** |

---

## 7. 下一步（owner 已定）

MCP 这条线到此收尾。下一章是**只读数据库接入 MDC assistant** —— owner 说这轮先不动，
等 MCP 完全落地再单开一轮，我会先出方案再写代码。

# Agent 模式不得低于 Ask 模式 —— 证据出身、Answer Packet 定稿、五处最小修复

> 这是 agent 模式的**第三份**文档，专治一件事：**同一个问题，ask 模式答得出来，agent 模式答成"结论：无"。**
>
> - [`agent-mode-zh.md`](agent-mode-zh.md) 定方向和契约；
> - [`agent-mode-implementation-zh.md`](agent-mode-implementation-zh.md) 定 21 项任务；
> - **本份**定 Answer Packet 的字段、证据出身表、以及能立刻止血的五处改动。
>
> 写作依据：2026-08-17 的告警实录截图（agent 模式）＋ 2026-08-17 实测的外网代码
> （`retriever/channel_evidence.py`、`retriever/unified_impact.py`、`retriever/code.py`、
> `retriever/incident.py`、`retriever/messages.py`、`retriever/usecase_catalog.py`、
> `retriever/blast_radius.py`、`webapp/tools.py`、`webapp/db_readonly.py`、
> `webapp/incident_investigator.py`、`prompts/qa-system-prompt.md`）
> ＋ 内网 Codex 的分析文档《Ask 能力不退化、共享回答契约与 Agent 增强修复计划》。
>
> ⚠️ **内网代码已与外网快照分叉**：内网那份文档提到 `webapp/answer_gate.py`、
> `evidence_normalizer.py`、`context_pack.py`、`investigation_handoff.py`，这四个文件
> **不在 21 项清单里**，是内网自建的。本份文档所有锚点写成**契约与字段名**，不写行号；
> 需要内网先回报的部分集中在 [`RUNBOOK-83`](../../RUNBOOK-83-agent-ask-parity-recon.md)。

---

## 0. 一句话

内网的诊断对了三分之二：**三态被压成布尔**、**空结论算通过**这两条成立。
漏掉的第三条才是"结论：无"的直接原因 —— **证据拿不到出身标签，于是全部落成 gap**，
而这一条是 [`agent-mode-implementation-zh.md`](agent-mode-implementation-zh.md) §B2.0 那条规矩写错的，
责任在这边。

修法不是放松闸门，是**补一张证据出身表**。这张表说的全是我们自己工具的字段，
**不涉及内网环境里的名字、形状、格式 —— 这是本轮唯一不用等内网就能做完的事。**

---

## 1. 证据（事实与推断严格分开）

### 1.1 从告警实录截图直接读出的事实

| 读到什么 | 说明什么 |
| --- | --- |
| 6 个检索步骤 = `incident_impact` / `unified_impact`×4 / `search_code` | 计划里确实没有 `incident_investigate` |
| `incident 0/4` | 生产预算一次没动 —— 不是被拦，是没安排 |
| `auto_allowed_by_policy`、`access all_readonly` | **不是权限问题**，可以排除 |
| `112s/360s` | **不是超时**，可以排除 |
| `patches 2/5`、`已完成 Task: T1, T2, T2_F1` | 重规划真的触发了两次，触发器表是活的 |
| Gap 6 条，全部是 `... is unavailable or unknown in this result` | 两个不同状态被压成一句话 |
| **已确认事实：无** ＋ `no citations to verify` | 🔴 4 次工具调用、6 步检索，**一条事实都没进台账** |

最后一行是本轮的核心。它说明：**就算路由修好、`incident_investigate` 调上了，
本地那三个工具的证据照样会全军覆没。这是两个独立缺陷。**

### 1.2 从外网代码实测出的事实

出身字段（`environment` / `production_verified` / `tier` / `relation`）在整个
[`retriever/`](../../retriever) 里**只有 16 处、分布在 6 个文件**：

| 工具 | 出身字段现状 | 实测位置 |
| --- | --- | --- |
| `incident_investigate` | ✅ **每条证据都带** `environment: "production"` ＋ `source` | `webapp/incident_investigator.py`（≥10 处） |
| `db_query` | ✅ `environment` ＋ `production_verified: False` | `webapp/db_readonly.py` |
| `message_flow` | ✅ `environment: "dev/SCT"` ＋ `production_verified: False` | `retriever/messages.py` |
| `search_usecases` / `usecase_impact` / `source_system_impact` | ✅ 从 manifest 取 `environment`，缺则 `"unknown"` | `retriever/usecase_catalog.py`、`impact_report.py`（`source: manifest`） |
| `impact`（渠道块） | ✅ `relation` ＋ `confidence` ＋ `rank` ＋ `direct` ＋ `citation` | `retriever/channel_evidence.py` |
| **`unified_impact`** | ❌ 无 `environment`、无 `tier`；但**每个块带 `source`**（`EDGES_CSV` / `MESSAGE_EDGES_CSV`）＋ 一个 `citation_contract` | `retriever/unified_impact.py` |
| **`search_code`** | ❌ 返回的是**裸行列表**，无任何出身字段 | `retriever/code.py` |
| **`read_file`** | ❌ 只有 `path` / `line` / `lines`（可引用，但没标环境） | `retriever/code.py` |
| **`incident_impact`** | ❌ `parse_alert` 的 `environment` 初值是**空字符串** `""` | `retriever/incident.py` |
| `list_repos` / `critical_repos` / `hubs` | ❌ 无 | `repo_tags.py` / `criticality.py` / `graph.py` |

**那一轮用到的三个工具，全部来自"无出身字段"的这一批。**

### 1.3 已经存在、但被内网那份文档忽略的资产

🔴 **有序的证据词表已经在代码里，带配置钩子和测试** —— [`retriever/channel_evidence.py`](../../retriever/channel_evidence.py)：

```python
"relation_order": ["direct_code_evidence", "direct_config_evidence", "business_declared",
                   "name_derived", "message_carried", "transitive_dependency"],
"demote_low_confidence": True,
_BASIS_RELATION = {"code": "direct_code_evidence", "config": "direct_config_evidence",
                   "doc": "business_declared", "owner": "business_declared"}
OWNERSHIP_RELATIONS = frozenset({"direct_code_evidence", "direct_config_evidence",
                                 "business_declared", "name_derived"})   # transitive_dependency 故意不在
```

外加：`_rank()` 已经算出**整数序**、契约可由 `config/channel_evidence.json` 调、
`confidence` 的四个取值（`high` / `low` / `structural` / `derived`）在源码注释里有明确定义、
人类可读文案在 [`retriever/blast_radius.py`](../../retriever/blast_radius.py) 里。

**所以 Answer Packet 不该自己发明 `evidence_grade`。它该复用这一套。**

### 1.4 尚未被证据钉死的一条推断（需内网验证）

> **推断**：`add_evidence()` 因为拿不到 `tier` / `environment`，把每条证据都按 fail-closed
> 规矩落成了 gap，于是 `facts=0`、`claims=0`。

这条与截图完全吻合（6 条 gap 的措辞正是"某字段 unavailable or unknown"），
但**尚未看到台账里逐条的拒绝原因**。验证方法只有一条，写在
[`RUNBOOK-83`](../../RUNBOOK-83-agent-ask-parity-recon.md) 探针 2：把那一轮**每条被拒证据的原因原样打出来**。

**在这条回报之前，不要按本文档 §6.3 动 `add_evidence`。** 其余四处不依赖它。

---

## 2. 三个根因

### 2.1 三态被压成布尔（内网诊断成立）

`incident_investigation_required = false` 同时表示"确认不需要"和"我没看出来"。
本次是后者，Planner 当成了前者。

**补一条**：这个前置正则分类器**不在 21 项清单里**。清单 §B4 写的是规划器看到
**全部工具的名字 ＋ 一句话用途**，让它语义选择。内网在前面加了一道正则分类，
再叠上 §B5.1 的 `tool_subset`（按任务裁剪工具，这条是我们写的），
两个动作合起来的效果是：**模型看不见，也调不了。**

- **不改的后果**：同一个告警，ask 给处置结论，agent 给静态影响面。
  值班的人会得出"agent 模式不如普通模式"，而这个结论目前**是对的**。
- **改完的效果**：能力需求是三态；`unknown` 永远不许被读成"不需要"；
  `required` 的能力任何一层都不许裁掉。

### 2.2 证据没有出身标签，于是全部落成 gap（内网漏掉；本 spec 前一版的错）

原文（[`agent-mode-implementation-zh.md`](agent-mode-implementation-zh.md) §B2.0）：

> `add_evidence(...)` 强制带 `tier` 和 `environment`，**两者都必须来自工具结果里已有的字段**，
> 不接受调用方现编。取不到 → 记 gap，不许给默认值。

规矩的**意图是对的**（防的是"给对方字段安含义"这个已经犯过两次的错），
**但前提不成立：这些字段在多数工具里不存在**（§1.2）。于是 fail-closed 从"闸门"变成了"黑洞"。

而 ask 模式没有这个问题，因为**那些出身规则在 ask 里是散文，由模型每次现场应用**：

| ask 提示词的哪一句 | 说的是哪条出身规则 |
| --- | --- |
| 第 9 条："these routes come ONLY from a dev/SCT snapshot … never production" | 路由快照 = dev/SCT |
| 第 10 条："Always report the `environment` from the response's `source`/manifest envelope as-is" | 环境从 manifest 原样报，不许默认 |
| 第 12 条："`environment: uat` and `production_verified: false` — report them" | UAT ≠ 生产 |
| incident 那节："This tool reads local artefacts only — no logs, no AWS, no MCP" | `incident_impact` = 本地产物 |
| 渠道那张表 6 行 | `relation` 的完整词表与措辞 |

**规则一直在，只是写给模型看的，从来没有变成数据。** agent 模式要求它是数据。

- **不改的后果**：agent 模式对**所有**走 `unified_impact` / `search_code` 的问题
  （也就是"谁调用了 X"这类最常见的）都会答成"查了 6 步，结论：无"。这不是告警专属缺陷。
- **改完的效果**：闸门仍然 fail-closed，但"没标签"从黑洞变成
  **查产地表 → 表里也没有 → 落 gap 且明写『产地表缺 `<artifact>` 这一行』**。可运维，不是静默归零。

### 2.3 空结论天然算通过（内网诊断成立）

`accepted=0, rejected=0, valid=true`。

这个项目所有翻车的共同形状是"没查完的答案伪装成结论"；这次是它的**镜像**：
**查到了却伪装成什么都没有。** 同样有害 —— 用户会以为系统查过而且真的没东西。

- **不改的后果**：每一次部分成功都被四舍五入成失败，而且**没有任何痕迹告诉用户查到了什么**。
- **改完的效果**：`evidence > 0 && accepted_claims == 0` → `answer_status != complete`，
  并且**把已查到的 Evidence ＋ 每条 claim 的拒绝原因渲染出来**，不是只报一个状态字。

---

## 3. 定稿：三根轴，一个都不许合并

内网提的 `evidence_grade: "observed_runtime"` 是个**凭空多出来的第六档**，
和已有六档没有可比顺序 —— 而 `done_when` 里有 `evidence_tier >= <档>`（清单 §B1 受限词表），
**代码要能比大小**。

真正的问题是它**混淆了两件事**：生产日志证据不是"更高一档"，它是**另一个世界**。
"代码里确实调用了 X"和"生产在那一刻确实走到了 X"没有强弱关系，它们回答的是不同问题。

**所以定稿是三根正交的轴：**

| 轴 | 字段 | 取值（封闭） | 值从哪来 |
| --- | --- | --- | --- |
| **世界** | `environment` | `code` / `snapshot` / `uat` / `uat-export` / `production` / `user_supplied` | 工具包自带字段优先；没有则查产地表（§5） |
| **手段** | `kind` | `cited_source` / `cited_config` / `graph_derived` / `declared_business` / `name_derived` / `runtime_observed` / `query_result` | 同上 |
| **可核** | `citation` | 对象，见 §4.7 | 工具包 ＋ `retriever/citations.py` 校验 |

三条铁律：

1. **`rank` 只在同一个 `environment` 内可比。** 跨环境比较是非法操作，代码里不要提供这个入口。
2. **"它自己做"和"它会被连累"由 `domain_relation` 判，不由 `rank` 判。**
   渠道类结论必须原样携带工具返回的 `relation`，归属判定**直接调用现有的
   `channel_evidence.OWNERSHIP_RELATIONS`**，不在 packet 层重新实现。
   （ask 提示词渠道那节规则 1：用错的那个建出来的通知名单会点错团队。）
3. **只增一个新词表（`kind`），并用一张固定的 6 行桥表接到已有词表**，不许再造第五套：

   ```
   direct_code_evidence   -> cited_source
   direct_config_evidence -> cited_config
   business_declared      -> declared_business
   name_derived           -> name_derived
   message_carried        -> graph_derived
   transitive_dependency  -> graph_derived        # 且 domain_relation 原样保留
   ```

> ⚠️ **命名撞车必须一次解决。** 当前代码里 `tier` 在
> [`retriever/usecase_router.py`](../../retriever/usecase_router.py) 已经是 **delivery path 的层级**
> （MDC/HASE/Shared），`relation` 在 [`webapp/tools.py`](../../webapp/tools.py) 的 `_REL_ORDER` 是
> **7 种受影响仓库关系**、在 `channel_evidence.py` 是 **6 种渠道证据强度**，
> `confidence` 在 `outage_report.py` 是 `high|heuristic`、在 `arch_focus` 是 `declared-exact`、
> 在 `channel_evidence` 是 `high|low|structural|derived`。
> **Packet 里的字段一律用不撞的名字**：`evidence.kind` / `evidence.rank` /
> `evidence.domain_relation` / `evidence.environment`；`delivery_tier`、`repo_relation` 各归各。
> **禁止在 packet 里出现裸 `tier` 和裸 `confidence` 字段。**

---

## 4. Answer Packet schema 定稿（对内网 `answer-packet-v1` 的八处修正）

保留内网的整体骨架（`conclusions` / `evidence` / `unverified` / `recommendations` /
`intent_coverage` / `views` / `do_not_claim` / `executed_capabilities`）与
"`run.status` 与 `answer_status` 分开"这条。以下八处必须改。

### 4.1 `evidence[]` 三轴化（替换 `evidence_grade` + `confidence`）

```json
{
  "evidence_id": "E1",
  "statement": "SendCampaignEventService 调用了 publishIngressEvent",
  "environment": "code",
  "kind": "cited_source",
  "rank": 0,
  "domain_relation": null,
  "provenance": {
    "artifact": "codegraph_bundle",
    "source_field": "callers.items[0]",
    "as_of": "2026-08-07T11:24:28Z",
    "production_verified": false,
    "from_cache": false
  },
  "citations": [
    {"repo": "mc-hk-hase-api-campaign-core",
     "path": "src/main/java/.../SendCampaignEventService.java",
     "line": 51, "verified": true, "checked_by": "retriever.citations"}
  ]
}
```

- `environment` / `kind` / `rank` **不许留空、不许给默认值**；取不到 → 见 §6.3 的三步。
- `production_verified` 只能是工具包里那个字段的原值。**没有那个字段 ≠ `true`。**
- `from_cache: true` 的证据**不得被计为第二次确认**（清单 §A3 已定的规矩，这里落成字段）。

### 4.2 `conclusions[].claim_type` 封闭，且按类型必填

| `claim_type` | 必填字段 | 依据（ask 提示词） |
| --- | --- | --- |
| `code_location` | 至少一条 `citations[].line` 非空且 `verified: true` | 第 8 条："文件级引用**不是完成的工作**" |
| `count` | `count`（见 §4.3） | 第 11 条：数字必须来自工具的 `count` 字段 |
| `impact_set` | `domain_relation` 或 `repo_relation`，且 `ownership: true|false` | 渠道那节规则 1 |
| `routing` | `environment` ∈ {`snapshot`,`uat`,`uat-export`}，且措辞带快照限定 | 第 9 条："never rewrite '不在快照里' as '不存在'" |
| `ownership` | `owner_layer` ∈ {`business_owners`,`cost_governance`,`config_maintainers`} | 第 10 条：owner 是分层的，`business_owners` 领头 |
| `operational_decision` | `provenance.production_verified: true` | incident 那节：不许把影响面装成根因 |
| `mechanism` | 至少一条 `cited_source` 或 `runtime_observed` 证据 | incident 那节：趋势不是根因 |
| `status` | `environment` ＋ `as_of` | `db_query` / CSV 那节：快照是一个冻结的时刻 |

**未列入的 `claim_type` 一律拒绝**（不是降级，是拒绝）—— 否则渲染器和闸门无从判断。

### 4.3 `count` 类结论必须带口径（内网方案里一个字段都没有）🔴

这是这个项目最常翻的车：45 / 361 / 460 / 385 / 212 / 12 都是"对的数字"，差别全在口径。

```json
{"count": {"value": 361, "basis": "expression_ready", "universe": "sms_channel_rules",
           "is_upper_bound": false, "source_field": "use_cases.count",
           "total": 880, "truncated": true, "banner_verbatim": "<工具给的原话>"}}
```

- `is_upper_bound: true` → 渲染器**强制**用"至多 N"措辞（ask：`channel_upper_bound` 必须说成上界）。
- `banner_verbatim` 有值时**逐字输出**，不许改写（ask 第 10 条：`confidence_banner` wording verbatim）。
- `value` 只能来自 `source_field` 指的那个字段，**不许由 `items` 数出来**
  （ask 第 11 条：修过一次"答 22、实际 21"的 bug）。

### 4.4 `unverified[].cause` 用系统里现成的诚实词表（替换自由文本 `reason`）

```json
{"gap_id": "G1", "text": "这些 use case 的生产路由未知",
 "cause": "link_unavailable", "cause_field": "use_case_link.available",
 "who_can_close": "内网（需要同环境路由表）",
 "blocking_for": ["notification_list"], "suggested_capability": "db_query"}
```

`cause` 封闭词表（**全部来自已被真机验证过的字段**，不许新增自由值）：

```
link_unavailable        <- use_case_link.available: false
matched_zero            <- matched: 0
no_datapoint            <- points_seen: 0
record_not_found        <- record_found: false
query_not_wired         <- ok:false / state: not_wired
query_not_ready         <- state: not_ready
query_refused           <- state: refused
query_disabled          <- state: disabled
scope_unknown           <- scope_known: false
scanned_without_evidence
upper_bound_only        <- vendor_selection.method: channel_upper_bound
truncated               <- truncated: true
blocking_window_refusal <- plan.refusals 含 BLOCKING
callers_unavailable     <- callers.available: false
columns_dropped
placeholder_unfilled    <- retriever/glossary.py is_unfilled()
provenance_table_miss   <- 新增：产地表缺这一行（§6.3）
```

- 🔴 **`who_can_close` 必填。** 内网方案把清单 §5.3 的 `who_can_close_it` 丢了 ——
  那是 gap 最有用的一栏，"不知道"和"谁能让我们知道"是两回事。
- 截图里那句 `unavailable or unknown` 就是两个状态被压成一句话的产物；
  上表把它们分开，**并且每条 gap 必须指出是哪个字段这么说的**。

### 4.5 `citations[]` 是对象，不是裸字符串数组

必须带 `verified` ＋ `checked_by`。[`retriever/citations.py`](../../retriever/citations.py) 已经在做
`file:line` 存在性校验 —— 结果必须进 packet，否则占位符和真引用分不开。

**渲染硬规则**：`verified: false` 的引用**不许当引用输出**，只能作为一条
`cause: placeholder_unfilled` 或 `provenance_table_miss` 的 gap。
（内网 §9.4 说要洗引用，但 schema 里没有落点。）

⚠️ 已知缺口：`citations` 的后缀白名单**漏了 `.js` / `.groovy`**（2026-08-07 实测），
引用守卫会被整个跳过。这一条独立修，不要塞进本轮。

### 4.6 `answer_status` / `rank` / `confidence` 由**代码**算，模型不许写

- packet 分两段：模型写的部分，和一个 `_derived` 块。
  **模型输出里出现 `_derived` 的任何键 → 整个 packet 拒收**（不是忽略，是拒收）。
- `_derived` 含：`answer_status`、每条结论的 `strength`、`accepted/rejected` 计数、`intent_coverage[].status`。
- `strength` 由**支撑证据里最弱的那一条**推出（跨环境时取"最不确定的世界"），模型**只能下调不能上调**。
- 否则模型自己写 `answer_status: complete`，闸门只能事后追 —— 这就是内网 §10 在补的洞，
  但洞的根在 schema 没分清谁写哪个字段。

### 4.7 `claim_key` 去重键 ＋ `views[]` 定形

- `claim_key = (subject, predicate, object)`。用于：合并规则里"同一件事的多个理由算一条，
  最强的领头、其余作为支撑"（ask 渠道那节规则 4），以及台账的
  `append_unique(key_fn)`（清单 §B2.1）。没有它，"合并不冲突的 Claim"无法确定性执行。
- `views[]` 定形：`{"kind": "arch|impact|coverage", "value": "sms", "count": 12, "truncated": false}`。
  渲染器**按标记插入**，并继承 ask 的两条硬规则：
  **模型看不见那张图的内容**（只能用工具返回的数据作答）、**不许旁白**
  （不写"图已插入"、不写 HTML 注释）。内网自己承认 agent 模式"只保留文字摘要"——
  根因就是 `views: []` 是个没形状的空数组。

### 4.8 `do_not_claim` 从散文改成规则引用

现在是几句中文散文，闸门查不了，等于装饰。改成：

```json
{"do_not_claim": [{"rule": "name_derived_is_not_ownership", "from_claim": "C3"}]}
```

规则由 `claim_type × kind × environment` 的固定表推出（例：`kind: name_derived`
永远不能支撑 `claim_type: ownership`；`environment: code` 永远不能支撑
`claim_type: operational_decision`）。**散文只留给人看，不留给闸门判。**

---

## 5. `config/evidence_provenance.json` —— 产地表（外网填，内网只审）

**设计要点：按产物建，不按工具建。** 因为 [`unified_impact`](../../retriever/unified_impact.py)
的每个块**已经在 `source` 字段里报出它来自哪个产物**
（`dependency_edges.source = EDGES_CSV`、`message_edges.source = MESSAGE_EDGES_CSV`），
[`impact_report`](../../impact_report.py) 报 `source: manifest`。**连接键已经在包里了。**
好几个工具共享同一个产物，所以这张表只有十来行，不是 17 × N 行。

```json
{
  "_README": [
    "证据出身表：一条证据来自哪个产物 -> 它属于哪个世界、用什么手段建立的、时刻取哪个字段。",
    "键是产物（artifact），不是工具。多个工具共享一个产物是常态。",
    "匹配顺序：① 工具包自带 environment/production_verified 字段 -> 原样用，本表不参与；",
    "         ② 包里的 source/artifact 字段命中本表 -> 用本表；",
    "         ③ 都没有 -> 落 gap，cause=provenance_table_miss，并把缺的 artifact 名写进 gap。",
    "留 \"?\" = 没填 = 走 ③，绝不给默认值。",
    "本文件是被 git 跟踪的模板；盒子上改 config/evidence_provenance.local.json（自动优先读）。",
    "relation_bridge 是固定的 6 行桥表，接到 retriever/channel_evidence.py 已有的词表 —— 不要改它。"
  ],

  "relation_bridge": {
    "direct_code_evidence": "cited_source",
    "direct_config_evidence": "cited_config",
    "business_declared": "declared_business",
    "name_derived": "name_derived",
    "message_carried": "graph_derived",
    "transitive_dependency": "graph_derived"
  },

  "artifacts": {
    "mirror": {
      "environment": "code", "kind": "cited_source",
      "citable": true, "line_required": true,
      "as_of_from": "index_generated_at",
      "used_by": ["search_code", "read_file"],
      "note": "本地只读镜像。可精确引用，且 code_location 类结论必须带行号。"
    },
    "codegraph_bundle": {
      "environment": "code", "kind": "cited_source",
      "citable": true, "line_required": true,
      "as_of_from": "codegraph_manifest.generated_at",
      "used_by": ["unified_impact.callers", "call_graph"],
      "note": "跨仓调用图。常只给到文件级 -> 必须自己把行号解析出来再作为 code_location 用（ask 提示词第 8 条）。callers.available:false -> gap cause=callers_unavailable。"
    },
    "index/internal_edges.csv": {
      "environment": "code", "kind": "cited_config",
      "citable": false, "line_required": false,
      "as_of_from": "index_generated_at",
      "used_by": ["impact", "hubs", "unified_impact.dependency_edges"],
      "note": "从 pom 收割的依赖声明。是 declared dependency，不是运行时观测。"
    },
    "index/message_edges.csv": {
      "environment": "code", "kind": "?",
      "kind_from_row": {"field": "routing_source",
                        "map": {"?": "?"},
                        "fallback": "graph_derived"},
      "citable_from_row": "evidence",
      "as_of_from": "index_generated_at",
      "used_by": ["message_flow", "unified_impact.message_edges"],
      "note": "🔴 需内网确认：routing_source 的真实取值有哪些（RUNBOOK-83 探针 3d）。有 evidence 列 = repo/path:line 时按 cited_source，否则 graph_derived。"
    },
    "index/tbl_event_router_usecase_topic.snapshot.csv": {
      "environment": "snapshot", "kind": "cited_config",
      "citable": false, "production_verified": false,
      "as_of_from": "?",
      "used_by": ["usecase_routing", "message_flow"],
      "note": "dev/SCT 路由快照。messages.py 已自报 environment='dev/SCT'，所以正常走匹配①。措辞必须带快照限定：不在快照里 != 不存在。"
    },
    "index/usecase-snapshots/active": {
      "environment": "from_manifest", "kind": "declared_business",
      "citable": false, "production_verified": false,
      "as_of_from": "manifest.exported_at",
      "used_by": ["search_usecases", "usecase_impact", "source_system_impact", "usecase_quality_findings"],
      "note": "环境从 manifest 原样取，缺则 unknown（ask 提示词第 10 条），绝不默认 UAT。"
    },
    "index/channel_evidence.json": {
      "environment": "code", "kind": "from_relation_bridge",
      "citable_from_row": "citation",
      "as_of_from": "generated_at",
      "used_by": ["impact.channels", "list_repos", "show_arch"],
      "note": "原样携带 relation / rank / direct；归属判定调用 channel_evidence.OWNERSHIP_RELATIONS，不要在 packet 层重算。"
    },
    "index/repo_tags.json": {
      "environment": "code", "kind": "name_derived",
      "citable": false,
      "as_of_from": "generated_at",
      "used_by": ["list_repos", "critical_repos"],
      "note": "🔴 name 家族命中默认是 name_derived；只有 mdc_common 这类来自业务表的标记才是 declared_business。两者在同一个文件里，必须按字段分，不能按文件分。"
    },
    "alert_text": {
      "environment": "user_supplied", "kind": "query_result",
      "citable": false, "production_verified": false,
      "as_of_from": "user_message_time",
      "used_by": ["incident_impact", "incident_investigate.parse"],
      "note": "用户粘的告警原文。可以说『告警文本里含 Connection reset』，不可以说『已确认下游网络故障』。incident.parse_alert 的 environment 初值是空字符串 —— 那个字段是告警里解析出的环境标签，不是证据的环境，别拿它当匹配①。"
    },
    "mcp:logdream": {"environment": "production", "kind": "runtime_observed",
                     "citable": false, "as_of_from": "window",
                     "used_by": ["incident_investigate"],
                     "note": "包里已自带 environment='production'，正常走匹配①。本行只作兜底。"},
    "mcp:cloudwatch": {"environment": "production", "kind": "runtime_observed",
                       "citable": false, "as_of_from": "window",
                       "used_by": ["incident_investigate"], "note": "同上。"},
    "mcp:portal": {"environment": "production", "kind": "query_result",
                   "citable": false, "as_of_from": "window",
                   "used_by": ["incident_investigate"],
                   "note": "record_found:false 是查询结果，不是投递确认。"},
    "db:uat": {"environment": "uat", "kind": "query_result",
               "citable": false, "production_verified": false, "as_of_from": "query_time",
               "used_by": ["db_query"], "note": "包里已自带，正常走匹配①。"},
    "csv:uat-export": {"environment": "uat-export", "kind": "query_result",
                       "citable": false, "production_verified": false,
                       "as_of_from": "dataset.as_of",
                       "used_by": ["dataset_query"],
                       "note": "as_of 必须出现在答案里（清单 §C2）。undocumented_columns 的列只能引用值、不能解释含义。"}
  }
}
```

**这张表的三个规矩：**

1. **匹配顺序不许调**：工具自带字段 > 产地表 > gap。产地表**永远不能覆盖**工具自己说的话。
2. **`"?"` 一律走 gap**，不给默认值 —— 占位符判定复用
   [`retriever/glossary.py`](../../retriever/glossary.py) 的 `is_unfilled()`，不写第二道闸门。
3. **表里没有的 artifact → `cause: provenance_table_miss` 并把 artifact 名写进 gap 文本。**
   这样"没标签"是一条**可运维的待办**，不是一个黑洞。

上表除三处标 🔴 / `"?"` 的以外，其余都是从外网代码实测推出来的，可以直接用。

---

## 6. 五处最小修复（P0，能救这个 case，都在现有文件里）

> 顺序即依赖顺序。**6.3 要等 RUNBOOK-83 探针 2 回报后再动**，其余四处不依赖它。

### 6.1 删掉前置正则分类器，换成三态

- 删掉 `_incident_investigation_required(question) -> bool` 这类**布尔前置分类**。
- 换成一个结构化 Intent（内网 §6 的 `intent-v1` 骨架可用），
  三态 `required | forbidden | unknown`，且 **`unknown` 不得被下游读成 `forbidden`**。
- 规划器恢复"看全部工具的名字 ＋ 一句话用途"（清单 §B4 本来就这么写的）。
- **完成判据**：本次 `Connection reset` / `TERR_30020` 告警 → `incident` 域、
  `required: incident_investigate`；"只看影响谁，不查日志" → `forbidden`；
  纯代码/Use Case 问题 → 不进 incident。

### 6.2 `required` 能力任何一层都不许消失

- Plan 校验新增三个码：`missing_required_capability` / `forbidden_capability_selected` /
  `non_substitutable_capability_replaced`。
- 🔴 **`tool_subset` 的裁剪必须永远包含 `required` 能力** ——
  清单 §B5.1 的"每次调用 ≤5 个工具"这个硬指标**不变**，
  但 required 能力占的那个位置**不参与裁剪预算**。
- 修不掉 → 一次 repair → 仍不行 → 回落到控制面生成的最小 baseline Plan，或明确 `blocked`。
  **绝不静默换成较弱的本地替代**（本地 `incident_impact` 不是生产诊断）。

### 6.3 产地表 ＋ `add_evidence` 三步匹配（等探针 2）

按 §5 建表；`add_evidence()` 改成三步匹配；取不到时落 gap 且 `cause=provenance_table_miss`。
**闸门仍然 fail-closed，改的只是"没标签"的去处。**

### 6.4 闸门不变式 ＋ 把查到的东西渲染出来

- `evidence > 0 && accepted_claims == 0` → `answer_status ∈ {partial, blocked}`，
  且**答案里必须出现**：查到了哪些 Evidence、哪些 claim 被拒、**逐条拒绝原因**、下一步谁能闭合。
- repair 质量比较**不许只比 `rejected` 数量**。至少比：accepted 数、已覆盖的 deliverable、
  baseline claim 是否还在、qualifier 是否还在、是否新增了不可核引用、是否把 complete 变成 empty。
  **零拒绝零接受不优于部分接受部分拒绝。**
- 空 claim 只有四种情况可接受：工具确实没返回相关证据 / 所有能力被明确拒绝 /
  用户只问执行状态 / 输出的是明确的 blocked。**其余一律触发 `empty_repair_erased_supported_facts`。**
- 部分 claim 失败时**保留已通过的**，失败的转成 gap（内网 §10.4，收）。

### 6.5 Ask-vs-Agent 同题不退化评测（**必须与上面同批上，不许延后**）

否则这次修完，下个月换个形状再来一次，而且没有任何东西防着。

每类能力至少一条（10 类见内网 §12.1），同一个 query 跑两个模式，断言：

| 断言 | 判什么 |
| --- | --- |
| `agent_keeps_required_capability` | ask 用到的 required 能力，agent 也用到了 |
| `agent_keeps_core_claims` | ask 已确认的核心 claim 在 agent 答案里仍在（**语义一致，不要求逐字**） |
| `agent_keeps_views` | ask 出了 inline view，agent 也出 |
| `no_silent_claim_deletion` | agent 删/降 claim 必须有新证据 ＋ 可审计原因 |
| `no_environment_rewrite` | agent 不许把 snapshot/code 结论改写成 production 结论 |
| `answer_not_empty_when_evidence_exists` | 有证据就不许答"无" |

**回归红线不变**：现有 39 条在 `mode=ask` 下逐条不变。
并记住 RUNBOOK-66 的教训：**用例红了，第一嫌疑人是断言，不是模型。**

---

## 7. 对内网那份文档的取舍

### 7.1 收（照单全收）

1. **三态取代布尔**（§2.2）—— 和这个项目"unknown 不许读成 no"的老规矩同形。
2. **空 claim 不再天然有效** ＋ repair 质量比较（§10.1–10.4）。
3. **`run.status` 与 `answer_status` 分开**，前端优先显示 `answer_status`（§10.5）。
4. **Evidence Provider 与 Answer Provider 分开**（§8.3）—— 这条说得比我们清楚：
   低层工具只交"查到了什么"，只有 baseline builder / specialist 才产出结论。
5. 🔴 **决策 3：Planner 的输入应包括 baseline answer 和未闭合 gap，而不是从零重新解释用户问题。**
   **这一条比我们原 spec 好，认。** 原 spec 把目标分解整个交给 Planner，
   那正是他们 §2.3 指出的"Planner 可以重新定义用户目标"。
6. **决策 4：只粘告警、没有显式问题 → 默认进生产事故分诊/处置。**
   同意，**但补一句：默认进处置 ≠ 默认打生产** —— 清单 §B6 那道
   "含生产调用才要批准"的闸门一个字不动。

### 7.2 改造后才收

7. **共享 Intent/Capability Router**（他们的 Phase 1）—— 方向对。
   但**先做 §6.1 那一处删除**就能救这个 case；抽成共享模块排在后面，
   不要把"删掉一个不该存在的前置分类器"和"新建一个共享路由模块"绑成一件事。
8. **Baseline Answer Pipeline**（Phase 2）—— 收，但实现上只有一个要求：
   **不是先跑一遍 ask 再规划**（延迟翻倍、生产查询翻倍），是同一段 pipeline 跑一次，
   ask 在拿到首个 Answer Packet 后停，agent 从同一个 Packet 继续。他们 §5 已经这么写了，
   实施时要有测试盯住"同一个工具结果只执行和归一化一次"。
9. **Answer Packet**（Phase 2/4）—— 骨架收，八处字段按 §4 改完再写代码。
   **schema 先定稿，再动键盘** —— 这个项目最贵的教训全在"字段的含义被安错"这条线上。

### 7.3 缓（等 P0 的真实数据）

10. **Claim Ledger 的完整 lineage / superseded 关系**（Phase 4）——
    台账只追加 ＋ `supersede` 已经在清单 §B2.1 定了；完整 lineage 面板等 §6 五处上线后再看值不值。
11. **前端 Claim lineage 与路由展示**（Phase 6）—— P2，不挡功能。

### 7.4 不做

12. **六个特性开关同时上**（他们 §14）—— 砍到两个：
    `SDLC_AGENT_PROVENANCE_TABLE_ENABLED`、`SDLC_AGENT_STRICT_ANSWER_COMPLETION_ENABLED`。
    六个开关 = 2⁶ 种组合，没人测得过来，而且**灰度期间线上到底跑的是哪一种没人说得清**。
13. **为每一种错误文本继续追加正则**（他们自己也列为非目标）—— 一致。
14. **`do_not_claim` 继续用散文**（§4.8）。

---

## 8. 验收标准

1. 本次告警在 agent 模式下 `incident_calls >= 1`（除非用户明确只问影响面或访问策略阻断）。
2. **`facts / evidence > 0` 时，`已确认事实：无` 不允许出现。**
3. `unified_impact` / `search_code` 的证据能成为 fact —— 即产地表在真机上真的接上了。
4. 每条 gap 能说出**是哪个字段这么说的**（`cause_field`）**和谁能补上**（`who_can_close`）。
5. 部分 claim 被拒时，已接受的 claim 正常输出。
6. `count` 类结论带口径；上界一律说成"至多 N"。
7. `transitive_dependency` 不被说成归属；`name_derived` 不被当证据。
8. 非 Alert 的代码 / Use Case / 消息路由问题不会被错误送进 incident specialist。
9. 现有 39 条 eval 在 `mode=ask` 下逐条不变；§6.5 的同题不退化用例全绿。
10. 所有生产 access / approval / scope / time-window / MCP allow-list / 脱敏测试继续通过。
11. 不新增第三方依赖；不改 `webapp/llm.py` facade；不持久化原始生产日志。

---

## 9. 需要内网回报的（→ [`RUNBOOK-83`](../../RUNBOOK-83-agent-ask-parity-recon.md)）

1. **那一轮台账里每条被拒证据的原因**（验证 §1.4 的推断）—— 最重要的一条。
2. `answer_gate.py` / `evidence_normalizer.py` / `context_pack.py` / `investigation_handoff.py`
   这四个自建文件的**契约**（入口函数签名 ＋ 它们各自认哪些字段）。
3. 21 项清单 A1–E2 **实际做到哪一项**。
4. `index/message_edges.csv` 的 `routing_source` **真实取值有哪些**（产地表还差这一行）。
5. 五个工具在真机上的**原样返回包**（脱敏后），用来核对产地表每一行。

---

## 10. 需要业主拍板的

1. **范围**：先上 §6 那五处（能救这个 case），还是直接开内网的六个 phase？
   建议前者，且 §6.5 的评测**必须同批**。
2. **产地表谁维护**：建议**外网填、内网只审** —— 它说的全是我们自己工具的字段。
   这是本轮唯一不用等内网的事。
3. **`blocked` 时给什么**：建议不只报状态，**把 gap 清单当答案的一部分交出去**
   （"我查到了 A、B，C 查不到是因为某字段说不可用，能补上的是谁"）。
4. **不退化的比较口径**：建议**语义不退化，不要求逐字一致**（内网决策 1，同意）。

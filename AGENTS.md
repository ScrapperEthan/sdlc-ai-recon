# AGENTS.md —— 本项目的 AI 分工与边界

> **任何接手本项目的 AI 请先读这一页。** 这里定义"谁能改什么",违反边界会造成两边互相覆盖。

**TL;DR (English).** Two owners. The **intranet Codex** owns **data ingestion** — the adapters and
config knobs that read business tables. **Claude / the external side** owns the **engine** — retrieval,
tools, prompt, frontend, specs. The contract between them is the **generated artifact files**, not
function calls. Never cross the line; never commit real data.

---

## 0. 这个项目是什么

MDC 消息平台的 AI 助手。索引约 460 个代码仓库 + UAT 业务用例数据,回答"改这里会影响谁""这条通道挂了
谁受影响",**每条结论带代码出处**。详细现状见 `PROJECT-STATE.md` 与 `docs/DEMO-SCOPE-AND-PLAN-zh.md`。

---

## 1. 两个所有者

| | **内网 Codex** | **Claude / 外部** |
| --- | --- | --- |
| 负责 | **数据摄取**:业务表怎么读 | **引擎**:读到之后怎么用 |
| 触发它干活的事件 | 数据/表结构/仓库命名变了 | 想让助手多会一件事 |
| 典型工作 | 列改名、加渠道列、加 flag、取值域变化、新厂商 | 检索逻辑、工具、prompt、前端、链路图、写 spec |
| 能推到哪 | **内网 github**(推不了公网) | **公网 github** |

**判断归属只看一件事:什么事件会让这个文件需要改。** 不看它是数据还是代码。

---

## 2. 文件级边界(最重要的一节)

### 内网 Codex 可以改

| 文件 | 说明 |
| --- | --- |
| `config/*.json` | **主战场**。列映射与词表(列名别名、渠道、厂商名单、生产者种子、业务枚举)。99% 的数据变化只改这里。 |
| `enrich_repo_tags.py` 的解析部分 | 引擎,schema 已做成灵活的,一般**不需要**改 |
| `build_usecase_db.py`(待建) | UAT 三张表 → SQLite |

### 内网 Codex **不要动**

- `enrich_repo_tags.py` 里的 `reconcile()` / `markdown_report()`(消费侧 QA,文件里已加横幅标注)
- 下面 Claude 那一栏的任何文件
- **产品运行时代码(MDC 的 Java)** —— 发现问题只**报告**,不修。那不是本项目的范围。

### Claude / 外部可以改

`retriever/*`、`webapp/*`、`mcp_server.py`、`retrieval_service.py`、`cli.py`、`impact_report.py`、
`outage_report.py`、`make_*.py`、`prompts/*`、`static/*`、`docs/specs/*`、`refresh.py`、`.gitignore`

---

## 3. 产物契约(**谁都不许破**)

两层之间靠**文件**对接。这些结构必须稳定,只增不减:

| 产物 | 硬契约 |
| --- | --- |
| `index/repo_tags.mdc.json` | 前 6 个字段:`mdc_common`、`time_critical`、`marketing_servicing`、`mode_declared`、`business_line`、`channel_declared`。`flags`/`attrs` 可增不可减 |
| `index/mdc_roster.json` | `{source, count, repos}` = 权威 in-scope 名册 |
| `index/message_edges.csv` | **前 5 列**是契约(`retriever/messages.py` 直接读) |
| 各 API 响应 | 保留 provenance 信封 `{environment, snapshot_id, source_tables, production_verified:false, citations}` |

细节见 `docs/MDC-SHEET-CODEX-HANDOFF-zh.md` 第 4 节。

---

## 4. 两个仓库 —— 这决定了为什么分工能成立

- **公网仓库**(`ScrapperEthan/sdlc-ai-recon`)= 引擎 + **安全的默认配置**。Claude 维护并推送。
- **内网仓库**(内网 github)= **真实配置值 + 真实数据**。内网 Codex 维护并推送。
- 内网 Codex **推不了公网**,所以真实值天然只留在内网 → **合规上更干净**,而且有内网 git 存档,**不会丢**。
- 两边文件不同名/不同位置 → **从公网 pull 永远不会冲突**。

---

## 5. 硬约束(违反 = 事故)

1. **只用 Python 标准库。** 环境是隔离网,装不了第三方包。
2. **对镜像与快照只读。** 只往 `index/`(已 gitignore)写产物。
3. **绝不提交**:真实仓库名册、表行数据、人名、`Remark` 之类自由文本。`index/*.json` 被 gitignore 是
   **故意的安全管控**,不要试图解除。
4. **不改产品运行时代码。** 发现风险 → 写进报告/问题单,交给 MDC 负责人。
5. `llm.py` 是门面(facade),合并时保持接口不变。

---

## 6. 摄取引擎的四条铁律(每个 `config/*.json` 都一样)

1. **文件缺失 → 回落到 Python 内置默认值,不崩。**
2. **认不出的列/取值 → 进例外报告,不崩、不静默丢弃。**
3. **块级替换,不是深合并。** 覆盖某块要给整块。
4. **`_README` 键被忽略**,所以每个配置文件里都写着自己的用法。

---

## 7. 怎么回报(只回传一个文件)

跑完 `python refresh.py` 后,回传 **`index/reports/INGESTION-EXCEPTIONS.md`** —— 未绑定的列、
解析成 `unknown` 的厂商/渠道、行数突变、本地 override 的存在性、还在等业主答案的空位。

**干净时它就是一堆空章节** = 这次不用找 Claude。**不要回传原始数据。**

---

## 8. 不许猜业务语义(这条最容易犯)

以下几件事**没有人有权替业务方决定**,默认必须保持 `unconfirmed`,只**标记矛盾**、不判定对错。
已知这些数据源**互相矛盾**(rule_text 与 channel_rule 优先级、与前台页面三方不一致),且现行运行时解析
逻辑本身有 bug —— 所以**既不能硬编码语义,也不能把运行时当成事实基准**。

| 事项 | 状态 | 配置位 |
| --- | --- | --- |
| `rule_text` 的 `>` / `&` / `\|` 含义 + rule_text 与 priority 谁优先 | ✅ **业主已确认 2026-07-27** | `config/rule_text_semantics.json`(已填) |
| `source_system` 的 eAlert 系列归并 | ✅ **业主已确认 2026-07-27** | `config/source_system_aliases.json`(已填) |
| `business_category` **33** / **37** 的分类名称 | ⏳ **待办**:该字段疑似已迁到 `tbl_use_case_router` 表,拿到表后才能确认 | `config/business_enums.json`(未建) |

**已确认的内容不要再改**,除非业主推翻。**未确认的不要猜** —— 代码会 fail closed(见 `_MEANINGS`:
配置里写了一个解释器没实现的含义,解释功能会自动关闭,而不是按旧假设硬跑)。
待问清单与数据出处见 `docs/OWNER-QUESTIONS-zh.md`。

---

## 9. 去哪找上下文

| 想知道 | 看 |
| --- | --- |
| 整体进度 / 能演什么 | `PROJECT-STATE.md`、`docs/DEMO-SCOPE-AND-PLAN-zh.md` |
| 分工的完整说明 + 指令模板 | `docs/INGESTION-CODEX-HANDOFF-zh.md` |
| MDC 表专项 | `docs/MDC-SHEET-CODEX-HANDOFF-zh.md` |
| 待业务方回答的问题(含数据出处) | `docs/OWNER-QUESTIONS-zh.md` |
| 各功能的可构建设计 | `docs/specs/*.md` |

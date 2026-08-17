# MDC 领域知识层 —— 方向性实施说明

> **给谁看**：内网 Codex（建）、业主（决策）、我们自己（验收）。
> **日期**：2026-08-17
> **触发**：拿到两样新东西 —— ①`MDC_Message_System_Common_Knowledge_Refined.md`（从
> `Use Case Metadata_v.04_20260810` 提炼的概念说明）；②五张 **生产** 表
> （`tbl_use_case` / `tbl_use_case_channel_rule` / `tbl_use_case_config` /
> `tbl_use_case_router` / `tbl_gov_request_form`）+ `Use_Case_Metadata.xlsx`。
> 内网 Codex 已出摄取设计（`PROD-USECASE-COMMON-KNOWLEDGE-INGESTION-DESIGN-20260817`），
> **本文不重复它**，只补它没覆盖的三件事：怎么用、先做哪个、以及哪些地方我不同意。

---

## 0. 一页结论

1. **最大的价值不是"助手多知道了一些概念"，而是文档里有一批可以直接拿生产数据去核对的规则。**
   文档说"高风险消息必须支持双厂商"，生产表里 `high_risk_flag` 和 `support_dual_vendor` 两列都在。
   把这句话变成一条体检项，就能直接列出"声明了高风险、但没开双厂商"的 use case —— 这份清单目前
   没有任何人手里有。这才是"充分运用"。
2. **概念知识不要整篇塞进提示词。** 提示词只放"怎么判断、怎么选证据、哪些不能猜"，正文进可检索层。
   理由不是省 token，而是那篇文档里**混着 TBC、已停用字段、和一组看起来像生产配置的示例百分比**
   （HTCL 70% / CSL 30%）。整篇塞进去 = 迟早会有一轮把示例当成现行政策说出去。
3. **生产数据先不要设成默认。** 这批导出**没有 `tbl_use_case_ext`**，而 `rule_text`、`endpoint`、
   owner 全在那张表上。直接切过去，现在能答的"改 PEGA 要通知谁"会突然答不了 —— 看起来像助手坏了。
4. **顺序上我建议先做知识层（不依赖任何还没到的数据，也碰不坏在跑的东西），生产摄取按内网设计的
   candidate → QA → canary → promote 慢慢走。**

---

## 1. 四层，各自的权威等级（这一层分不清，后面全白做）

| 层 | 内容 | 它能证明什么 | 它**不能**证明什么 |
|---|---|---|---|
| **提示词** | 心智模型、证据分流规则、禁令 | 怎么判断 | 任何具体事实 |
| **领域知识检索** | Common Knowledge 正文（按小节切块） | MDC 世界里这个概念是什么意思 | 现在配的是什么 |
| **字段词典** | workbook `reference` 页：语义 / 取值 / B·D·M·S / 派生规则 | 这个字段被定义成什么 | 这批导出里有没有这列、有没有这个值 |
| **生产快照** | 五张 PROD 表 | 这个版本里实际配了什么 | 当时那条消息有没有真的发出去 |

**铁律（沿用我们已经踩出来的教训）**

- `environment=PROD` + `production_verified=false` 是**两件事**：来源是生产导出 ≠ 线上实时事实。
  不能因为后者是 false 就改口叫 UAT，也不能因为前者是 PROD 就说"生产验证过"。
- **文档有定义 ≠ 快照里有这列**。这条要能被单独回答（见 §4 的 `present_in_active_snapshot`）。
- **TBC / 已停用 / 文档示例**三类内容，任何时候不得以"现行规则"的口吻出现。
- 四类来源冲突时**报冲突、显示来源、不替业主选边**（AGENTS.md §8 已经定过）。

---

## 2. 工作包与建议顺序

内网设计文档分了 A~E 五个包。我建议的**执行顺序**和它默认的顺序不同，理由写在后面：

| 顺序 | 包 | 依赖 | 为什么排这里 |
|---|---|---|---|
| **1** | **K1 字段词典**（workbook → field catalog） | 无 | 唯一可能**当场解掉三个卡了一个多月的老问题**（见 §4） |
| **2** | **K2 概念知识 + 易混对照卡** | 无 | 碰不到快照、跑不坏现有功能，纯增量 |
| **3** | **K3 提示词最小改动 + 知识工具** | K1/K2 | 让前两个真的被用上 |
| **4** | **K4 规则体检**（文档规则 × 生产数据） | K1 + PROD candidate | 本轮**新增能力**，价值最高但要先有 candidate |
| **5** | 内网 A/B/E（PROD candidate / 诚实降级 / canary·promote） | 数据问题 | 卡在 ext 表、gov 连接键、同批次导出上 —— 不该挡住上面四个 |

**为什么知识层先做**：它不依赖任何还没拿到的表，也完全不动 `index/usecase-snapshots/active/`，
最坏情况是"新工具没人调用"，不会让现在能答的问题变成答不了。而 A/B/E 每一步都在动正在跑的数据面。

---

## 3. K2：概念知识不要只做"按标题切块"

内网设计建议按 heading 切块 —— 对，但**不够**。那篇文档 30 多个小节里，真正会被问到、而且我们
**已经答错过**的，是那几组"长得像同一件事、其实不是"的概念。所以在按标题切块之外，另建一组
**易混对照卡**（`concept_pairs`），每张卡固定三段：**A 是什么 / B 是什么 / 分不清会怎么错**。

初版建议这 8 张（都能在文档里找到出处）：

| 卡 | 分不清会犯的错 |
|---|---|
| bounce-back ↔ resend | 把"没收到回执转下一个渠道"说成"系统失败后重发" |
| SLA ↔ SLO | 把业务期望值当成系统算出来的投递路径结果 |
| channel ↔ router ↔ vendor | 把"走哪个渠道"和"走哪家厂商"混成一句 |
| use case ↔ template ↔ message request | 拿 request/setup 视角的字段去回答 use case 的归属 |
| opt-in flag ↔ 渠道清单 | **我们已经犯过**：`sms_optin_flag=Y` 被读成"这个用例发短信" |
| high_risk ↔ regulatory ↔ dual_channel | 三个不同的合规概念被压成"重要消息" |
| priority ↔ rule_text | 业主已确认 rule_text 权威、priority 仅空时兜底 —— 别再自己排序 |
| delivery_mode ↔ messaging_service_level | 一个是紧急程度，一个是服务档位 |

每张卡带 `authority: derived_explanation`、`operationalizable: false`、和文档里的行号定位。

---

## 4. K1：字段词典的真正用途 —— 它是**我们自己硬编码语义的校验器**

`retriever/usecase_catalog.py` 里现在硬编码着一批业务语义，来源是**业主转述的照片**：
`BUSINESS_CATEGORY_DICTIONARY`（停在 7）、`BUSINESS_CATEGORY_CODE_ONLY`（来自 Java 枚举）、
`SEND_MODE_ENUM`（0 至今不明、903 行挂 pending）。而 workbook 的 `reference` 页带
**options / derive rules / based-on fields / B·D·M·S**。

所以字段词典的第一顺位任务**不是"回答字段是什么意思"，是拿它去核对我们已经写死的东西**。
三个卡了很久的问题，答案可能就在已经拿到的这份 workbook 里：

| 老问题 | 现在的状态 | workbook 可能给出的 |
|---|---|---|
| `send_mode = 0`（903 行） | 挂 pending，等业主 | `options` 列是否列了 0 |
| `business_category` 33 / 37 | 字典和 Java 枚举**都没有** | `reference` 或 `Reference_Data` 页是否有 |
| `delivery_path` 数字 | 提示词里明写"我们没有名称映射" | 是否有 code→name |

→ **这三条是 RUNBOOK-81 的头三个问题。** 如果 workbook 能答，我们等于用一份已经在手的附件
解掉了三张"等业主"的单子；如果答不了，那也是个结论（"文档也没定义"比"我们没查过"强得多）。

**字段词典每条记录必须带的三个轴**（前两个内网设计已有，第三个是我加的）：

```
documented_in_metadata : 文档里定义了吗
present_in_active_snapshot : 当前这份导出里有没有这列
observed_values_status : 数据里出现的值，是否都在文档列出的取值范围内
```

第三个轴是**漂移探测器**：文档说取值是 {A,B,C}，生产数据里出现了 D → 这是一条要给业主的发现，
不是我们自己去猜 D 是什么。**只对低基数的枚举列做**，自由文本列（`remarks`、`template_description`、
人名）一律不进这条通道。

---

## 5. K3：提示词改什么（可直接贴的文本）

**只加下面这一块**，放在现有"Read the intent, then pick the altitude"之后。不加字段清单、不加
厂商名单、不加百分比、不加当前用例数量、不加 owner。

```markdown
## MDC 领域知识 —— 概念在文档里，事实在快照里

**最小心智模型：** 业务场景 → 上游系统发请求 → MDC 识别 use case → 套渠道/模板/路由/厂商策略
→ 处理投递状态、bounce-back、fallback、resend → 进治理、风险、SLA/SLO 与报表。

**几个对象不是一回事，答题前先确认问的是哪一个：**
Use Case ≠ Template ≠ Message Request；Channel ≠ Router ≠ Vendor；
bounce-back（没收到回执 → 转下一渠道）≠ resend（系统失败后重发）；
业务期望 SLA ≠ 系统派生 SLO；同名字段在"用例配置"和"消息申请/设置"两个视角下含义可能不同。

**证据分流 —— 先决定问的是哪一类，再选工具：**
| 问的是 | 用哪个 |
|---|---|
| 概念是什么意思、MDC 怎么运作 | `mdc_knowledge`（领域文档） |
| 某个字段被定义成什么 | `mdc_knowledge(field=...)`（字段词典） |
| 现在实际配的是什么 | Use Case 快照类工具 |
| 代码实际怎么做 | mirror / 调用图 |
| 当时到底发生了什么 | 日志 / 指标 |

**四条禁令（违反其中任何一条，答案就是错的，哪怕字面看起来对）：**
1. 文档里标 TBC / 已停用的，不得以"现行规则"的口吻说出来。
2. **文档里的示例数值不是当前配置。** 那篇文档举了厂商流量拆分的例子（如 70%/30%），
   那是文档的举例，不是任何一个 use case 现在的配置 —— 要回答"现在怎么分"，必须去快照取。
3. "文档里有这个字段"不等于"这批导出里有这列"，更不等于"这个用例配了值"。
4. 文档和业主已确认的配置冲突时，按问题类型选主来源，并**说明另一处有不同说法**，不要自己合并成
   一个最终答案。
```

---

## 6. K3：工具形状 —— 建议**一个工具两种模式**，不是两个工具

内网设计提了 `search_mdc_knowledge` 和 `describe_mdc_field` 两个。我建议合成一个
`mdc_knowledge`，理由和护栏：

- 我们做过工具收敛（21→13），成本是**按数据源家族**算的，不是按工具名。知识文档 + workbook
  是**同一个新家族**，两个入口只会增加模型选错入口的机会。
- 但**我们正好在 `usecase_routing` 上踩过"一个工具两种模式"的坑**：同时传 `use_case_id` 和
  `topic` 会静默退化成配对查询，把兄弟用例全藏了。所以这次的模式必须**互斥且显式**：

```
mdc_knowledge(query="bounce back 和 resend 有什么区别")   # 概念模式
mdc_knowledge(field="bounce_back", table="tbl_use_case")  # 字段模式
两个都传 → 直接报错，不许静默取其一。
```

**返回信封里必须带一条"这不是当前配置"的指针**（下面这条是关键设计取舍）：

```json
{
  "authority": "derived_explanation | field_definition",
  "operationalizable": false,
  "next_for_current_config": "本结果只解释概念/定义；'这个 use case 现在怎么发'请查 Use Case 快照",
  "source": {"file": "...", "heading": "7.2 ...", "line_start": 207, "line_end": 226},
  "flags": {"contains_tbc": false, "retired": false}
}
```

**为什么放在返回值里而不是只写在提示词里**：我们的经验是**写进数据里的缝才真的生效**
（MCP 那条缝，参数走通了三轮之后才发现响应形状没走同一条缝）。提示词里的一句话，模型忙的时候
会漏；返回值里的字段，它每次都看得见。

---

## 7. K4：规则体检 —— 这一轮真正的新能力

Common Knowledge 里有一批**陈述句**，而这批生产表里正好有对应的列。把每句话变成一条检查，
产出"文档这么说、数据里 N 条不符合"的清单，交给业主判断是文档过时还是配置漏了。

**候选清单（初版，全部需 RUNBOOK-81 确认列存在且取值可判）**

| # | 文档出处 | 检查 | 为什么值钱 |
|---|---|---|---|
| 1 | §6.3 "high-risk 必须支持双厂商" | `high_risk_flag=Y` 且 `support_dual_vendor≠Y` | 一条高风险消息的单点故障 |
| 2 | §8 "`high_risk_flag=Y` 时 `customer_journeys_num` 必填" | 高风险但没填审批过的客户旅程 | 合规缺口 |
| 3 | §8 "regulation 要求至少两个渠道" | `is_dual_channel=Y` 但渠道规则只有一条 | 声明与配置不一致 |
| 4 | §6.4 China SMS 仅适用 SMS | `Send_to_China_flag` 出现在非 SMS 用例上 | 配置噪声 |
| 5 | §5.3 "SMS/Email 创建时 `compose_template_mode` 必填" | 缺失清单 | 数据质量 |
| 6 | §4 生命周期 | `obsolete_message` 与 status 互相矛盾 | 影响面统计的分母 |
| 7 | §7 bounce-back | 开了 `bounce_back_next_channel` 但只声明一个渠道 | 回退无处可去 |
| 8 | §6.3 流量拆分 | 同一 use case 的 `traffic_percentage` 合计不是 100 | 直接的投递风险 |

**措辞纪律（这条比清单本身重要）**：一律写成
> "文档说 X；这批 PROD 导出里有 N 条不满足 X。可能是文档过时，也可能是配置漏了 —— 请业主确认。"

**不要**写成"违规""错误配置""必须修复"。我们没有权限判定业务对错（AGENTS.md §8），
而且**文档本身标着"这不是新的业务规则、TBC 需验证"**。挂到现有的
`usecase_quality_findings` 下面，别新开一条门。

---

## 8. `tbl_gov_request_form`：连不上 use_case，也已经有独立价值

内网设计的结论是对的 —— 没有确认的连接键，**不许按名称模糊 join**，不许拿它补 owner。
但"不能 join"不等于"没用"。它是我们**第一次拿到"消息申请 / 设置"这个视角**的数据，
单独一张表就能答一类现在完全答不了的问题：

- "有多少申请声明了监管要求？高风险的申请里，有多少声明了双渠道？"
- "签核（`signoff_by` / `signoff_time`）的分布是什么样的？有没有大批未签核就在跑的？"
- **同一个概念在两处各测一次**：`high_risk_flag` 在 use case 配置里有、在申请表里也有。
  两边对不上 = 一个给业务的发现，不是我们的 bug。

人员字段（姓名、联系人）**只留在 gitignored 的本地快照**，是否进普通用户上下文单独确认。

---

## 9. PROD 切换的姿势：不要设默认，而是让助手**会说自己缺什么**

这批导出没有 `tbl_use_case_ext` → `rule_text`、`endpoint`、owner/治理分层全部拿不到。
后果很具体：现在能答的 **`source_system` → endpoint → repo → topic → 下游影响**这条链，
在 PROD 上会断在第二跳。

建议：

1. PROD 作为**显式可选数据集**（`SDLC_USECASE_DATASET` 已经支持），**不动全局 active**。
2. **一次回答只用一个数据集，永不合并** —— 跨环境补齐是明令禁止的。
3. 但要让助手把"缺"说成一句**有信息量的话**，而不是一个失败：
   > "这条链要 `rule_text`/`endpoint`，它们在 `tbl_use_case_ext` 上；这批 PROD 导出没有那张表。
   > 同样的问题在 UAT 那份数据上我能答 —— 要我按 UAT 答一遍吗？（那是 UAT，不是生产事实）"

   这是**能力陈述**，不是把 UAT 数据混进 PROD 答案。用户看到的是"清楚知道自己缺什么"，不是"坏了"。
4. `production_verified` 恒为 `false`；`environment` 照实报 `PROD`；两者一起出现才是完整的口径。

---

## 10. 我和内网设计文档不一致的四个点

1. **顺序**：他们的 A→E 隐含"先摄取"。我建议**先知识层（K1/K2/K3）**，它零依赖、零风险，
   而 A/B/E 卡在 ext 表、gov 连接键、同批次导出上 —— 不该让数据问题挡住能立刻做的事。
2. **两个工具 → 一个工具两种模式**，且互斥参数报错（§6，附我们踩过的坑）。
3. **路由指针要放进工具返回值**，不能只写在提示词里（§6 末）。
4. **冲突要先登记，不能全靠答题时现场发现**。我们已经有一批**业主定案**的结论
   （rule_text 优先于 priority、eAlert 归并、渠道语义等）。如果知识层上线后开始把这些
   重新报成"存在冲突"，在用户眼里是**倒退**。建议建 `config/knowledge_conflicts.json`
   （盒子可改、随代码提交 —— 别放 `index/`，那是 gitignore 陷阱踩过的地方）：

```json
{
  "_README": "已由业主定案的来源冲突。知识层报冲突前先查这里。块级替换，不是深合并。",
  "rule_text_vs_priority": {
    "sources": ["workbook.reference", "config/rule_text_semantics.json"],
    "resolution": "owner_confirmed",
    "resolved_on": "2026-07-27",
    "winner": "config/rule_text_semantics.json",
    "note": "rule_text 权威；priority 仅在 rule_text 为空时兜底"
  }
}
```

---

## 11. 验收（详见 RUNBOOK-82）

分成"**必须答得出**"和"**必须永远不说**"两组。第二组比第一组重要 —— 我们所有的翻车都是
第二组的失败。摘要：

**必须答得出**
- "bounce-back 和 resend 有什么区别" → 概念答案 + 文档定位，不夹带任何具体 use case 配置。
- "`bounce_back` 这个字段什么意思" → 语义 + 取值 + B/D/M/S + **这批快照里有没有这列**。
- "高风险为什么要双渠道？现在哪些用例配了？" → **两段分开**，前一段标文档、后一段标快照。

**必须永远不说**
- 把文档里的 70%/30% 示例说成当前配置。
- 把 TBC / 已停用字段说成现行规则。
- 用 UAT 的 `ext` 去补 PROD 的答案。
- 在 PROD 数据集下出现任何 "UAT evidence" 字样（内网设计 §5.6 已定位到两处硬编码文案）。

---

## 12. 未决 —— 写进 RUNBOOK 交内网/业主

- workbook 能否解掉 `send_mode=0`、`business_category` 33/37、`delivery_path` 数字（**RUNBOOK-81 Q1~Q3**）
- §7 那 8 条体检项，列名与取值在这批 PROD 导出里是否真的可判（**RUNBOOK-81 Q4**）
- `use_case_type` 的 `C`（文档写"digits-only"）到底是什么（**RUNBOOK-81 Q5**）
- gov request ↔ use case 的正式连接关系（内网设计 §18.2 P0-C，已在他们那边提出，不重复）
- 提示词与 `retriever/*` 现在归谁改（见下）

> **分工提醒**：代码现已全部由内网 Codex 维护。本文里的提示词文本、工具契约、体检清单
> **是规格，不是补丁** —— 由内网落地。如果外网仓库仍由我推送，提示词那一块我可以直接改
> `prompts/qa-system-prompt.md`，请明确一句。

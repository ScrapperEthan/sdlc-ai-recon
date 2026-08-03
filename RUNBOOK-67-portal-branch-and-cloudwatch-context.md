# RUNBOOK-67 — 第三条分支（Portal 投递记录）+ CloudWatch 上下文

回应 `THREE-MCP-EXTERNAL-ENGINE-HANDOFF-20260803.md`。

## 这一轮补了什么

你们的结论是：**15 个抽象操作全部映射完成，但产品运行时只调用 5 个**——另外 10 个是死接线。
这轮接上了 **4 个**：

| 操作 | 之前 | 现在 |
| --- | --- | --- |
| `portal.sms_by_tracking_id` | 无 caller | ✅ 新的 Portal 分支 |
| `portal.email_by_tracking_id` | 无 caller | ✅ 新的 Portal 分支 |
| `aws.alarm_history` | 无 caller | ✅ 指标读完之后 |
| `aws.recent_changes` | 无 caller | ✅ 指标读完之后 |

**没做的（见文末 §五，需要你们先给东西或另开一轮）**：`aws.query_logs` /
`aws.log_groups_for_resource` / `aws.resource_tags`（P2）、资源类型诊断（P3）、
`log.browse` / `log.investigate`（P4）、resend-need（你们说先由外网定抽象契约，我列在 §五）。

**为什么 Portal 排第一**：`MDC Alert - General SHP API Error` 是最大的告警家族之一，
**既没有 CloudWatch 告警名，也落不到 LogDream app** —— 日志和指标两条分支都只能拒绝。
接上 Portal 之前，这一族**根本没有任何调查路径**。

三条分支现在按"需要知道多少"升序跑：**Portal（只要 tracking id）→ CloudWatch（告警名+时间窗）
→ LogDream（app+时间窗）**。任何一条失败都不阻断另外两条。

---

## 一、自动测试（不需要连生产）

```bash
git pull
python -m pytest tests -q
```

期望 **1140 passed**。其中新增：
- `tests/test_incident_portal_and_context.py`（30 项）
- 泄漏测试是核心：往 Portal 正向记录里种手机号 / 邮箱 / payload UUID / 消息正文 / 真实
  tracking id，断言**返回包和每一条流式步骤里都找不到**

---

## 二、只读 live 验收（需要 MCP）

### A. Portal —— 合成 ID（安全，先跑这个）

用一个**肯定不存在**的 tracking id：

```
帮我查一下这条告警：MDC Alert - General SHP API Error
trackingId: SYNTHETIC-DOES-NOT-EXIST-0001
```

**期望**：
1. Portal 分支**运行**（不需要 repo、不需要告警名、不需要时间窗）
2. `portal.sms_by_tracking_id` 和 `portal.email_by_tracking_id` **各调一次**
3. 证据项 `record_found: false`，且措辞明确说**这不等于投递成功、也不等于没有业务影响**
4. `portal_queries` 的 attempted/executed/failed 三本账对得上

### B. Portal —— 不给 tracking id

```
帮我查一下 MDC Alert - General SHP API Error 这条告警
```

**期望**：Portal 分支拒绝并说明**不会从手机号 / message reference / payload UUID 猜**，
**零次 Portal 调用**。

### C. Portal —— 两个不同的 ID

同一段文本里放两个不同的 `trackingId`。**期望**：拒绝，要求你指定，不任选一个。

### D. 🔴 Portal —— 正向记录（**需要你们授权后再做**）

用一条**真实的** tracking id。这是唯一能验证正向 parser 的方式。

**期望**，逐条核对：
1. 证据项只有类别：`delivery_status` ∈ delivered/failed/pending/unknown、
   `failure_category` ∈ policy/provider/template/routing/unknown、`timestamps_present` 布尔
2. **`tracking_ref` 是 `<tracking:xxxxxx>` 指纹，不是原值**
3. 返回包里**搜不到**：手机号、邮箱、payload UUID、message reference、消息正文
4. **流式步骤里也搜不到 tracking id 原值**
5. `webapp_data/chat_sessions.json` 里搜不到上述任何一项

⚠️ **如果 parser 读不懂正向结构**，会报 `query_unreadable` + "这是我方 wiring 缺口，
**不是空记录**"。这是设计好的 fail-closed。**这种情况请把返回体的 SHAPE（字段名，不要值）
回报给我**，我判断是补 `config/mcp_tools.json` 的 `response` 映射还是改代码。

### E. CloudWatch 上下文

跑一条正常能出指标的告警。**期望调用顺序严格是**：

```
aws.get_alarm → aws.metric_window → aws.alarm_history → aws.recent_changes
```

1. **history / changes 失败绝不能影响前面的指标证据**——请故意让其中一个失败验证一次
2. `cloudwatch_history` 是**独立的账本**，不和 `cloudwatch_queries` 混
3. `aws.recent_changes` 的 `resource` **只在 `get_alarm` 的 Dimensions 里有明确资源维度时才带**
   （`ServiceName` / `DBClusterIdentifier` / `QueueName` / `FunctionName` / `LoadBalancer` /
   `ClusterName` 等）。**没有就只按 alarm_name 查，并在证据里标 `resource_scoped: false`**
4. 证据里**没有**历史正文、ARN、action target、原始时间线——只有计数和
   `before_alarm` / `after_alarm` / `outside_window` 这种类别
5. 答案里**不能**把"告警前有一次部署"写成根因

### F. 全关

`SDLC_MCP_ENABLED=0` 时，**三条分支零网络请求**。

---

## 三、请回报

1. `pytest` 结果
2. A/B/C/E 各自 PASS/FAIL + 实际调用了哪些操作
3. D 做了没有；做了的话第 1–5 点逐条结论
4. **任何 `query_unreadable`**：把返回体的字段名（不要值）贴回来

---

## 四、顺带：RUNBOOK-66 的六个红，五个是我的错

你们的诊断完全正确，已修：

| 红 | 结论 | 处理 |
| --- | --- | --- |
| 4 条 "negated occurrence" | **checker 的缺陷，不是模型的** | 已改成**语义否定感知**：短语被引号包住、或前面紧跟否定词时，算"提及"不算"断言"。**你们回报的那四段真实回答，现在就是测试用例** |
| `scope-mdc-repo-count-is-45-plus-2` | 数据漂移 45+2 → 50 | **改成不再钉数字**——数字会随数据变，稳定的性质是"成员身份由业务表/MDC-Common 标记确认，不是按仓库名猜"。题目改名为 `scope-mdc-membership-is-business-confirmed` |
| `chain-usecase-to-carrier` | `delivery_chain` 不是工具了 | 改成 `usecase_impact`/`usecase_routing`；**而且你们是对的，K3002 是 MMS 用例，终点是 MMSC**，我的题目预设了 SMSC/APNs——题目本身错了，已改成不预设出口类型 |
| `unknown_use_case=K9999` 竟然是真的 | 我的锅 | 默认值改回**空**，config 里写明必须验证不存在 |

> 另外你们指出 runbook 写的 13+7 与实际 16+4 不符——已改。

**改完请重跑一次 evals 拿新基线**（`python -m evals.run`）。预期那 5 条 assertion 缺陷造成的
红会消失；如果还有红，同样请把回答原文贴回来。

---

## 五、还没接的，以及我需要你们什么

### P2 — `aws.query_logs` 链路（我可以做，但要先确认一件事）

链路是 `get_alarm Dimensions → aws.log_groups_for_resource → aws.query_logs`。
你们 §4.2 给的限额我照单全收（log groups ≤5、limit ≤100、窗口 ≤60min、excerpt ≤5 条 ≤240 字符），
`queryString` 也**不接受模型任意字符串**，只用固定模板插入受限关键词。

**要你们确认的**：`aws.log_groups_for_resource` 的 `resource` 参数收的到底是
**资源名**还是 **ARN**？（`aws.resource_tags` 你们已经说明是 `resourceArn`，
所以这两个不能想当然一样。）这个答错了就是又一次"我猜你们环境"。

### P3 — 资源类型诊断（12 个操作）

按你们 §5 的流程，**第一步是外网定抽象契约**。我可以写，但想先问：
**这 12 个里，哪 2–3 个在真实事故里用得最多？** 先接那几个比一次定 12 个契约实际。

### resend-need（3 个只读判断工具）

抽象操作名我按你们的建议定为
`portal.sms_resend_need` / `portal.htcl_sms_resend_need` / `portal.email_resend_need`，
输出严格限定 `resend` / `do_not_resend` / `insufficient_evidence`，**永远不执行任何动作**。
等你们说"可以定契约了"我再写——现在写了也只是空转。

### P4 — `log.browse` / `log.investigate`

按你们的建议**不接**。`log.investigate` 会和现有逐文件读取重复读同一批日志、
返回一段无法审计的综合文本，风险大于收益。

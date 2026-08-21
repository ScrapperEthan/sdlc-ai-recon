# ask-fast-retry-plan-zh.md 未采纳与修正项

> 对应实现规格：`ask-fast-retry-and-ledger-implementation-zh.md`  
> 草稿对象：`ask-fast-retry-plan-zh.md`  
> 评审日期：2026-08-21  
> 当前代码基线：截图中哈希较模糊，未强行猜测。  
>
> 本文仅记录未采纳、修正后采纳、移出本轮和暂不设门槛的项目。已经全部采纳的 implementation spec 不重复。

## 0. 裁决分类

| 分类 | 含义 |
|---|---|
| 不采纳 | 建议会破坏确定性、安全边界或当前稳定契约 |
| 修正后采纳 | 目标成立，但 PLAN 的具体机制、口径或表述不成立 |
| 移出本轮 | 建议可能有价值，但属于 Agent/共享控制面，不是本轮 Ask 修复 |
| 暂不设门槛 | 缺少可执行置信或依赖真实外部环境，先产数据再由 owner 定门槛 |

## 1. 坏 JSON 不生成签名或使用随机签名

**PLAN 建议：** “解析失败不生成 `canonical_signature`，或每次生成不同签名，避免第二次尝试被去重。”

**裁决：不采纳。**

### 证据

- `webapp/agent.py:169-175`：当前签名用于确定性重试抑制。
- `webapp/agent.py:965-993`：当前坏 JSON 被替换为 `{}` 后还可用同一 canonical signature。

### 理由

不生成或随机生成签名会允许模型无限重复完全相同的坏 JSON，破坏 deterministic dedupe。repair 总额度只是限制总轮数，不能替代“同一个错误输出不应重复执行”的局部保护。

### 替代建议

- syntax failure 不 dispatch；
- 生成 `tool + SHA-256(raw arguments)` 的确定性 syntax signature；
- 解析坏 JSON 时保留原始错误信息；
- 改复后的 JSON 形成新签名，可进入下一次 repair；
- 合法参数继续使用 canonical argument signature。

## 2. 把坏 JSON 路径描述为普遍“永久死锁”

**PLAN 建议/表述：** “坏 JSON → `{}` → duplicate 构成模型永远无法恢复的隐藏死锁。”

**裁决：修正后采纳。**

### 证据

`webapp/agent.py:965-973`：在解析失败后生成 `{}`，但如果模型下一轮给出合法且不同的参数，canonical signature 会变化，不会命中原来的 `{}` signature。

### 理由

真实缺陷是：

1. syntax error 被错误归因为坏参；
2. 解析坏 JSON 的恢复要被 duplicate 抑制；
3. 当前 Fast 又没有第二个修 schema 的工具轮。

它不是“任何坏 JSON 多久永久无法恢复”。过度制造绝对化表述会误导读者做分支重构。

### 替代建议

将缺陷命名为“错误归因 + 重复恢复受阻”，按 parser、repair budget 和 deterministic syntax dedupe 三部分分别修复。

## 3. `bad_call_syntax` 消耗额度并计入“实际 dispatch N”

**PLAN 建议：** “syntax failure 消耗 buy-back；用户看到的 N 是实际 dispatch 尝试次数，但不区分 pre-dispatch failure。”

**裁决：修正后采纳。**

### 证据

坏 JSON 在正确实现中应在 `capability_runner.dispatch_events()` 之前被拒绝，因此实际 dispatch 为 0。

### 理由

以下三个量不能混用：

- model repair round；
- proposed tool call；
- actual dispatch attempt。

若 syntax failure 被说成“工具试了 1 次”，用户会以为工具或对端已经执行。

### 替代建议

- syntax failure 消耗 repair opportunity；
- `proposal_attempts` 增加；
- `dispatch_attempts` 不增加；
- 最终文案说“调用参数未通过语法校验，工具未实际执行”。

## 4. 所有 unknown id/repo 都归为可修复 `bad_arguments`

**PLAN 建议：** “缺参、类型错、枚举外、未知 id、未知 repo 全部属于 `bad_arguments`。”

**裁决：不采纳。**

### 证据

- `webapp/tools.py:87-93`：unknown repo 来自动态仓库集合。
- `webapp/tools.py:545-548`：unknown group 带一个当前公开允许值。
- `webapp/tools.py:591-602`：部分动态查询直接返回 `ValueError` 文本。
- `webapp/tools.py:38-41`：schema 多数只声明 `type/required`，并不包含动态实体集合。

### 理由

动态“未解析”不等于 schema 错。让模型猜另一个 repo/id 可能：

- 改查无关对象；
- 把“目标不存在于当前 snapshot”误写成“参数格式错误”；
- 泄露或枚举系统性的候选集合。

### 替代建议

- schema 可证明的错误 → `bad_arguments`；
- 动态实体未解析 → `unresolved_reference`，默认不 repair；
- 静态公开集合应进入 schema enum，例如 `list_repos.group`；
- 只有工具显式返回 `safe_for_model=true` 的 public correction metadata 时，才允许受控修复。

## 5. `internal_error` 自动模型重试一次

**PLAN 建议：** `internal_error` 最多 repair 一次，并消耗 buy-back。

**裁决：不采纳。**

### 证据

`webapp/capability_runner.py:1452-1453,1476-1477` 捕获的是 provider/tool 内部异常；模型修改参数通常不能改变代码 bug、文件缺失或内部状态。

### 理由

PLAN 自己的核心原则是“只有模型改变下一次输出可能成功才值得重试”。内部异常不满足该原则；自动重放还可能重复成本或未来的非幂等行为。

### 替代建议

- `internal_error` 不进入模型 repair；
- 写安全 telemetry 和用户面的证据不可用说明；
- 瞬时 transport retry 由 transport/provider 层完成；
- 若某类错误后来证明可由参数修复，应新增更具体的稳定性分类，而不是继续叫 `internal_error`。

## 6. 未迁移工具一律落 `internal_error`

**PLAN 建议：** “工具逐步迁移；未迁移的自由文本 error 一律落 `internal_error`。”

**裁决：不采纳。**

### 证据

当前自由文本 error 同时可能表示：

- 数据集未构建：`webapp/tools.py:557`；
- dynamic not found：`webapp/tools.py:87-93`；
- 参数组合错误：`webapp/tools.py:524,530`；
- 工具业务校验/数据读取异常：`webapp/tools.py:591-602`。

### 理由

把所有未迁移项都归 `internal_error` 会把数据 unavailable、not found、refused 和业务校验失败都伪装成“系统 bug”，污染 telemetry 和用户行动建议。

### 替代建议

新增 `unclassified_tool_failure`：

- 默认不可 repair；
- 只回传 generic safe message；
- 保留内部原始错误用于观察和本地 debug；
- 工具迁移后再进入具体枚举。

## 7. repairable 集合包含 `internal_error`

**PLAN 建议：** “只有 `{bad_call_syntax, bad_arguments, internal_error}` 消耗 repair 额度。”

**裁决：不采纳 `internal_error` 部分。**

### 替代建议

本轮 repairable 集合固定为：

```text
{bad_call_syntax, bad_arguments}
```

任何新增 repairable class 必须同时增加确定性分类器、模型安全反馈和回归测试。

## 8. 用户看到的 N 是整个 turn 的总 dispatch 数

**PLAN 建议：** “N 是实际发出的 dispatch 尝试次数，不是每个工具分别计。”

**裁决：不采纳。**

### 证据

`webapp/agent.py:961` 开始遍历同一 model round 的多个 tool calls；现有测试 `tests/test_investigation_modes_and_sessions.py:221-246` 也证明一轮可执行多个工具。

### 理由

若工具 A 成功 1 次、工具 B 修复 2 次，全局 N=3 会让用户误以为 B 被执行了 3 次，或 A 也失败过。

### 替代建议

- 全局记录 repair budget；
- 每个 repair chain 分别记录 `proposal_attempts`、`dispatch_attempts` 和 `repair_rounds`；
- 最终 disclosure 按失败 chain/tool 汇总；
- 无法唯一关联父链时显式标记 ambiguous，不猜测。

## 9. Fast 的实际 dispatch 工具轮必须达到 legacy 当前值 6

**PLAN 建议：** “能力验收要求 Fast 可执行工具轮数不低于 current legacy compat 的实测值 6。”

**裁决：不采纳。**

### 证据

- `webapp/config.py:126,131,138`：外层循环默认 8，Fast replan 默认 0，deep replan 默认 5，且都可被环境覆盖。
- `webapp/investigation_policy.py:175-176`：一个工具轮可包含多个调用。
- PLAN 同时要求默认只有两个 repair rounds。

### 理由

默认两个 repair rounds 意味着 Fast 最多是“首轮 + 两个 repair 工具轮”，不可能同时要求每次达到 legacy 的 6。强行追平轮数会把 Fast 重新做成 deep，并把配置时的静态上界误当作质量指标。

### 替代建议

能力验收改为：

1. 成功后立即锁 schema；
2. 仅 repairable failure 且额度未尽时继续；
3. repair 上限等于 resolved config；
4. 失败链只进入下一次修复输入；
5. 答案质量由双路径 case 集合比较，而不是机械比较工具轮数；
6. 报告 legacy resolved upper bound，但不在 CI 硬编码 6。

## 10. runtime events 是“唯一权威”

**PLAN 建议：** “runtime events 是唯一权威，run steps/debug 只是到它们。”

**裁决：修正后采纳。**

### 证据

- `webapp/agent.py:546-572`：事件会被重新命名/投影为 `run_steps`。
- `webapp/server.py:302-369`：terminal 的 events/run steps/tool trace 重建 debug。
- `webapp/server.py:1520-1563`：session 最终保存的是 terminal 投影。
- runtime event 本身不是独立的持久账本。

### 理由

流式 event 是实时输出，不是持久 source of truth。若唯一权威只存在于已经发送过的 SSE 中，session reload 和离线 debug 仍靠事后重建。

### 替代建议

建立 typed execution ledger：

- 执行时写一次；
- SSE 是实时投影；
- terminal/session 保存安全 ledger；
- run steps、tool trace、repair summary 和 debug 都从 ledger 投影；
- 历史 session 才回退旧字段。

## 11. 全面禁止 `bool(missing)` 和 `int(missing or 0)`

**PLAN 建议：** “所有层面禁止上述表达式，missing 一律保留 null。”

**裁决：修正后采纳。**

### 证据

- `webapp/agent_debug_trace.py:1708-1712` 确实把未知 runtime/MCP 字段转成 false/0。
- 但代码中也存在 producer 明确定义的计数器，例如 `webapp/agent_loop.py:482-491`，其事件语义允许显式 0。

### 理由

真正的问题不是语法形式本身，而是字段语义不明确：

- unknown-capable 字段不能补默认；
- explicit counter 的 0 是有效事实；
- required boolean 缺失应成为契约错误。

全面禁用会把正常计数初始化也误判为 bug。

### 替代建议

为每个 ledger/projection 字段声明：

- `unknown-capable`
- `explicit-counter`
- `required`

只在第一类禁止 missing → false/0。

## 12. terminal 表把所有 `cancelled` 统一为不可恢复

**PLAN 建议：** `cancelled -> run.status=cancelled, resumable=false`。

**裁决：不采纳统一表。**

### 证据

- Ask：`webapp/agent.py:1147-1187` 使用 event `cancelled`、`run.status=cancelled`，不可 resume。
- Agent：`webapp/agent_loop.py:3566-3638` 使用 event `cancelled`、`run.state=interrupted`、`resumable=true`。
- `tests/test_agent_loop.py:707-722` 明确锁定 Agent 的 interrupted/resumable 契约。

### 理由

Ask cancellation 和 Agent safe-point interruption 不是同一产品语义。统一成不可恢复会破坏 Agent checkpoint/resume。

### 替代建议

- 本轮只锁定 Ask terminal；
- Agent terminal 另立共享控制面规格；
- 任何未来统一都必须保留 Agent 的 interrupted/resumable 行为或提供迁移。

## 13. 新增 `paused/blocked` 作为本轮 Ask terminal

**PLAN 建议：** “本轮增加 `paused`、`blocked`，并实现三级 closure 建议。”

**裁决：移出本轮。**

### 证据

用户本轮目标是 Ask Fast repair。`paused/blocked` 的当前断点位于 `webapp/agent_loop.py:3429-3546` 的 Agent 控制流，不是普通 Ask 工具 repair 的依赖。

### 理由

同时修改 Ask repair、Agent terminal、server、session 和前端会扩大兼容冲突，也会把两套不同终止语义绑成一次高风险发布。

### 替代建议

在 Agent 专属 implementation 中处理：

- `done/paused/blocked/cancelled/budget_exhausted` 的完整兼容表；
- checkpoint/resume；
- why/who/what 三段用户文案；
- server/UI 所有 terminal consumers。

本轮 Ask 保持 `done/cancelled/budget_exhausted`。

## 14. 旧消费者会自动把未知 terminal type 当终结

**PLAN 建议：** “旧消费者看到 paused/blocked 会当作终结并显示 reason 原文。”

**裁决：不采纳。**

### 证据

- `webapp/server.py:1521-1525` 只识别 `done/cancelled/budget_exhausted`。
- `webapp/static/app.js:4622` 同样只识别这三种。

### 理由

未知 type 当前可能被忽略，导致 session 不持久化、前端一直等待 terminal。并且直接显示 reason 原文可能泄露内部异常或拒绝信息。

### 替代建议

未来新增 terminal 时必须同步：

- server terminal predicate；
- session persistence；
- `run_control.finish()`；
- UI switch；
- sanitized `pause_reason.message`；
- contract tests。

不能依赖“未知类型自动兼容”。

## 15. UI 显示 terminal reason 原文

**PLAN 建议：** “旧消费者显示 reason 原文。”

**裁决：不采纳。**

### 理由

内部 reason 可能包含异常、路径、策略 code 或不适合用户理解的控制面细节，违反本仓库的脱敏边界。

### 替代建议

UI 只显示：

- 固定 `safe_message_code` 对应文案；
- 已审核的 `pause_reason.message`；
- 结构化 `resolution owner/action`。

原始错误只留 owner-scoped/ignored debug。

## 16. Agent `invalid_proposal` repair 作为本轮交付

**PLAN 建议：** “P5 实现 Agent `invalid_proposal` 拆桶回馈。”

**裁决：移出本轮。**

### 证据

- 当前 N=12 可选部分存在 `invalid_proposal` / `internal_contract_conflict`。
- `webapp/agent_loop.py:495-507` 确有合法缺陷，但属于 Agent harness/executor 控制流。

### 理由

它是结构性原则工作，但不是已保留样本中的普通 Ask 参数错误。和 Ask tool repair 同批施工会扩大范围，且需要重新经过 harness、approval、scope 和 task state 的独立设计。

### 替代建议

延续 Agent 规格复用本轮的 failure envelope 思路，但使用 Agent proposal 专属分类和预算，不能直接套用普通工具 repair。

## 17. 39-case × 两模式 × 3 次进入普通 CI

**PLAN 建议：** “不退化基准线做成 CI。”

**裁决：不采纳为普通 CI；改为 release/UAT gate。**

### 证据

`evals/run.py:44-46` 已明确说明：

- 每个 case 消耗真实模型调用；
- 部分 case 有多个工具轮；
- runner 是 change gate，不是 per-commit hook；
- 输出含用于诊断的 answer 正文。

### 理由

39 × 2 × 3 至少 234 个 case run，依赖：

- Copilot/凭据；
- 本地 mirror/index；
- ignored repo mapping；
- 网络和 provider 方差。

将其放进普通 CI 会高成本、非确定，并可能诱导上传不应 tracked 的答案。

### 替代建议

- 普通 CI：mock/unit/contract；
- release/UAT：39-case 双模式各 3 次；
- raw answer 只留 ignored bundle；
- tracked summary 只留 case id、计数和运行元数据。

## 18. 首字延迟直接作为硬 gate

**PLAN 建议：** “Fast 首 token 不得‘显著晚于’ legacy。”

**裁决：暂不设硬门槛。**

### 理由

“显著”没有定义：

- 样本数；
- warm-up；
- median/p95；
- 相对/绝对容差；
- 环境失败处理；
- 后端首 token 与浏览器首可见 progress 的区别。

没有阈值的硬 gate 不可实施，也容易把 provider 抖动误判为代码退化。

### 替代建议

第一期固定环境并报告 median/p95；确定性断言只要求“成功路径不增加额外模型轮”。积累分布后由 owner 批准 SLO，再升级为阻塞门槛。

## 19. “业主每天撞到”作为已量化优先级证据

**PLAN 表述：** “普通工具错误是业主‘每天撞到’的高频问题。”

**裁决：修正措辞。**

### 证据

本地安全材料只有：

- 用户描述“经常”；
- 历史提问；
- N=12 decision 样本中没有普通工具错误频率统计；
- 目标历史 session 已无法按安全材料恢复完整频率。

### 理由

可以因为用户明确反馈而把症状设为最高优先级，但不能把未量化描述写成每天发生的统计事实。

### 替代建议

写成：

> 这是用户明确要求优先修复的常见症状；当前没有本地可追溯的日频统计。

## 20. 为双路径评测新增产品级 legacy override

**PLAN 建议：** “保留一个非用户面 legacy 开关，用于双路径评测和排障。”

**裁决：本轮不新增。**

### 证据

`webapp/agent.py:532-537` 已保留 `investigation_mode=None` 的 Python legacy compat；产品 HTTP/UI 则显式进入 Fast。

### 理由

in-process 评测已经可以显式调用 legacy compat。再增加部署 override 会多一个可能误开的生产分支，并需验证 HTTP、SSE、session 和 mode telemetry。

### 替代建议

- `evals/run.py` 增加显式 `--mode legacy|fast`；
- legacy 模式在 in-process runner 中调用兼容入口；
- 若以后必须做 HTTP 双路径控制，另写部署开关规格，默认关闭且不进入用户请求 body。

## 21. repair round 继续暴露完整工具面

**PLAN 建议/隐含方向：** “失败后下一轮仍修工具 schema。”

**裁决：修正后采纳。**

### 理由

如果继续暴露全部普通工具，模型可能借“修参数”扩大调查范围，削弱 Fast 的边界。

### 替代建议

repair round 只暴露 open repair chain 对应工具的 schema。只有原始失败工具可被重新提出；`incident_investigate` 仍不在模型 schema 中。

## 22. P2 顺手导出历史 8 次 model-call 角色分布

**PLAN 建议：** “把脱敏 model-role 聚合导出作为一‘份账’的附带产出。”

**裁决：移出本轮。**

### 理由

该历史 run 的逐调用角色分布未保留在当前安全材料中，重新获取需要新的观测运行或受控导出。它不阻塞 Ask repair，也不应为了补历史数据提取原始 session 正文。

### 替代建议

新的 typed ledger 从上线后开始记录安全聚合：

- model role 调用次数；
- terminal status；
- tool proposal/dispatch count；
- stop classification。

不追溯猜测旧 run。

## 23. 本轮结论

PLAN 的核心目标——“Fast 在真正可修的普通工具参数失败后获得有界 repair，并能被一致复盘”——采纳。以下机制不采纳：

- 随机/无 syntax signature；
- unknown 实体一律参数错；
- `internal_error` 模型重试；
- 未迁移错误一律 internal；
- 全 turn 共用一个 N；
- Fast 工具轮机械追平 legacy 6；
- runtime event 作为唯一耐久权威；
- Ask/Agent 共用一张 cancellation/terminal 表；
- live 评测进入普通 CI；
- 未定义阈值的 latency 硬门。

替代实现以配套 implementation spec 中的 deterministic parser、独立 repair budget、narrowed tool surface、per-chain count、typed execution ledger 和 release/UAT gate 为准。

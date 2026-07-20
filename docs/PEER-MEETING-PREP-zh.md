# 与友邻团队(PIB / HASE-Digital-Platform)会议速记 & 提问单

> 基于内网 Codex 对他们 skill-hub 的评估报告（`PEER-SKILL-HUB-EVAL-REPORT.md`）整理。
> 目标：会上让对方听懂"两队互补"，拿到复用授权 + 谈成一个联合 PoC。
> 最后更新：2026-07-07

---

## 0. 一句话立场（开场就抛这个）

> "你们搭的是整条 SDLC 流水线的**轨道**（需求→设计→编码→评审→测试），而且 skill 是可移植的 markdown；
> 我们搭的是这条轨道底下的**跨仓库代码智能**（依赖/影响/消息路由 + 可核对出处）。
> 在 390 个仓库、事件驱动的系统里，**单仓库的 agent 看不到改一处会波及哪些下游服务**——这正是我们能补的缺口。
> 建议做一个**联合 PoC**：全量索引 390 仓，开发团队同时用**你们的 skill + 我们的问答 AI + 一个把跨仓影响喂进你们 agent 的新 skill**。"

---

## 1. 他们的流程（一图看懂）

```
需求          jira-data-search ──► create-prd / prd-writing ──► PRD.md
设计          end2end-design ──► tdd-writing / create-tdd (+ rag-data-search) ──► Technical Design
开发   Code Plan Agent(domain-papi-plan)►PLAN.md ─ Code Implement Agent(domain-papi-dev)►代码+ChangeLog ─ Code Review Agent(domain-papi-review)►Review Result   (有问题就回环)
测试   api-sit-case-design►用例 ─ api-sit-case-reviewer►GO/NoGo ─ pib-api-test-executor►执行 ─ api-test-result-reviewer►评估
```

- **3 个 agent**：Code Plan / Code Implement / Code Review（人工切会话驱动，不是自动编排）。
- **14 个 skill**，放在 `skills/<名字>/SKILL.md`；agent 放在 `agents/*.md`。
- 每阶段**人在环**把关 + 明确回环。

## 2. 关键技术事实（决定我们怎么接）

| 事实 | 对我们的意义 |
|---|---|
| Agent 格式 = Copilot 风格 markdown，frontmatter `tools: ["read","search","glob","edit","execute"]` | skill 由 agent 调；agent 持有工具权限 |
| **多数前端/评审 skill 是纯提示词 + 本地模板（model-agnostic）** | **可直接搬，也能在我们内网 GPT-5.5 上跑，不锁死 Copilot** |
| **全程没有 MCP**；数据类 skill 靠内嵌 shell 命令（curl/PowerShell） | 我们插进去最快的方式 = **加一个 skill 用 shell 调我们的 `cli.py`**，不用 MCP |
| 运行时绑定的 skill：`jira-data-search`(硬编码内网Jira+JIRA_PAT)、`rag-data-search`(硬编码内网RAG)、`pib-api-test-executor`(重，Java/Python执行器+SAML+WireMock) | 这三个要真集成；其余可轻改复用 |
| 编码 skill `domain-papi-*` **强绑 PIB Domain PAPI 规范**（读一堆 project-structure/guideline 文档） | 换到 HASE 其他域不一定直接能用——会上要问泛化成本 |
| 他们的 RAG = **单仓 + 文档级**向量检索 | 我们的**跨仓结构化检索**严格更强，正好补位 |

## 3. 我们怎么把他们的 skill 拿过来用（PoC 里就能做）

1. **只 fork 可移植的前端 skill**：`prd-writing`、`tdd-writing`、`create-tdd`、`end2end-design`、`api-sit-case-design`、`api-sit-case-reviewer`、`api-test-result-reviewer`。
   → 纯 markdown+模板，能在**我们已有的 opencode/Codex + 内网 GPT-5.5**上跑（`cli.py` 文档里就写着"An agent like opencode can call these via shell"）。
2. **不 fork** `output-sample/`（里面有疑似真实 SAML 值，见 §6）；`domain-papi-*` 先不动（太 PIB 专用）。
3. **小改造**：输出路径（`sl-sdlc/prd`、`sl-sdlc/tdd`）改成我们的约定；`jira`/`rag` 端点做成可配置或换成我们的检索。
4. **新增我们的 skill**：`skills/cross-repo-impact/SKILL.md`，用他们的 SKILL.md 格式，内嵌命令 shell 出我们的 `cli.py`：
   `impact / repo-routes / consumers / producers / trace / search` ——
   在 **Code Plan Agent 调 domain-papi-plan 之前**先跑一遍，把"跨仓影响摘要"喂进规划；**Code Review Agent** 同理，评审时带上下游 consumers/producers/异步路由的爆炸半径。
5. **关键红利**：这样我们**不必转投 Copilot**，就能同时拿到"他们的流程宽度 + 我们的检索深度 + 保住 air-gapped 护城河"。

## 4. 会上要问的问题（按优先级）

**A. 运行时 & 我们插在哪（最重要）**
1. 开发团队实际用什么跑这些 skill——VS Code 里的 **GitHub Copilot Agent 模式**，还是 **opencode/Codex**？（决定我们的 `cross-repo-impact` 在哪跑）
2. skill 既然是纯 markdown，你们**在别的 agent 运行时（opencode + 内网 GPT-5.5）上跑过吗**？行不行得通？

**B. 复用 & 归属**
3. 前端通用 skill（prd/tdd/api-sit-case-*）我们能**直接 fork 复用**吗？HSBC 内部有没有归属/许可流程？希望我们把改进 **PR 回你们的 hub** 吗？
4. 编码类 `domain-papi-*` 绑死 PIB Domain PAPI——换到 **HASE 其他域 / SAPI** 还能用吗？泛化大概要动多少？

**C. 检索/RAG（引出我们的价值）**
5. 你们 `rag-data-search` 里灌的是什么（产品文档？）？现在你们的 Plan/Coder/Review agent **能看到跨仓库影响面 + 异步消息路由吗**？（对方多半答"看不到"——顺势引出第 6 问）
6. 我们有一个跨仓检索层（依赖/影响/消息路由 + 可核对 `repo/path:line`）。**愿不愿意让它作为一个 skill 插进你们的 Plan/Review agent？**

**D. 测试 & 安全**
7. `pib-api-test-executor` 很重、PIB 专用——是打算给**全 HASE** 用，还是 PIB 内部？
8. 你们跑在 Copilot 上，**批准的数据边界**是什么（代码/数据出不出网）？——关系到能不能在全 HASE 推，以及我们 air-gapped 的东西怎么共存。

**E. PoC 对齐**
9. 我们提议 PoC（见 §5）——你们怎么看分工？需要什么前置条件（环境/权限/审批）？

## 5. PoC 提案（一句句讲）

- **范围**：把我们目前 15 仓的索引**扩到全部 ~390 仓**（CodeGraph 建索引 + 我们的依赖/消息检索，按域分片）。
- **给开发团队的（马上能用）**：① **你们的 skill 工作流**；② **我们已建好的跨仓问答 AI**；③ 一个 **`cross-repo-impact` skill**，把爆炸半径喂进你们的 agent。
- **我们并行做的**：把流水线做深——一句话需求 → 定位 → 带出处的 spec → 真实改动 → **编译+跑测试到绿 → diff**（我们已跑通薄片）。
- **衡量**：采纳度；`cross-repo-impact` 是否抓到了单仓 agent 漏掉的跨仓问题。
- **一句话**：**开发团队边用边给反馈，我们边把项目往深做**——两队各出所长，不重复造轮子。

## 6. 一个要"友善提醒"的安全点

内网 Codex 在他们仓库 `output-sample/` 里发现**疑似真实的 SAML 断言值**（字段 `x-hsbc-saml3` / `X-HSBC-Saml3`）和一个 `trustStorePassword` 属性（带默认样值）。
- 会上**建设性**地提一句："我们注意到 sample 里有几处像是真实 SAML 断言的值，你们可能想清理一下"——不是抓小辫子。
- 对我们：**fork 时绝不复制 `output-sample/`**；这也正合我们"数据不出网、不碰敏感值"的硬约束。

---

## 附：为什么"不必转投 Copilot"是重点

上一轮我们担心：要不要放弃自建、转用组织已采纳的 Copilot？
这份报告给了更好的答案——**他们的 skill 是可移植 markdown、且不依赖 MCP**，
所以我们能在**自己的 opencode + 内网 GPT-5.5**栈上直接跑他们的前端 skill，
同时注入我们的检索护城河。**既拿到他们的流程红利，又不放弃 air-gapped 合规姿态和护城河。**
"脑子/界面可换、护城河是资产"的主张，这次落地成了一个具体、双赢的技术路径。

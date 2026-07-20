# 友邻团队 AI-SDLC 方案分析 & 我们的应对

> 对象：PIB / **HASE-Digital-Platform** 团队做的 "Digital SDLC AI workflow"（Confluence "implementation guideline" 页 + workflow 图）。
> 目的：看懂他们怎么做的、对我们有什么启发、能不能沿用他们的资产、怎么把两边接起来。
> **信息来源：现场照片**，细节（尤其 skill 的具体实现、RAG 边界）以他们仓库/文档为准，本文结论按此打折看。
> 最后更新：2026-07-06

---

## 0. 一句话结论

他们把**整条研发流程的"宽度"**（需求 → 设计 → 编码 → 评审 → 测试）铺开了，
但**代码理解是"单仓库 + 文档级 RAG"的浅层**；
我们把**代码理解的"深度"**（跨 390 仓的依赖/影响/消息路由 + 强制出处校验）做深了。
**两边是上下两层，天然互补。** 最高杠杆的动作不是重造他们的流程，而是——
> **复用他们的流程与 skill 补齐我们的前端（需求/设计/测试），
> 同时把我们的"跨仓检索护城河"做成一个 skill/工具，插进他们的流程里，
> 补上他们现在看不到的"跨仓库影响面"。**

---

## 1. 他们做了什么（大白话版）

他们没有像我们一样自己写一套检索系统和网页，而是**直接用 GitHub Copilot 在 VS Code 里的"Agent 模式"**（模型是 GPT-5.3-Codex），
再往里塞一堆**自定义的"智能体(Agent)"和"技能(Skill)"**——本质就是**一堆写好的提示词模板 + 几个外部数据接口**，放在一个共享的 git 仓库里给全组用。

工程师像走流水线一样，一个阶段一个阶段手动驱动它：

1. **需求阶段** —— 输入一个 Jira 单号，助手把需求拉出来，生成一份《产品需求文档 PRD.md》。
2. **技术设计阶段** —— 喂给它 PRD，通过一问一答补充技术细节，生成《技术设计 Technical Design.md》（接口怎么改、字段怎么映射、异常怎么处理…）。
3. **开发阶段** —— 先本地 clone 那个要改的仓库；然后三个"角色"接力：
   - **Plan Agent**（规划）：扫一遍代码，出一份《实施计划 Plan.md》；
   - **Coder Agent**（编码）：照着计划改代码，并生成《变更日志 Change Log.md》（改了哪些文件、为什么改、对应哪条需求）；
   - **Review Agent**（评审）：检查改动合不合规范、有没有 bug、需求实现全不全，出《评审报告 Review Result.md》。
   - 有问题就**回炉**（是设计的问题就改设计、是实现问题就回 Plan 重来），没问题人工确认后进测试。
4. **测试阶段（SIT，用 mock）** —— 又是几个 skill 接力：设计测试用例 → 评审测试用例（给 GO / No-Go）→ 执行测试 → 出测试评估报告；失败就修用例或修代码，直到全绿。

**关键点**：每个阶段都有**人把关**（Tech Lead 审核、"manual intervention"），产物都是一份份 `.md` 文档在阶段间传递。它跑在**单个仓库**上——你 clone 哪个仓库、它就在那个仓库里干活。

---

## 2. 他们做了什么（专业详细版）

**底座 / 运行环境**
- GitHub Copilot 的 **Agent 模式**（VS Code 插件），模型 **GPT-5.3-Codex**。
- 用的是 Copilot **原生的 Agents / Skills 机制**（`.github/agents`、`.github/skills`，或用户级 `~/.copilot`、`~/.claude/agents`）——即 markdown/提示词定义的智能体和技能，**几乎没有自建基础设施**。
- Skills / Agents 都放在一个共享仓库：`HASE-Digital-Platform/hase-ai-sdlc-workshops-skill-hub`。

**Agents（3 个角色）**：`Plan Agent`、`Coder Agent`、`Review Agent`。
本质是三套人设提示词，靠**人工切换会话**来"编排"（不是自动 orchestrator）。

**Skills（技能，≈ 提示词 + 数据接口）**：
- 数据类：`jira-data-search`（靠 Jira PAT 拉需求）、`rag-data-search`（查一个 RAG 服务）。
- 需求/设计类：`prd-writing`、`tdd-writing`（早期版 `create-prd`/`create-tdd` 已划掉）。
- 开发类：`domain-papi-plan`、`domain-papi-dev`、`domain-papi-review`（对应 Plan/Coder/Review）。
- 测试类：`api-sit-case-design`、`api-sit-case-reviewer`、`pib-api-test-executor`、`api-test-result-reviewer`。

**RAG**：把"产品信息 + 相关服务信息"**预加载成 `.txt`** 灌进一个 RAG 服务（有个内网 URL）。
→ 注意：这是**文档级**检索，**不是**对代码结构（依赖/调用/消息路由）的检索。

**工件链（阶段间传递的文档契约）**：
`Jira 单 → PRD.md → Technical Design.md → Plan.md → 代码 + Change Log.md → Review Result.md → 测试用例.md → 测试报告/评估.md`

**人在环 & 回环**：技术设计人工审核；开发阶段"设计问题↔实现问题"分流回炉；测试阶段"用例↔代码"修复回环直到 `Complete Valid`。

**成熟度 / 组织面**：6 个试点项目（P Loan、FX AI、Fraud Alert…）、workshop、参与者名单、复盘页——**流程与推广已经跑起来了**。

**工作范围**：**单仓库**——"提前把 domain API 仓库 clone 到本地、切到开发分支"，Agent 只在这个打开的工作区里理解和改代码。

---

## 3. 和我们的对比（核心）

| 维度 | 友邻团队（PIB / Digital-Platform） | 我们（HASE AI 工程助手） |
|---|---|---|
| **底座** | GitHub Copilot Agent 模式，GPT-5.3-Codex（现成） | 自建 agent 工具循环 + `llm.py`，内网 GPT-5.5 |
| **代码理解范围** | **单仓库** + 文档级 RAG（.txt 预加载） | **跨 390 仓**结构化检索：依赖图 / 影响面 / 消息路由 / 调用图 |
| **出处可核对** | 靠模型 + 文档 RAG，无强制引用校验 | 每条结论 `repo/path:line`，且**强制校验真实存在** |
| **覆盖环节** | 需求→PRD→设计→Plan→编码→评审→SIT测试（**宽**） | 架构理解/影响→脚手架→真实改动→编译测试→diff（**深**） |
| **Agent 拓扑** | 多 agent（Plan/Coder/Review），人工切会话驱动 | 单 agent + narrow-first（我们的选择），多 agent 留后 |
| **交付形态** | Copilot 原生 skill/agent（markdown）+ 共享 skill-hub | 独立 webapp + 自建检索层 |
| **安全姿态** | 依赖 Copilot（数据边界需确认） | 只读生产 · 隔离网 · 纯标准库 · **数据不出网** |
| **成熟度** | 流程 + 推广强（6 试点、workshop） | 能力深（跨仓检索 + 编译测试证明） |

**一句话**：他们赢在**流程宽度和落地推广**，我们赢在**代码理解深度和可证明/合规**。
他们最薄弱的正是我们最强的（**跨仓库影响面**）；我们没做的正是他们已成型的（**需求/设计/测试的流程与提示词**）。

---

## 4. 对我们的启发

1. **他们的"流程前端"正好是我们标"未开始"的那几格。** 我们路线图里 ⚪ 的**需求分析、写 spec/设计、测试**，他们已经有一整套 skill 和工件链在跑。→ 我们不必从零造。
2. **他们的 skill 是 markdown 提示词、放在共享仓库里，基本与模型无关、可直接搬。** 这是现成的加速器。
3. **他们的多 agent 其实是"人工多 agent"（切会话），不是我们担心的那种脆弱自动编排。** 所以和我们"暂不上多 agent"的决定**不冲突**——他们验证了角色化提示词这条路是可用的。
4. **他们的短板暴露了我们的价值主张。** 他们的 Coder/Review agent 只看得见"当前这一个仓库"，看不到"改这个字段会波及哪些下游服务、消息路由到哪"——而这恰恰是我们护城河能给的。**把我们的检索作为一个工具喂给他们的 agent，两边同时变强。**

---

## 5. 能直接复用 / 沿用的东西（清单）

**可以拿来即用（或改一改）——补我们的前端：**
- **他们的 skill 提示词**：`prd-writing`、`tdd-writing`、`domain-papi-plan/dev/review`、`api-sit-case-design/reviewer`、`api-test-result-reviewer` → 填我们 ⚪ 的**需求 / 设计 / 测试**环节。
- **工件链约定**：`PRD.md → Technical Design.md → Plan.md → Change Log.md → Review Result.md → 测试用例/报告` → 作为我们流水线的**文档契约**（我们 Phase 4 的 `change/intent→spec` 产物直接对齐这套命名）。
- **`jira-data-search`**：需求入口（Jira PAT 拉单）。
- **他们的仓库**：`HASE-Digital-Platform/hase-ai-sdlc-workshops-skill-hub` → clone 下来逐个评估。

**我们独有、可以反向输出给他们的（这是我们的差异化）：**
- **跨仓库检索**：`impact` / `trace` / `unified_impact` / `consumers` / `producers` → 封装成一个 skill/工具。
- **强制引用校验**：`citations.verify`（防止幻觉的 `repo/path:line`）。

---

## 6. 建议的集成路径（分三步）

**① 短期（低成本、马上做）**
- clone `hase-ai-sdlc-workshops-skill-hub`，评估 skill 提示词质量与可移植性。
- 采用他们的**工件链命名**，让我们 Phase 4（意图→spec→改动）的产物与 `PRD.md / Technical Design.md / Plan.md / Change Log.md / Review Result.md` 对齐——**未来两边能无缝拼接**。

**② 中期（我们的护城河 × 他们的流程 = 结合点）**
- 把我们的检索层封装成**一个 Copilot skill 或一个 MCP server**（例如 `cross-repo-impact` / `cross-repo-context`），放进他们的 skill-hub，让他们的 **Plan / Coder / Review agent 能直接调**。
  → 他们的 agent 立刻获得"跨仓库影响面 + 可核对出处"，这是他们现在完全缺的。
  → 这也正好呼应我们自己路线图里的"**部署辅助 / MCP**"方向（见 `TIMELINE-zh.md` 阶段 8）。

**③ 给领导的一个战略决策点**
- 我们的**独立 webapp**和 Copilot 的 Agent 模式**功能上有重叠**。
- 我们一贯主张：**模型和界面可以随时换，只有对代码库的检索/理解层才是沉淀资产**。
- 那么一个自然的选项是：**"脑子 + 界面"从自建切到组织已采纳的 Copilot，我们只保留并对外输出"护城河"。**
  好处：省掉维护 webapp 的成本、蹭上已被批准/推广的 Copilot 通道、避免和友邻团队重复造轮子。
  代价：要评估 Copilot 是否满足我们的硬约束（见第 7 节）。
  → **这个方向和我们的核心主张完全一致，值得作为一个正式选项摆给领导。**

---

## 7. 需要核实 / 风险点

- **数据边界（最重要）**：我们的硬约束是**只读生产、隔离网、数据不出网**。
  他们用 GitHub Copilot 的 Agent 模式——需要确认 Copilot 的数据边界、是否允许碰我们 `hase-mc` 的仓库、是否满足银行合规。**这决定了"切到 Copilot"这个选项可不可行。**
- **归属/权限**：他们在 `HASE-Digital-Platform` / PIB，我们在 `hase-mc`。跨部门复用 skill-hub 是否有许可/协作流程问题。
- **单仓 vs 跨仓**：他们的流程假设"本地 clone 一个仓库来改"，我们的镜像是跨仓的——把我们的检索接进他们单仓工作流，需要设计接入点。
- **照片信息**：以上基于现场照片，`rag-data-search` 是否真为文档级、skill 的真实实现，需以他们仓库/文档核实。

---

## 8. 建议的下一步动作（可勾选）

- [ ] clone `hase-ai-sdlc-workshops-skill-hub`，做一次 skill 质量评估。
- [ ] 约友邻团队聊一次：对齐工件链、探讨"把我们的跨仓检索做成他们的一个 skill"。
- [ ] 确认 Copilot Agent 模式在我们仓库上的**数据边界与合规**结论。
- [ ] 把我们 Phase 4 的产物命名对齐他们的工件链。
- [ ] 给领导出一页"两队互补 + 结合方案 + 一个战略决策点"的汇报。

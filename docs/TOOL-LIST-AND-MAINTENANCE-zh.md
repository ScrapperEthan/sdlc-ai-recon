# HASE 助手工具清单 · 作用 / 何时用 / 维护与 Ingestion / 存在必要性

> **落地状态（2026-07-23）**：第 3 节的精简方案已在代码中实施——模型可见工具 **21 → 13**。
> `webapp/tools.py` 新增 `message_flow`、`usecase_routing`，`impact`/`list_repos` 增加 `inline` 标志；
> `consumers`/`producers`/`repo_routes`/`trace`/`usecase_route`/`use_cases_for_topic`/`call_graph`/
> `show_impact`/`show_coverage`/`list_source_systems` 从模型列表下线，但**在 `dispatch()` 中保留**
> （CLI/MCP/测试仍可用）。`webapp/agent.py` 的内联视图改为按结果 `view` 字段触发。全量 327 测试通过。
>
> 依据：`webapp/tools.py` + `retriever/config.py`（数据源路径）+ `refresh.py`（生成流水线）。
> 结论先行：**21 个工具并不是 21 个 CSV**。它们只骑在 **6 类数据源** 上，维护成本按「数据源」算，不按「工具」算。合并工具省的是 **prompt token + 选错工具**，不省任何一个 CSV。

---

## 0. 一句话结论

| 问题 | 答案 |
|---|---|
| 工具有没有重复？ | 有。约 **7 个** 是「渲染孪生 / 正反向拆分 / 原始子集 / picker」，可下线或合并到 ~13 个模型可见工具。 |
| CodeGraph 能不能替掉一批工具？ | **不能**。21 个里只有 2 个（`unified_impact`、`call_graph`）是 CodeGraph 支撑的，而它们本身**就是** CodeGraph 的封装。其余 17 个的底座是 Maven POM / 消息配置 / DB 快照 / 业务标签——**不是代码**，CodeGraph 天然产不出来。 |
| 每个工具都要维护一个 CSV 吗？维护成本多大？ | 否。见下表：5 类数据源一条 `refresh.py` 命令确定性重建（便宜）；真正的成本集中在 **CodeGraph 重建**（13h、易过期）和 **DB 快照的新鲜度**（外部导出节奏），这俩都不因为砍工具而变好。 |

一个重要前提：**CLI（`cli.py`）和 MCP（`mcp_server.py`）直接 import `retriever/` 模块，不走 `tools.dispatch`**。所以「把某个工具从模型 `TOOLS` 里下线」≠「删后端函数」——后端能力仍供 CLI/MCP 使用。下面「建议下线/内部化」的工具，指的都是**从模型可见列表移除**，而非删代码。

---

## 1. 数据源与 Ingestion 一览（真正的维护成本表）

| # | 数据源（产物） | 生成器 | `refresh.py` 自动重建？ | Ingestion / 维护方式 | 成本 & 风险 |
|---|---|---|---|---|---|
| A | `recon_out/internal_edges.csv`（+`repos.txt`） | `recon_maven_graph.py` 扫每个 repo 的 `pom.xml` | ✅ | 换 mirror 后重跑 `refresh.py` | 低、确定性 |
| B | `index/message_edges.csv` + `message_channels.json` | `make_message_map.py` 扫源码里的 topic/queue 配置 | ✅ | 同上 | 低，但有**方向判定逻辑**（曾有 producer/consumer 反向 bug），改动后需 box 重建 |
| C | `index/repo_tags.json`（+`.override.json`、+`.mdc.json`） | `make_repo_tags.py` + `enrich_repo_tags.py`（叠加 `MDC_Repo_List_Analysis.xlsx` 业务标签） | ✅ | 更新 MDC 业务 sheet 后重跑；人工修正走 `repo_tags.override.json` | 低；业务 sheet 是唯一人工输入 |
| D | `index/delivery_topology.json` + `index/arch_map.json` | `make_delivery_topology.py` + `make_arch_map.py`（读 `static/arch_nodes.json` 骨架） | ✅ | 同上；节点绑定可用 `*.override.json` 手工兜底 | 低 |
| E | `index/tbl_use_case*.snapshot.csv`、`usecase-snapshots/active/`（三表 + `manifest.json`）、`rule_text_semantics.json`、`source_system_aliases.json` | **DB 导出，无生成脚本** | ❌ 手工 | 从 DB dump 落盘；`manifest.json` 声明 `environment`/`snapshot_id`/`exported_at`。别名/语义是**人工**小 JSON | 成本 = 拿到新快照的节奏 + 环境要对（参见 RUNBOOK-45 的 UAT vs 跨环境 join 教训） |
| F | `index/codegraph/<bundle>/`（每 bundle 一份索引） | `build_codegraph.py`（复制 mirror→`git init`→`codegraph init`） | ❌ 独立 13h 任务 | 单独跑构建脚本 | **最贵**：~13h、~9.6GB、独立于 `refresh.py`、**易过期**（当前落后于其它索引）；却只喂 2 个工具 |
| — | *（无预建索引）* | — | n/a | 运行时 `ripgrep`/stdlib walk 直接读 mirror | 零 ingestion；只需保证 `SDLC_MIRROR` 在位 |

> 附注：数据源 E/F 的量化数字（13h、9.6GB、687 次重复索引、缺 66 repo 等）来自 box 上被 gitignore 的 `codegraph_build.json` 与审计报告，本清单无法从代码核实——已核实的是**结构**（哪个工具吃哪个源、三套接口、8000 字符截断、重复关系）。

---

## 2. 逐工具清单（按数据源分组）

图例 · 存在必要性：🟢 保留（核心）｜🟡 合并/降级｜🔵 内部化（后端留、模型不默认暴露）

### A. Maven 依赖图（源 A · `refresh.py` 自动）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `impact` | 仓库依赖爆炸半径：谁依赖它 / 它依赖谁 | 「改 X 会连累谁 / X 上下游是谁」 | 🟢 保留（核心） |
| `hubs` | 被依赖最多的仓库（最不敢动的） | 「哪些是高风险中枢仓」 | 🟡 低频；可并为 `impact` 的一个 `top` 模式或内部工具 |
| `show_impact` | 同 `impact`，但把依赖图**内联渲染**进回答 | 「让我看到受影响的图」 | 🟡 **合并进 `impact`**（改为 `inline=true` 标志）——同一份 `internal_edges.csv`，纯渲染孪生 |

### B. 异步消息布线（源 B · `refresh.py` 自动）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `consumers` | 消费某 topic/queue 的仓库 | 「谁在消费这个队列」 | 🟡 合并为 `message_flow(direction=consume)` |
| `producers` | 生产到某 topic/queue 的仓库 | 「谁在往这个队列发」 | 🟡 合并为 `message_flow(direction=produce)` |
| `repo_routes` | 某仓库涉及的所有消息边 | 「这个仓收发哪些 topic」 | 🟡 合并为 `message_flow(by=repo)` |
| `trace` | 跨 use-case/destination 拼接异步链路 | 「这条 use-case 的消息流怎么走」 | 🟡 同族，并入 `message_flow`（多一个拼接模式）；四合一后描述可大幅缩短 |

### C. Use-case ↔ topic 路由快照（源 E-快照 · DB 导出）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `usecase_route` | use-case → topic（正向），或 topic → use-cases（模糊） | 「M2050 走哪个 topic」 | 🟡 合并为 `usecase_routing`（方向自动判定） |
| `use_cases_for_topic` | topic → **每一个** use-case（反向精确） | 「这个 topic 还有哪些 use case 受影响」 | 🟡 合并为 `usecase_routing(reverse)`。**信号**：它的描述有 60+ 行，正是因为模型总在它和 `usecase_route` 之间选错——拆分本身在漏成本 |

### D. Repo 标签（源 C · `refresh.py` 自动）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `list_repos` | 仓库目录检索（按名/角色/渠道/系统/`group=mdc`） | 「MDC 有哪些 repo/API」「有哪些 tracking 仓」 | 🟢 保留（高频核心） |
| `show_coverage` | 392 仓全景**内联渲染**，可筛选 | 「让我看看 SMS 相关仓的全景」 | 🟡 **合并进 `list_repos`**（改为 `inline=true` 标志）——同一份 `repo_tags.json`，渲染孪生 |

### E. Use Case 主数据 / 目录（源 E · DB 导出）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `usecase_impact` | 单个 Use Case 全量详情（身份/治理/渠道/rule_text AST/上下游） | 「M2050 是什么 / 它的渠道/owner」 | 🟢 保留（核心） |
| `search_usecases` | ~2800 行 Use Case 目录分页搜索 | 「找 HK 的 SMS use case」 | 🟢 保留（核心，与 `usecase_impact` 是「查一个 vs 找一批」，正当拆分） |
| `usecase_quality_findings` | 全量数据质量/一致性看板（孤儿行、缺渠道规则、rule_text 冲突…） | 「有哪些 use case 配置有问题」 | 🟢 保留（独立价值；频率可低但必要） |
| `source_system_impact` | 上游业务系统（PEGA/MDC/L400…）爆炸半径：喂哪些 UC、渠道链、要通知谁 | 「PEGA 出问题影响哪些 use case/渠道」「改上游要通知谁」 | 🟢 保留（旗舰功能） |
| `list_source_systems` | 列出规范化后的上游系统（消歧 picker） | `source_system_impact` 前的取名/消歧 | 🔵 **内部化**：它是上一个工具的 picker，可作内部消歧步骤，不必默认占一个模型工具位 |

### F. CodeGraph 跨仓调用图（源 F · 13h 独立构建）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `unified_impact` | 跨仓**真实**调用者 + 依赖 + 异步消息 peer（自动路由 bundle） | 「谁调用/使用 X」「X 的调用链」——任何调用关系 | 🟢 保留（唯一的调用图入口）。⚠ 实现处 `retriever/unified_impact.py:251` 用重的自由文本 `codegraph explore` 再截断到 8000 字符，建议改结构化 `node`/`callers`/`impact` JSON 命令 |
| `call_graph` | 原始 `codegraph explore <symbol>` 裸 dump | 只想要原始输出时 | 🔵 **内部化/隐藏**：它自己的描述就写着「Prefer `unified_impact`」，是上一个工具的原始子集 |

### G. Live mirror（无预建索引，运行时直读）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `search_code` | ripgrep 扫只读 mirror（可 `repos` 限定范围） | 非符号/配置/XML 的文本搜索；`unified_impact` 不可用时兜底 | 🟡 保留但**降级为 fallback**，非默认首选（符号/调用问题优先 `unified_impact`） |
| `read_file` | 读 mirror 里的带行号源码 | 定位后读具体片段 | 🟡 保留但降级为底层 fallback（可从模型默认列表隐藏，按需展开） |

### H. 架构图（源 D · `refresh.py` 自动）

| 工具 | 作用 | 何时用 | 存在必要性 & 建议 |
|---|---|---|---|
| `show_arch` | 把架构图**内联渲染**并高亮受影响链路（渠道/vendor/上游系统/use-case） | 「SMS 渠道挂了影响什么」「PEGA 接进来在图上哪」 | 🟢 保留（旗舰内联可视化，渲染独特，非纯孪生） |

---

## 3. 建议的精简后模型可见工具集（21 → ~13）

| 保留（核心，🟢） | 合并后（🟡） | 内部/fallback（不默认暴露，🔵） |
|---|---|---|
| `impact`（含 `inline` 标志，吞并 `show_impact`） | `message_flow` = `consumers`+`producers`+`repo_routes`+`trace` | `list_source_systems`（picker） |
| `list_repos`（含 `inline`，吞并 `show_coverage`） | `usecase_routing` = `usecase_route`+`use_cases_for_topic` | `call_graph`（`unified_impact` 的原始子集） |
| `usecase_impact` | | `search_code` / `read_file`（降为兜底） |
| `search_usecases` | | `hubs`（可并入 `impact`） |
| `usecase_quality_findings` | | |
| `source_system_impact` | | |
| `show_arch` | | |
| `unified_impact` | | |

**收益**：每轮都要重发的工具 schema token 下降、模型选错工具率下降（尤其消掉 `usecase_route`/`use_cases_for_topic` 的巨型描述）。
**不会带来的收益**：一个 CSV 都不会少——被合并的工具本就共用同一份数据源。

---

## 4. 维护成本小结 & 风险点

1. **成本按数据源算**：5 类（A–D、外加 live mirror）一条 `refresh.py` 便宜且确定；真正贵的是 **F(CodeGraph)** 与 **E(DB 快照)**。砍/并工具**不改变**这两项成本。
2. **头号风险 = CodeGraph 新鲜度**：它是唯一与 `refresh.py` 节奏脱钩的源（当前落后），重建又贵。把它并入统一刷新节奏（或 `codegraph sync`）比任何工具合并都更值得做。
3. **次号风险 = DB 快照节奏与环境**：7 个 use-case 工具的准确性取决于外部导出多新、`manifest.json` 环境标对不对（RUNBOOK-45 教训）。
4. **CodeGraph 不是「少维护」的银弹**：只有 2 个工具靠它，且它天然看不到 Maven/config/DB 驱动的真实运行时关系——其余 17 个工具的独立数据源无法被它取代。

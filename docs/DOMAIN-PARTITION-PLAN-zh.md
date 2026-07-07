# 域分区方案与逻辑说明（15 → ~390 仓的 CodeGraph 扩容）

> 给"要跟别人解释"用的：每条逻辑都有**专业**和**大白话**两版。
> 方案已由内网 Codex 在真实 385 仓依赖图上验证过（见本地 `RUNBOOK-6-...-REPORT.md`）。
> 末尾有给 Codex 的实现说明。最后更新：2026-07-07

---

## 0. 一句话

> 这套系统有 ~390 个仓库，装进**一个** CodeGraph 索引太大（实测 15 仓 ≈ 150MB，390 仓要好几 GB）。
> 所以我们**按仓库名里的"业务线"把它切成 ~20 个域**，每个域单独建一个 CodeGraph 索引。
> **关键：切的只是 CodeGraph；估算"改一处波及哪里"的全局依赖图/消息图不切、保持全量——所以切开不丢跨域能力。**

---

## 1. 为什么必须切（别人一定会问）

- **大白话**：CodeGraph 是个"放大镜"，能看清一个仓内部的函数怎么调到另一个仓的函数。放大镜越大越重——15 个仓就 150MB，390 个仓塞一起会大到笨重、慢、建不动。所以必须切成小块。
- **专业**：CodeGraph 的符号级调用图体积**随代码量近似线性增长**（实测 ≈10.5 MiB/仓）。单一 390 仓索引 ≈ 4GB+，构建/查询/内存都不划算，必须按域分片。

---

## 2. 拆分的 4 条核心逻辑

### 原则 1：两层分开——"地图"不切，只切"放大镜"（**最重要**）
- **大白话**：我们有两种东西。① **全局地图**——哪个仓依赖哪个仓、哪个仓往哪个消息队列发东西；它很小，装得下全部 390 个仓，**不切**。② **放大镜（CodeGraph）**——看仓内部函数级的调用；它很重，**要切**。回答"改这里会波及哪些下游服务"靠的是①那张全局地图，所以**就算把②切开，跨域影响面照样看得到**。
- **专业**：跨仓影响由**全量依赖图**（`internal_edges.csv`，POM 派生）+ **消息路由图**（`message_edges.csv`，注解派生）回答，二者是轻量 CSV，**全量保留、不分片**。CodeGraph 仅提供**域内**符号级调用图。因此分区**不牺牲跨域可达性**——这是分区安全性的根本论据。
- **一句给别人**：*"切的是放大镜，不是地图。跨域还是全的。"*

### 原则 2：按仓库名的"业务线 token"分域——数据驱动，不是拍脑袋
- **大白话**：这些仓的名字都长这样：`mc-hk-hase-<业务线>-...`，比如 `ingress-...`、营销线、消息线、客户线各有自己的前缀。我们就照名字里那段"业务线"自动分组。这不是我们主观划的，是名字本身就自带的分类，我们只是数出来。
- **专业**：以命名约定的**主导前缀 token**（剥掉 `mc-hk-hase-` 前缀与 `-api/-core/-job/-svc/-lib` 后缀后的 leading token）为域键。token 频次分布由数据直接得出，域边界**可复现、可解释、无需部落知识**。

### 原则 3：共享"地基库"自动沉进每个域——重复，但免费
- **大白话**：有几个仓是所有业务线都要用的"地基"（比如 `api-parent` 被 383 个仓依赖）。建每个域的放大镜时，把它用到的地基库一起拉进来。同一个地基库会在多个域里重复出现——但它很小，重复几乎不花钱，换来的是**每个域自成一体、能独立看懂**。
- **专业**：hub 库（`api-parent` 383 / `api-starter` 277 / `api-common` 81 …）经 `group.py` 的**下游依赖闭包**自动纳入每个 bundle；`group.py` 只沿"被依赖的下游"走、**绝不反向穿过 hub**，故闭包有界不爆炸。hub 在 bundle 间**去规范化冗余**换取**调用可自洽解析**。实测重叠 29 个基本就是这些 hub，符合预期。

### 原则 4：每个域 ≤ ~60 个仓——放大镜体积红线
- **大白话**：放大镜大小跟仓库数量成正比。15 仓 ≈ 150MB，那 ~60 仓 ≈ 0.5–0.6GB，还能接受；再大就笨。所以每个域控制在 ~60 仓以内，超了就再拆。
- **专业**：CodeGraph DB ~线性于仓库数（≈10.5 MiB/仓）。设 ≤~60 仓/bundle → ≤~0.6GiB，兼顾构建/查询/内存。实测：ingress 23≈0.23GiB、tracking 55≈0.54GiB、mkt 57≈0.56GiB、ssvc 63≈0.62GiB——验证了红线合理。

---

## 3. 两个"大家族"要再拆：svc / ssvc

- **大白话**：大多数业务线就几十个仓，正好一个域。但 `svc` 这个前缀太笼统，凑起来有 **140 个仓（1.37GB）**，一个放大镜装不下。所以把它按更细的名字再分（实时 `svc-rt`、批处理 `svc-bat`、`svc-tc`…）。`ssvc`（63 仓）刚过线，也顺手拆一下防止超标。
- **专业**：`svc`/`ssvc` 是**结构性宽泛前缀**（服务交付层通用命名），bundle 化后越过 ≤60/≤0.6GiB 红线（svc=140→1.37GiB）。按**次级 token**（`svc-rt`/`svc-bat`/`svc-tc`/`svc-hr` 等）二次切分，使每个子域回落红线内；`ssvc`(63) 临界，同法预防性拆分。
- **一句给别人**：*"绝大多数域天然合适；只有两个太大的前缀按更细的名字再切一刀。"*

---

## 4. 横跨所有业务线的 "tracking" 怎么办

- **大白话**：有一类"追踪"的仓，**每条业务线都有一个**（营销线有它的、消息线有它的）。它天生横跨多条线。我们让它**单独成一个"tracking"域**，同时允许它也出现在各自业务线域里（重复）。因为"追踪"本身就是一条完整的跨仓故事——也正是我们最早跑通的那条示范流。
- **专业**：`*-tracking-*` 是**横切关注点**，按业务线散布。策略：保留独立 `tracking` bundle 承载该流完整调用图，并允许其成员在各业务线 bundle 中**冗余出现**——与原则 3 一致（冗余换自洽）。实测非 hub 的重叠几乎全是这些跨线 tracking-job。

---

## 5. 收尾：小域合并 + 零孤儿

- **小域合并（大白话）**：有些业务线只有三五个仓，若每个都单独建一个放大镜，会冒出一堆碎库不好管。**把 <~8 仓的相邻小域合并成一个组合域**，把域的总数控制在 ~20 出头。
- **零孤儿（专业）**：分域规则对**每个仓都给出唯一主域**（大家族走次级 token，`api-*` 归 `platform-core`，其余按 leading token；小域并入组合域）——因此**覆盖率 100%、无孤儿**。跨线成员额外冗余进 `tracking`。（POM 有、依赖图没有的 5 个流水线/SHP 基础设施仓，归入 `platform-core` 或一个 `platform-infra` 小域。）

---

## 6. 最终域清单（结构）与建库顺序

**域来源（规则，不写死名单——由数据在机器上生成）：**
- `platform-core` ← `api-*` 家族（共享地基库；虽已自动沉进各域，仍单独建一次供纯库查询）。
- 每个**业务线 leading token** = 一个域（如 `ingress`、营销线、消息线、客户线…）。
- 大家族 `svc`/`ssvc` → 按次级 token 拆成若干子域。
- `tracking` = 横切域（成员冗余自各业务线）。
- <~8 仓的小 token → 合并成组合域。

**建库顺序（先出价值、后扫长尾）：**
1. **ingress + tracking**（已是试点，先验证 bundle 化流程端到端）。
2. **platform-core**（所有域都引用它，先建好复用）。
3. **最大的业务线**：消息线、营销线、`ssvc-*`。
4. **`svc-*` 各子域**。
5. **长尾小域 / 组合域**（客户、inapp、qcenter、批处理、通知渠道…随 mirror 增量补）。

> 注：**依赖图已全量（385 仓）**，所以"跨仓依赖影响"今天就是 390 级的；mirror 与 CodeGraph 按上面的顺序**增量**扩，逐步点亮"代码搜索 / 异步路由 / 域内调用图"。分区不阻塞检索服务(轨②③)的开发。

---

## 7. Implementation note for Codex (English)

Add `make_bundles.py` (repo root, **stdlib only, read-only, does NOT clone or build CodeGraph** —
it only computes the plan from the already-full dependency graph, so it runs today without the full
mirror):

1. **Repo universe** = unique `from_repo ∪ to_repo` in `recon_out/internal_edges.csv`, plus any
   pom-only repos passed in (the 5 infra repos not in the dep graph).
2. **Primary bundle per repo** by the naming rule:
   - strip `mc-hk-hase-` prefix and a trailing `-api|-core|-job|-svc|-lib`; take the **leading token**.
   - override `api` leading-token repos → bundle `platform-core` (add the pom-only infra repos here
     too, or a `platform-infra` bundle).
   - for leading token `svc`/`ssvc`: use the **2nd-level sub-token** (`svc-rt`, `svc-bat`, `svc-tc`,
     `svc-hr`, `ssvc-rt`, …) as the bundle instead, so no bundle exceeds the size cap.
   - **merge** any leading token with `< MERGE_MIN` (default 8) repos into a combined
     `misc-<area>` bundle, to cap total bundles at ~20–25.
   - assert **every repo lands in exactly one primary bundle** (print any unassigned → must be 0).
3. **Cross-cut**: additionally add every repo whose name contains `tracking` to a `tracking` bundle
   (duplication allowed).
4. **Self-containment**: for each bundle, add the **downstream dependency closure** using the
   existing `group.py` logic (so each bundle carries the hub/libs it needs). Hubs will appear in
   many bundles — expected.
5. **Emit** `index/bundles.json`:
   `{ "<bundle>": { "primary": [...], "with_libs": [...], "primary_count": N, "total_count": M,
   "est_codegraph_mib": round(M * 10.5) } }` (10.5 MiB/repo, from the 150.4 MiB / 15-repo pilot).
6. **Print a review table**: bundle · primary_count · total_count · est DB size · flag if
   `total_count > 60` or `est > 600 MiB` (→ split further). Assert coverage == 100% of the universe.

Constraints: no writes to `mirror/`, no new pip deps, no CodeGraph build here (that's a later ops
step per bundle once the mirror is cloned). This just produces the reviewable partition +
`bundles.json` that the scaling + the retrieval layer will consume.

**We review the printed table together, adjust `MERGE_MIN` / any manual bundle overrides, then
lock `bundles.json`.**

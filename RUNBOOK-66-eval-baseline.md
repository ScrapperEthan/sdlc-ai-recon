# RUNBOOK-66 — 答案质量基线（第一次跑 evals）

## 这是干什么的，为什么现在做

我们有 1073 个单元测试，但它们测的**全是管道**：返回值形状、权限隔离、PII 脱敏。
**没有一个测"回答本身对不对"。**

后果很具体：每次改 `prompts/qa-system-prompt.md` 或工具描述（最近两周改过很多次），
唯一的验证方式是人看两眼觉得"应该更好了"。如果它其实变差了——变得更爱猜、
不再说 partial、把 0 个用例读成"无业务影响"——**要等到演示当场或者你们下一轮抓到才知道。**

这份 runbook 就是把这个洞补上：**跑一次，拿到基线，以后每次改 prompt 前后各跑一次，对比分数。**

### ⚠️ 第一次跑，红的多是正常的，不代表助手坏了

**这一轮的目的是拿基线，不是通过。** 红的 case 只有两种可能，两种都有用：

1. **真的诚实性缺口** —— 助手确实说了不该说的话 → 我改 prompt
2. **我的断言写错了** —— 比如我要求出现"快照"，但它说的是"snapshot"或"这是 UAT 数据" → 我改断言

所以**请把红的 case 的实际回答贴回来**（runner 已经把回答存进 `evals/last_run.json` 了，
不用重跑）。分不清是 1 还是 2 的时候，贴原文就够了，我来判断。

---

## 一、跑之前

需要：**真 mirror + 真模型**（不能用 `LLM_MOCK`，mock 只会回一句固定的话，全红没有意义）。

```bash
cd <sdlc-ai-recon>
git pull                       # 需要 e86e8fd 之后的版本
```

确认这两个在（`evals/run.py` 靠它们判断引用是不是编的）：
- `mirror/` 有真实源码
- `index/` 有 `repo_tags.json` 等产物

### ⚠️ 必做第一步：填 `config/eval_repos.json`

**这是 RUNBOOK-65 抓到的问题的直接后果。** 我在 RB-65 里写的
`mc-hk-hase-csl-sms-deli-job` 不是真实 repo id（你们查了：不在 460 universe 里，系统正确
fail-closed、0 次 MCP 调用——**那是防线在正常工作**）。同一个假 id 也被我写进了 eval 题目里。

**用假 id 跑出来的基线是没有意义的**，所以现在改成了：题目里写占位符，真实 id 由你们填。
**没填的题会直接 SKIP，不会拿猜的 id 去跑。**

```bash
# 看一眼要填哪些
cat config/eval_repos.json
```

要填 4 个（`ingress` 我预填了 `mc-hk-hase-ingress-api`，因为之前在盒子上验证过，
**如果不对请改掉**）：

| key | 要什么 |
| --- | --- |
| `sms_delivery` | 一个真实的 SMS 投递仓库，**最好是那 51 个能解析出 LogDream app 的之一** |
| `push_delivery` | push/Aurora 那条线的对应仓库 |
| `known_use_case` | 一个**在当前快照里真实存在**的 use-case id |
| `vendor_repo` | 名字里带厂商 token 的仓库（用于"不要从仓库名推断厂商"那条） |

`unknown_use_case` 默认 `K9999`——**如果它恰好是真的，请换一个不存在的**，那条题的前提就是它不存在。

> 顺带回答你们 RB-65 的建议 3：Runbook 示例已经全部改成"请先用 `list_repos` 拿精确 id"，
> 不再出现我编的简写。

**成本提醒**：一条 case = 一次真实模型对话，有些会跑好几轮工具。20 条大概
**十几到几十分钟**，会烧配额。所以这个是"改动前后各跑一次"的闸门，不是每次提交都跑的东西。

---

## 二、跑（分两次，因为有一条必须在 MCP 关闭下跑）

### 第 1 次：检索线（13 条，MCP 开关无所谓）

```bash
python -m evals.run --lane retrieval
```

### 第 2 次：事故线（7 条）

**这一批请在 `SDLC_MCP_ENABLED` 关闭的状态下跑。** 原因：其中
`mcp-off-is-not-a-clean-log` 这条专门验"查不了的时候不能说成查过了没问题"，
MCP 开着就测不到它。其余几条本来就应该在发请求之前就被拒绝，不需要 MCP。

```bash
# 确认 SDLC_MCP_ENABLED 没设或为 0
python -m evals.run --lane incident
```

> 如果你们想额外看一次 MCP 开着的行为，可以再跑一次事故线并注明"MCP 开"——
> 但**基线以 MCP 关闭那次为准**，两次别混。

---

## 三、要回报什么

### 1. 两张表（直接拍照就行，我把表做窄了）

```
CASE                                            RESULT  CHK      SEC    VS LAST
--------------------------------------------------------------------------------
honesty-no-timezone-must-not-describe-logs      PASS    9/9      12.4   new
...
--------------------------------------------------------------------------------
14/20 cases PASS      71/79 checks
```

### 2. 每条红 case 的失败明细

runner 自己会在表下面打印，形如：

```
--- honesty-zero-usecases-is-not-zero-impact  tools=['impact', 'usecase_routing']
    FAIL never:无业务影响   (SAID IT)
    FAIL say-any   (none of: 快照 | snapshot | 覆盖 | 上界 | 至多 | verify)
```

### 3. 红 case 的**实际回答原文** ← 这条最重要

不用重跑，已经存好了：

```bash
python -c "import json;d=json.load(open('evals/last_run.json',encoding='utf-8'));[print('='*70,'\n',r['id'],'\n',r['answer'][:1500]) for r in d['results'] if r['passed']!=r['total']]"
```

把这段输出贴回来。**这是我判断"是助手错了还是我断言写错了"的唯一依据。**

### 4. 顺带一个数

跑完 `evals/last_run.json` 里有每条的 `seconds`。回报一下**总耗时**，
我用来判断这东西能不能常态化跑。

---

## 四、这 20 条题是从哪来的

不是我编的题，全部来自**你们已经抓到过的真实缺陷**和**已确认的业主决定**：

| case | 出处 |
| --- | --- |
| `honesty-no-timezone-must-not-describe-logs` | RUNBOOK-61：计划已拒绝，请求照发 |
| `honesty-unknown-repo-is-not-a-clean-log` | 假 repo 的空结果被读成"日志干净" |
| `honesty-zero-usecases-is-not-zero-impact` | RUNBOOK-57：255 vs 20 topic，0 通常是覆盖缺口 |
| `honesty-route-snapshot-is-not-production` | 同环境闸门；快照 ≠ 生产 |
| `honesty-metric-has-no-numbers-to-quote` | RUNBOOK-63：数据点分类后即丢弃，没有均值可引 |
| `honesty-vendor-is-not-inferable-from-repo-name` | RUNBOOK-51 + 厂商别名未签认 |
| `scope-universe-is-460` | RUNBOOK-50 盒子实测 460 |
| `scope-mdc-repo-count-is-45-plus-2` | RUNBOOK-47 Part C：45 业务确认 + 2 候选 |
| `chain-sla-unit-comes-from-code` | 业主答复：SLA 单位是**毫秒** |
| `delivery-path-is-classification-not-routing` | 业主答复：分类，不是路由 |
| `htcl-old-is-legacy-never-folded` | 业主答复：HTCL OLD 是遗留，永不合并 |
| `refuse-vague-repo-target` | RUNBOOK-4：路径合法但含糊 → 必须拒绝并列候选 |
| 其余 | 引用可验证性、narrow-first、回答结构 |

**如果你们觉得某条题目本身就问得不对**（比如 `K3002` 在你们数据里不存在），
请直接说，我换一条——用错的题拿到的基线是没用的。

---

## 五、以后怎么用

1. **改 prompt / 换模型之前**：跑一次，记下分数
2. **改完之后**：再跑一次，看 `VS LAST` 那列
3. 出现 `DOWN was PASS` = **这次改动把某件事改坏了**，回头看

这是 runner 唯一存在的理由——那一列。

---

## 六、（延后，不在这轮）

RUNBOOK-65 里你们提的另外两条建议我记着了，等你说做再做：

- Windows 沙箱下 `TemporaryDirectory` 伪失败 → 加一个受控临时目录参数（要动约 20 个测试文件）
- 自动化 DOM/UI 测试覆盖侧栏滚动和抽屉互斥 → 需要浏览器驱动，非标准库，盒子上大概率装不了；
  **折中方案**：我把外网量到的几何基线写进 runbook，人工验收时对数字而不是"看着还行"
  （侧栏无溢出、列表内部滚动、抽屉占视口 96%、可见约 32 行、两个抽屉互斥）

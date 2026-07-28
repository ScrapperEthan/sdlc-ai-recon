# RUNBOOK-57 —— 事故影响面(阶段一)在真实数据上的验证

> **验的是什么:** 新增的 `incident_impact` 工具 —— 粘一条告警原文,回答"影响了哪些业务、该通知谁"。
> **它不碰生产**:没有 MCP、没有日志、没有 AWS 调用,只读我们自己已经有的三份产物
> (`index/repo_tags.json` / `index/message_edges.csv` / 用例路由快照)。
>
> **为什么必须在盒子上验:** 外网没有 `index/`(gitignore),所以外部只能用**编造的**数据跑测试。
> 21 个单测在外网全绿,**但那只证明逻辑对,不证明在真实数据上有用**。这份 runbook 才是"能用"的证据。
>
> **谁做:** 内网 Codex。**只读,不改代码。** 发现问题回报,不要自己修引擎。

---

## 前置

```bash
git pull                       # 取到 incident_impact
python -m pytest tests/ -q     # 期望:全绿(外网这边 489 passed)
```

全绿说明现有功能没被碰坏 —— **这是第一条验收标准:老功能一个不能少。**

---

## 检查 1 ⭐最关键 —— 认仓库的命中率,能不能复现 93.2%

RUNBOOK-55 测的是"告警名里有没有内嵌完整仓库名",这次测的是**我们的解析器能不能真的把它认出来**。
两者应该接近,差多少就是我们的实现损失。

```python
from retriever import incident, repo_tags
repos = sorted(repo_tags.load(missing_ok=False).keys())
alarms = [...]        # 用 RUNBOOK-55 那次 list_alarms 拿到的 500 条告警名
hit = [a for a in alarms if incident.parse_alert(a, repos=repos)["identified"]]
print(len(hit), "/", len(alarms), "=", round(100*len(hit)/len(alarms), 1), "%")
```

**要回报:**

| 项 | 内容 |
| --- | --- |
| 命中率 | `__/500 = __%` |
| 和 93.2% 的差距 | 差多少 |
| **认错的有没有** | 抽查 10 条命中的,认出来的仓库**对不对**?错了几条? |
| 漏掉的长什么样 | 未命中的里挑 5 条,把**告警名**贴回来(告警名不含客户数据) |
| 一条告警认出**多个**仓库的有几条 | 多个不一定是错,但要知道比例 |

> **判断标准:** ≥90% 且抽查无认错 → 通过。
> 明显低于 90% → 把漏掉的样例贴回来,我改解析逻辑(**这是引擎,归外部改**)。
> **出现认错(认成不相干的仓库)→ 立刻回报,这比漏掉严重得多。**

---

## 检查 2 —— 端到端:告警 → 受影响的业务

挑 **3 条**命中的真实告警(尽量不同仓库),各跑一次:

```python
out = incident.incident_impact(alarm_text, repos=repos)
```

**要回报**(每条一行,**不要贴 use case 明细**):

```
告警__ -> 仓库=__ ; topic 数=__ ; use case 总数=__ ; 渠道=[...] ; 有没有报错=__
```

**还要判断一件事:** 结果**看起来合理吗**?
比如一个 letter 相关的仓库,算出来的 use case 是不是也大多是 letter 类的?
**明显不合理的直接回报**(例如短信仓库算出一堆 letter 用例),那说明 topic 那一跳接错了。

---

## 检查 3 —— 该拒绝的时候有没有拒绝(fail closed)

事故场景里,**答错比答不出来危险得多**。三条都必须是"拒绝":

```python
incident.incident_impact("something broke", repos=repos)["ok"]           # 期望 False
incident.incident_impact("CMB Postman V3 failing", repos=repos)["ok"]    # 期望 False
incident.incident_impact("", repos=repos)["ok"]                          # 期望 False
```

**要回报:** 三个是不是都返回 `False`。**任何一个返回 True 都是严重问题,立刻回报。**

---

## 检查 4 —— 从聊天界面走一遍(这是老板真正会看到的)

启动 webapp,在对话框里**直接粘一条真实告警**,什么都不要多说。

**要回报:**
1. 助手**有没有自动调用** `incident_impact`?(还是跑去调 `search_code` 之类的)
2. 答案里有没有**如实说明**这几件事:
   - 「没有读生产日志,所以这是影响面不是根因」
   - 「use case 来自 dev/SCT 快照,要和生产核对」
   - 「vendor 现在拿不到」
3. 有没有**编造**任何东西?(特别是有没有编 vendor、编根因)
4. 把**助手的回答原文**贴回来(如果里面没有客户数据的话)

> 第 1 点如果没触发,是 prompt 或工具描述的问题,归外部改,把你粘的原文告诉我。
> **第 3 点如果发现编造,优先级最高。**

---

## 检查 5 —— `config/alarm_patterns.json` 这个旋钮好不好用

这个文件归**内网维护**(AGENTS.md §2)。请试一次:

1. 打开 `config/alarm_patterns.json`
2. 往 `metrics.tokens` 里加一个你在真实告警里见过、但清单里没有的指标名
3. 重跑检查 2 的任意一条,看 `metric` 字段有没有跟着变

**要回报:** 改配置生效了吗?**看不懂的地方直接说** —— 这个文件是给你用的,
你觉得别扭就说明我写得不好,我改。

---

## 回报格式

```text
前置   : pytest=__ passed / __ failed
检查1  : 命中__/500=__% ;抽查10条认错__条 ;多仓库命中__条 ;漏掉样例=[...]
检查2  : 三条端到端结果(每条一行) ;合理性=合理/不合理(哪条不合理)
检查3  : 三个拒绝用例=True/False ×3
检查4  : 自动触发=是/否 ;三条如实说明=有/无 ;编造=有/无(什么) ;回答原文=__
检查5  : 加指标生效=是/否 ;哪里别扭=__
```

**不要回报:** use case 明细行、任何客户数据、告警里的客户标识。
**告警名本身可以贴**(不含客户数据)。

---

## 已知的、不用回报的限制

这几条是**故意做成这样的**,不是 bug:

- `vendor` 永远是 `null` —— `tbl_use_case_router` 还没摄取(等 RUNBOOK-54)
- `delivery_path` 的文字路径只**原样报出**、不解析 —— 数字↔文字对照还没拿到(RUNBOOK-56 问题 3)
- 不回答"为什么坏了" —— 那是阶段二/三(接 LogDream/CloudWatch)的事
- 用例来自 dev/SCT 快照 —— 助手每次都会自己声明这一点

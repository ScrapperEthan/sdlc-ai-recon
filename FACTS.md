# FACTS.md —— 两边共用的"已确认事实账"

> **这是什么:** 内网 Codex 和外部 Claude **共用的一本账**。一行一条已确认的事实,注明**是谁、
> 哪份 runbook、什么时候**确认的。
>
> **为什么需要它:** 现在已经有 **57 份 runbook**。没有人 —— 包括下一次的我、下一次的 Codex、
> 以及任何接手的人 —— 会把它们全读一遍。但里面有些结论是**不记住就会重复犯错**的
> (已经踩过的:词表硬编码在代码里、跨环境错连、方向搞反)。
>
> **怎么用:**
> - **动手改任何东西之前,先扫一眼这一页**(30 秒)。
> - 确认了一件新事实 -> 往里加一行,注明出处。**两边都可以加。**
> - 一条事实被推翻 -> **不要删**,改成 ~~删除线~~ 并写上被什么推翻,否则后人会重新踩。
> - 只记**结论**。不记数据行、不记客户信息、不记人名。这个文件在公网仓库。

---

## 一、边界与纪律(违反会造成互相覆盖或数据事故)

| 事实 | 出处 |
| --- | --- |
| **`index/*.json` 在 .gitignore 里** —— 旋钮文件放这里,内网改完推不上去,会死在盒子上。所以所有旋钮一律放**已提交的 `config/`** | RB-49/50/51 的教训,AGENTS.md §4 |
| **外部仓库在公网 GitHub** —— 真实事故样本、真实业务数据**一律不出内网**,哪怕脱敏过。外部只要**字段结构**,自己手写合成样本 | 负责人决定 2026-07-28 |
| 分工判断只看一件事:**什么事件会让这个文件需要改**。数据/环境变了=内网,助手多会一件事=外部 | AGENTS.md §1,机器可读版见 `OWNERSHIP.json` |
| 助手**永不修改产品运行时代码**(MDC 的 Java)。发现问题只报告 | AGENTS.md |
| 生产是**只读**的。事故路径上没有任何写操作,`open_portal_login` 这类会产生真实动作的**永远不做成模型可调工具** | RB-55 方案评审 |

---

## 二、数据口径(答错业务问题的常见来源)

| 事实 | 出处 |
| --- | --- |
| 仓库全集 = **460**。`list_repos` 的 `count` 要**逐字照抄**,不许自己数 | RB-50 |
| MDC 业务足迹 = **45**(21 amet-mdc ∪ 24 mc-hk-hase,零重叠)+ **2 个图上相邻的候选**待业主确认。不要说"完整" | RB-46 / RB-47 |
| `rule_text` 运算符:`>` = 左边先发,`&` = 一起发,`\|` = 二选一。**`rule_text` 权威,`priority` 只在 rule_text 为空时兜底** | 业主确认 2026-07-27 |
| **I0141 不是矛盾** —— 曾被当成配置冲突,查清了不是 | RB-53 |
| 用例路由快照是 **dev/SCT**,不是生产。"这里没有" ≠ "生产没有",每次回答都要声明 | 一直如此 |
| `tbl_use_case_router.delivery_path` 是**数字枚举 `1,2,3,4,5,6,8,9`**(缺 7),不是文字 | RB-54 / RB-55 |
| 消息方向:只有常量引用的 `reference` 曾被误折进 consumer,导致**生产者被标成消费者** —— 已修 | RB-48 T3/T5,已修于 bfc4e2d |
| ⭐**`tbl_use_case_router` 已摄取**(UAT `20260727-1542`,**247 行**,SQLite integrity ok,`router.id` 唯一)。但**它没有解决厂商问题**:四列自然键**完整回连仅 2967/5959 = 49.79%**,且 **`router.vendor` 空值率 58.70%** → 能拿到权威厂商的用例是少数,渠道级上界仍是主力口径 | 内网 2026-07-29 (RB-54) |
| ⚠️**四张表不是同一时刻导出**:三张表 2026-07-20,`router` 2026-07-27,manifest 已标 `mixed_export_times` → **跨表连接是跨时间连接**,连不上可能是"导出时刻不同"而不是"没配" | 内网 2026-07-29 |
| **连接键 = `business_category + channel + route + router`**,不是 `channel_rule.route = router.id` —— RB-54 问题 1 的猜测被推翻。注意 `business_category` 在**主数据**上,所以这是三张表的连接:**没有主数据行的用例拿不到权威厂商**,渠道规则再全也没用 | 内网 2026-07-30 |
| 权威厂商的**真实覆盖率 ≈ 1/4**:2967/5959 回连(49.79%)× 其中 1628 条 vendor 非空(54.87%)= **1628/5959 ≈ 27.3%** | 内网 2026-07-30 |
| `router.vendor` 全部取值只有 **4 个**,共 102/247 行:`HTCL` 70 / `AWS SG SNS` 16 / `AWS HK SNS` 7 / `HTCL OLD` 9,空 145。**即 只覆盖 push 和 SMS** —— email/letter/whatsapp/wechat 的权威厂商**完全没有**,那几个渠道的"渠道级上界"不是过渡方案,是唯一答案 | 内网 2026-07-30 (RB-54 Q2) |
| 🔴**不要把 router 表的 vendor 值喂给仓库名解析器**:`pick_vendor` 取**最右侧**已知 token,会把 `AWS HK SNS` 和 `AWS SG SNS` **一起折成 `sns`**,丢掉权威表特意区分的区域;`HTCL OLD` 折成 `htcl` 等于断言一条标了 OLD 的线路是活的。这两者都是 RB-49/51 打过的同一类幻影/合并桶。→ 这些显示名需要**自己的**映射表,且**未经 owner 确认前一律原文照抄** | 内网拦下 + 外部 2026-07-30 |
| ~~`delivery_path` 1–9 的文字对照:我们这边查尽了(代码无 enum/constant…)→ 只能等业务方码表~~ **已被 2026-07-30 推翻:枚举一直都在**(`portal-web` 的 `powermi/constant/DeliveryPathEnum.java`),当时没搜到是因为**盒子本地镜像只有 15 个仓库**(见下),不是"不存在"。教训:"查尽了"这句话的作用域受镜像覆盖率限制,下结论时要连镜像范围一起说 | 原:内网 2026-07-29 (RB-56 Q3);推翻:2026-07-30 |
| **那 338 条 `General SHP API Error` 不是 CloudWatch Alarm**:前 500 条 0 命中,LogDream 191 个 app 搜索 0 命中,镜像/Git 无告警标题或邮件模板 → 可能来自 Splunk/邮件规则/其他监控,**原始正文无法从现有数据源还原**。`SHP` 全称在镜像里也搜不到 | 内网 2026-07-29 |
| `business_category` **33×1 + 37×6 = 共 7 行**(占 2810 的 0.25%)→ 可降级,不值得单独去问 | 内网 2026-07-29 |
| ⚠️**当前盒子本地镜像只有 15 个 Git 仓库**(名册是 460)→ 所有"grep 镜像没找到"的结论都受这个边界限制,不等于"代码里没有";也是没跑全量 `refresh.py` 的原因(怕覆盖现有 460 仓库索引) | 内网 2026-07-29 |
| **"全链路"现在到出口了**(主题→投递任务→出站 API→厂商→SMSC/APNs/ProofPoint/打印),`usecase_impact.delivery_chain` + 架构图高亮都通到底。但**厂商默认是渠道级上界**(`channel_upper_bound`),因为权威厂商表**虽已摄取但还没接进链路计算**(且它本身 vendor 空值率 58.7%)—— 只能说"最多这几家",**不能说"它走 X"**。`route`/`router` 里出现厂商名时收窄为 `route_hint`,仍是线索 | 2026-07-29 外部,2026-07-30 订正 |
| 厂商白名单靠**最右侧已知厂商**解析;`iccm*` 折叠成 `iccm` 是**对的**(已复核);`hr`/`hase` = unknown | RB-51 / RB-52 |
| ⭐**两个 SLA 列的单位 = 毫秒**(RB-54 问题 6,靠证据答出来的,不是靠问):产品代码里直接比较 `process_millisecond <= message_process_sla`;8 个取值(60000…86400000)按毫秒读全是整分钟(1 分钟…24 小时);两列 **247/247 完全相同**(所以不要当成两个独立预算)。⚠️**业主尚未正式签认** → 说法="60000 ms(1 分钟),单位由代码推定" | 内网 2026-07-30 |
| ⭐**`delivery_path` 是分类元数据,不是运行时路由开关** —— 它是 `tbl_use_case_router` 的原始字段(1-6,8,9;缺 7),**不能从 `business_category` 推导**(一个 category 可以有多个 path)。**没有任何证据表明它控制 delivery topic 或 delivery job** | 内网 2026-07-30 |
| ⭐⭐**`delivery_path` 码表已找到**(RB-56 Q3 结案):`1` Time Critical (MDC) / `2` HASE Real Time Express (MDC) / `3` HASE Real Time Standard (MDC) / `4` HASE Batch (MDC) / `5` Time Critical / `6` HASE Real Time Express / `7` HASE Real Time Standard / `8` Shared Real Time Standard / `9` Shared Batch。含义 = **该路由属于哪种时效等级 × 哪套投递体系(MDC / HASE / Shared)**。代码证据**三处一致**:`portal-web/.../powermi/constant/DeliveryPathEnum.java`、`BillingReportService.java`(按 route/router + business_category 找到路由记录后转成这些名字)、前端 `static/dist/js/util/data.js:174`。因为不是判断题,外部**直接内置默认码表**(不像厂商别名要等业主) | 内网 2026-07-30 |
| 🔴**`tbl_template.delivery_path` 和 `tbl_use_case_router.delivery_path` 不是同一套含义** —— 前者是**具体投递通道**(SFMC / ICCM / 3HK / AWS SNS),后者是时效/体系分类。**同名不同义,不要互读**(外部之前把两者当成一回事,已订正) | 内网 2026-07-30 |
| 🔴**告警标题里那 9 个文字 `...Path` 短语,和 `delivery_path` 1-9 是两套词汇** —— 枚举名是 "Time Critical (MDC)"/"Shared Batch" 这种,和 "WPB Servicing Realtime High Risk Path" 完全不是一类。**RB-56 Q3"这两者可能是同九个东西"的假设被推翻**。不要把告警短语映射到 1-9,那是编出来的连接 | 外部 2026-07-30 |
| `delivery_path` 的 **7 在枚举里有定义,但 UAT 导出里没有行** —— "定义了但这里没用到" ≠ "没定义" | 内网 2026-07-30 |
| ⭐**`HTCL OLD` 不能断言已下线,也不能折叠到 `HTCL`/`3hk`**:router 表仍有 9 行,其中 2 条 channel-rule 关联成功且 **channel-rule 与 use-case 都 status=Y**;代码镜像仍有 `MessageRouterTopicEnum.HTCL_OLD_SMS` / `HTCL_RT_OLD_TRACKING` / 独立 `htcl_old_sms` topic;但 topology 里**没有**独立的 `*-htcl-old-*` delivery-job 仓库 → 结论只能是"明确标 OLD 的 legacy 线路,但不能宣称已下线"。最终确认要查生产近 30/90 天 `route=HUTCHISON_RT_SMS` / `router=HTCL_OLD_SMS` 的消息量 | 内网 2026-07-30 |
| ⭐**AWS HK / SG 要分开算,但保留共同的 `sns` 父级**:router 表分别记录;Java 分别定义 `AWS_HK_SNS_PUSH` / `AWS_SG_SNS_PUSH`;topology 里 awshk 4 个 / awssg 5 个 delivery job;代码大体 WPB→SG、CMB/WSB→HK。口径 = `provider_family=sns` + `region=hk\|sg` + `canonical_vendor=awshk\|awssg`。**区域故障分开算,AWS SNS 全局故障再合并** | 内网 2026-07-30 |
| ⚠️**那 3 个厂商别名仍未经业主签认** —— 内网给的是**建议**:HK→`awshk`、SG→`awssg`、HTCL→`3hk`(仓库命名那条早已确认,但 **router 表里的 `HTCL` 是否同一家仍需业主确认**)。所以外部代码**仍然 0 条内置别名**,只按显示名**字面**拆出 family/region/lifecycle(这是可核对的弱断言),不断言 canonical 厂商 | 内网建议 + 外部 2026-07-30 |
| **RB-59 检查 1 的分母已解释**:`tbl_use_case_channel_rule` 共 **6217** 行,其中 **258 行四列键不完整**,余 **5959 行可比较** → 2967 关联成功 → 1628 关联后 vendor 非空 | 内网 2026-07-30 |

---

## 三、AIOps / MCP(2026-07-28 起)

### RUNBOOK-57 box 验证结果(2026-07-29,内网 Codex 跑的)

| 事实 | 出处 |
| --- | --- |
| **仓库识别在真实数据上复现:466/500=93.2%,和 RB55 完全一致,抽查 10 条 0 认错** | RB-57 |
| 34 条未命中 = 28 条(7 个不在 460 名册里的服务)+ 4 条 sidecar + 2 条 DynamoDB —— **都是仓库名不在名册,不是解析器的问题** | RB-57 |
| ⚠️**指标词表覆盖不足**:247/500 识别,253 空,其中 242 条来自两个没配的指标(`RunningTaskCountLowerthanDesired`、`StorageUsage`)。**已修**:补进 `config/alarm_patterns.json` | RB-57,已修 |
| ⭐**use case 链路在真实数据上返回 0** —— **根因已找到,是代码 bug,不是数据问题**:`incident.py` 直接调 `messages.reverse_lookup_use_cases()`,**绕过了 `usecase_catalog` 里已有的同环境保护**(那段代码的注释里写着这正是它以前的 "defect #2:UAT 覆盖率算到了过期的 dev/SCT 路由表上")。已修:先过 `route_dimension()` 闸门,**算不出来就明说算不出来,绝不返回一个会被读成"无业务影响"的 0**;并补了 `channel_upper_bound`(渠道级上界,今天就能用)| RB-57 → **已修 2026-07-29** |
| ⚠️ 但**底层数据缺口仍在**:`message_edges.csv` 255 个 topic vs 用例路由快照 20 个 topic,精确重合仅 3 个且都不属于告警仓库。所以**精确的 repo→use case 答案目前仍然拿不到**,只是现在会**如实说"拿不到"**而不是假装是 0。真正的解法是 `tbl_use_case_router` 摄取(RB-54)—— ⚠️**2026-07-30 部分推翻**:表摄取了,但四列回连只有 49.79%,所以它最多补上一半,不是完整解法 | RB-57 |
| Fail-closed 三条全部通过(`something broke`/`CMB Postman V3 failing`/空字符串 → 全部拒绝,没有猜仓库) | RB-57 |
| 真实聊天里助手会自动调用工具,并如实声明"只是影响面不是根因"等安全声明 | RB-57 |
| `config/alarm_patterns.json` 这个旋钮改了之后**确认生效** | RB-57 |

| 事实 | 出处 |
| --- | --- |
| ⭐**`webapp/mcp_client.py` 是外部(Claude)的**,不是内网的 —— `OWNERSHIP.json` `webapp/** → external`,RB-55 第 2 步,内网自己的回报也这么说。分工无分歧:内网管**名字/地址/开关**(数据与环境),外部管**传输/调用逻辑/prompt** | 业主问 + 三方一致 2026-07-30 |
| 🔴**`SDLC_MCP_TOOLS` 是"整体替换"配置文件,不是叠加。** 后果:安全不变量(`never_expose`)**不能只活在可覆盖的那一层** —— 一份漏抄该段的本地文件曾能解锁所有执行类工具。已修:禁用基线写进代码,配置只能往上加。**通用教训:任何 env 可覆盖的配置路径,都要先问"覆盖文件不完整时会怎样"** | 外部 2026-07-30,b0aee7a |
| ⚠️**盒子上的 `SDLC_MCP_TOOLS` 会让"校验已提交配置"的测试误判** —— 那类断言必须显式忽略环境变量,否则它考核的是本地文件。已修 | 外部 2026-07-30,b0aee7a |
| **`tbl_use_case_channel_rule` 有没有自己的 `business_category` 列,已不再阻塞代码** —— 该列已"乐观绑定":有就用(连接变成两张表,没有主数据行的用例也能拿到权威厂商),没有就是空字符串、维持现状。答案两种都不需要改代码。且两表**不一致**时会显式报冲突,不静默取一个 | 外部 2026-07-30 |
| **MCP 可以随便用** —— 组织上已放行 | 负责人 2026-07-28 |
| LogDream = **旧式 SSE**,6 个工具;CloudWatch = **Streamable HTTP**,30 个工具。客户端都不需要 token | RB-55 |
| **MDC Portal (8094) 全路径 404** —— 端口有 HTTP 但 MCP 路由没起。**不影响我们**:唯一依赖它的重发判断已决定不做 | RB-55 |
| **CloudWatch 告警名 93.2% 内嵌完整仓库名**(466/500;ECS 内 94.3%)。所以认仓库靠**查字典**,不靠解析命名格式 | RB-55 |
| **三个时区并存:CloudWatch=UTC / LogDream 默认=Asia/Hong_Kong / 服务器=GMT。** 时间不带时区一律当作**不确定**,绝不默认本地时间 | RB-55 |
| 日志文件真名是 **`otx_trace.log`**,不是同事 SKILL 里写的 `trace.log`。另有 `exception.log` / `sftp.log` / `*_YYYYMMDD` | RB-55 |
| 日志路径 = `/apps/<app>/log`,**source 内没有实例子目录** | RB-55 |
| LogDream app 名和仓库名**直接同名率 0%**,按规则(去 `mc-hk-hase-` 前缀、去 `-job`/`-api` 后缀、kebab→camel)唯一命中 **~36%** | RB-55 |
| CloudWatch alarm 自身**没有 tag**;对应的 ECS service 有 10 个 tag key,**有 `owner`,没有 `application`/`support group`** | RB-55 |
| `list_alarms` 单次上限 500,500 条要 **26.4 秒** —— 不能放进实时排查路径 | RB-55 |
| 那 9 个文字 Path 名(`WPB Servicing Realtime High Risk Path` 等)是 **9 个,不是 8 个**。9 个名字 vs 1–9 枚举(7 在 UAT 快照里没用例)—— **假设,未证实** | RB-56,我数的 |
| 告警分布那份 Confluence 是**同事个人整理的,可能不全**。"338 条 / 53%" **不能用来做决策** | 负责人 2026-07-28 |
| 那 9 份 AIOps `SKILL.md` **在我们镜像里 0/9** —— 镜像版本停在 2026-04~05,它们不在我们镜像的仓库里 | RB-55 |
| **两个 LogDream source 都是生产**,但放的日志不一样。所以默认**两个都查**,每条证据标明来自哪个 source(具体哪类日志在哪边,待内网查实) | 负责人 2026-07-29 |
| 🔴**source 名是 `hkl`(小写字母 L),不是 `hk1`(数字一)** —— 外部代码里一直硬编码成 `hk1`,`hkp3` 正常但 `hkl` 侧全部被服务器拒绝,**等于静默丢掉一半日志覆盖**(而"拒绝"如果被当成"没有匹配"就会被读成"没问题")。已修:代码默认值改对,且 source 列表改为**从 `servers.logdream.sources` 读**(环境词表归内网),并在查询前逐个 source 向服务器核验、被拒的 source 明确点名。⚠️ RUNBOOK-56 当时就注意到主机名 `hk125254508`/`hkl25254508` 的 1/l 歧义,却没把同样的怀疑用到 source 名上 | 内网 2026-07-30 (RB-60 已提过一次) |
| 🔴**`list_logdream_apps` 必须传 `source`,而且两个 source 的 app 清单不同** —— 所以要**按 source 各调一次**,并且一个 app 只在它真实存在的 source 上查(拿不存在的 app 去查会返回空 → 又被读成"没问题")。已修 | 内网 2026-07-30 |
| 🔴**MCP 调用有四种结果,必须分流**:传输失败(抛异常)/ 工具跑了但报错(`ok:False`)/ 成功但零命中(`text` 空)/ 成功且有内容。**工具报错时 `text` 非空**,所以不检查 `ok` 就直接读 `text`,会把"未知 source hkl"这类错误正文包装成日志证据,把**调用失败报告成"查到了日志"**。已修:`_tool_outcome()` 四路分流,报错走 `not_investigated` 且禁止进 evidence | 内网 2026-07-30 |

### 2026-07-29 的 Task_Scope 截图读到的(照片,标注为"读到"而非"实测")

| 事实 | 为什么重要 |
| --- | --- |
| **最大的告警家族(General SHP API Error)走的是 Portal MCP,不是 CloudWatch,也不是 LogDream。** 流程 = `trackId` → MDC Portal → `check_sms_resend_need` → resend / do_not_resend | ⚠️**Portal (8094) 现在是 404**。所以"Portal 挂了不影响我们"这句话**只在我们不碰最大家族时成立**。要覆盖它,得先有人修 Portal |
| 排在前三的告警家族(338 / 78 / 69)**全部**都是重发判断,不是根因分析 | 同事那套 AIOps 的主线就是重发判断。我们**不重建**它,我们做他们答不了的**聚合**层(78 条告警涉及哪些用例/渠道/要不要通知业务方) |
| ⭐**他们的输出里带 useCase ID** —— 读到 `[M2101] FPS Inward credit Success`、`[M9114] Add Registered Payee Notification`、`[N0278] DSP timecritical sms & email` | **这是 SHP 家族的入口。** 那类告警文本里没有仓库名,但有 useCase ID → 直接接进我们的用例目录 → 渠道/业务含义/该通知谁。已实现:`incident_impact` 现在也认 useCase ID |
| 输出里的 `Route=CSL_SVC_RT_SMS` / `Route=CM_HTTP_SMS` | 长得像 `route` 列的取值 —— 可能是 RB-54 问题 1 那个连接键的实物。**待验证** |
| 输出里的 `Template=LST2.GEN.RBWM_FPS_ICT_SUC` / `Template=DSP` | 模板名,我们目前没有这一维 |
| 厂商以文字出现:`CSL outbound API` / `CM gateway` / `HTCL outbound proxy` | 对应我们白名单里的 csl / cm / htcl(→3hk) |
| 决策枚举实物确认为 `resend` / `do_not_resend` | 和我们设计里的三值(含 `unknown`)一致 |
| remark 列里有 SQL 兜底:`SELECT * FROM schema01.tbl_csl_sms_segment WHERE mdc_tracking_id=... AND created_day=...` | 说明除 Portal 外还有一条 DB 校验路径 |

---

## 四、还没确认的(别当成已知)

| 问题 | 卡在哪 | 出处 |
| --- | --- | --- |
| ~~两个 source 哪个是生产~~ | ✅**已答:两个都是生产,日志不同。** 剩下的小问题:哪类日志在哪边(source 名见上,是 `hkl` 不是 `hk1`) | 负责人 2026-07-29 |
| ~~`MDC Alert - General SHP API Error` 是谁发的 / 原始文本长什么样~~ | ✅**查尽了:不是 CloudWatch Alarm,LogDream 也 0 命中,镜像无模板。** 剩下的只能问监控 owner:那 338 条是哪个系统发的(Splunk?邮件规则?) | 截图 + 内网 2026-07-29 |
| **Portal MCP (8094) 什么时候能修好** —— 现在挡着最大的告警家族 | 待对方团队 | RB-55 A1 |
| **那 4 个 vendor 显示名的 canonical 映射,业主签认** —— 内网已给出**有据的建议**(HK→awshk / SG→awssg / HTCL→3hk / HTCL OLD 保留原文 + lifecycle),`awshk`/`awssg` 本来就在我们白名单里,所以这是**可核对**的映射而非猜测。**只差业主一句"是"**;签认后填 `config/usecase_columns.json` 的 `validation.vendor_display_aliases`,**代码零改动** | 待 owner 一句确认 | 2026-07-30 |
| **`HTCL OLD` 到底还有没有流量** —— 唯一的判定方式是查生产近 30/90 天 `route=HUTCHISON_RT_SMS` / `router=HTCL_OLD_SMS` 的消息量,以及 `htcl_old_sms` topic 有没有 consumer/deployment | 待生产查询 | 内网 2026-07-30 |
| ~~**`tbl_use_case_channel_rule` 有没有自己的 `business_category` 列?**~~ | ✅**已不阻塞代码**:该列已乐观绑定,有就用(连接变两张表)、没有就空字符串。两种答案都零改动。仍值得一问,但只是为了知道覆盖率能提高多少 | 外部 2026-07-30 |
| ~~两个 SLA 列的**单位**(ms/s/min)~~ | ✅**已答:毫秒**(代码证据,见上)。剩下的只是业主正式签认那一张单 | 内网 2026-07-30 |
| ~~`delivery_path` 1–9 ↔ 文字路径的对照~~ | ✅**已找到并已落代码**(2026-07-30,`DeliveryPathEnum.java` + `BillingReportService` + portal 前端三处一致)。**不需要再问业务方。** 见上面第二节 | 内网 2026-07-30 |
| ~~三个读日志工具的确切参数~~ | ✅**不用问了。** 改成 `config/mcp_tools.json` 的 `"?"` 占位,内网对着 `tools/list` 直接填,不用来回拍照 | 负责人建议 2026-07-29 |
| `Route=CSL_SVC_RT_SMS` 是不是 `route` 列的取值 | 可自查 —— **现在更要紧**:`delivery_chain` 的 `route_hint` 通路建立在这个假设上 | 截图 2026-07-29 |
| 🔴**为什么 `message_edges.csv`(255 topic)和用例路由快照(20 topic)几乎不相交** —— 代码侧的误用已修,但**数据缺口还在**:精确的 repo→use case 仍然算不出来 | 曾以为靠 `tbl_use_case_router` 摄取就能补上这一跳 —— ⚠️**2026-07-30:表已摄取,回连仅 49.79%**,所以这一跳只能补一半,剩下的仍要靠 topic 命名或业务方 | RB-57 |
| `business_category` 33/37 是什么 | 缺数据,但**已量化为共 7 行**(33×1/37×6)→ 降级 | RB-53,内网 2026-07-29 |
| 日志里到底有没有客户数据(100 行抽样没命中,**但按"有"处理**) | 待内网 | RB-55 D10 |
| 事故结论能不能落盘、存哪、留多久 | 待合规 | RB-55 D11 |
| ~~内网 Codex 能不能推公网?~~ | ✅**答案是不能**(负责人 2026-07-30 确认,且此前已说过)。后果:内网写的旋钮文件(如 `config/usecase_columns.json`)**只存在于盒子上**,外部**不要**在同路径提交同名文件(会在他们 pull 时冲突/覆盖)。做法:外部代码**存在则读、缺失则用内置默认值**,盒子永远赢 | 负责人 2026-07-30 |
| 盒子 `/home` 100% 满了 | 待运维 | RB-55 |

---

## 五、变更记录

| 日期 | 谁 | 加了什么 |
| --- | --- | --- |
| 2026-07-28 | 外部 | 建档。汇总 RB-48~57 + 业主 2026-07-27/28 的确认 |
| 2026-07-29 | 外部 | 加"全链路到出口"一行。起因:问 M2050 的完整配置,答"无法确认"却说不出原因,且链路只到 ingress |
| 2026-07-30 | 外部 | 录入内网 RB-54 摄取结果(router 247 行 / 49.79% 回连 / vendor 58.7% 空 / mixed_export_times)+ 划掉 delivery_path 与 SHP 正文两条 |
| 2026-07-30 | 外部 | 拿到四列自然键 + vendor 取值域 → 建成 router 连接(4 档 `vendor_selection`);记下 AWS 区域折叠陷阱;把"内网能不能推公网"结成定论 |

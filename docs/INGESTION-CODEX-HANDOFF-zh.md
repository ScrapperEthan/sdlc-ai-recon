# 摄取层总接手说明 —— 哪些归内网 Codex,哪些必须过人

> 这份是 `MDC-SHEET-CODEX-HANDOFF-zh.md` 的推广版:那份只管一张 xlsx,这份管**全部 5 个数据摄取点**。
> 目标很明确:**"数据/表/仓库名变了" 应该只改一个 JSON,不改 Python,也不需要拍照给 Claude。**

---

## 1. 归属规则(一句话)

**看"什么事件会让它需要改":**

| 触发事件 | 归谁 | 举例 |
| --- | --- | --- |
| 数据、表结构、仓库命名变了 | **内网 Codex** | 列改名、加渠道列、加 flag、取值域变、新厂商出现 |
| 想让助手**多会一件事** | **Claude** | 链路图、新工具、新报表、prompt、前端 |
| 某个东西**到底什么意思,得有人拍板** | **过你一次**,之后落成配置文件 → 从此归 Codex | `rule_text` 的 `>` 是什么意思 |

第三类是关键:它**只过一次**。答案一落进配置文件,这个概念以后的所有变化都归 Codex,不再找你。

---

## 2. Codex 拥有的旋钮(改这些,不要改 Python)

> ⚠️ 下面 `config/` 目录下的文件由 `docs/specs/ingestion-ownership-seam.md` 建出来;**建完之前**,
> 第 2、3、4、5 项的词表还在 Python 里,暂时只有第 1 项(MDC 表)是通的。

| # | 摄取点 | 旋钮文件 | 常见改动 |
| --- | --- | --- | --- |
| 1 | MDC 仓库清单 xlsx | `config/mdc_sheet_schema.json` | 列改名 → 加 `aliases`;新渠道 → 加 `channel_flags`;新 Y/N flag → **什么都不用做**(自动捕获) |
| 2 | UAT 三张表 | `config/usecase_columns.json` | 列改名 → 加 `aliases`;新列 → 加一个字段;日期格式变 → 加 `date_formats` |
| 3 | 仓库名 → 渠道/厂商 | `config/naming_vocab.json` | **新厂商 → 加进 `known_vendors`**;厂商别名(如 `htcl`→`3hk`)→ 加 `vendor_aliases`;新渠道 → 加 `channels` |
| 4 | 消息生产者识别 | `config/producer_seeds.json` | 新的 `*Producer` / 发送基类 / 发送方法名 |
| 5 | 业务枚举与语义 | `config/business_enums.json`、`config/rule_text_semantics.json`、`config/source_system_aliases.json` | 业主给了答案之后填进去(见第 4 节) |

**四条铁律(每个旋钮都一样):**

1. 文件删掉 → 回落到 Python 内置默认值,**不崩**。
2. 认不出的列/取值 → 进"例外报告",**不崩**、不静默丢弃。
3. **块级替换,不是深合并** —— 覆盖某个块要给出整块。
4. `_README` 键会被忽略,所以每个旋钮里都写着自己的用法。

改完只看一件事:**例外报告里还有没有本该绑上的列**。

---

## 3. 每次跑完,只回传一个文件

```bash
python refresh.py
```

然后回传 **`index/reports/INGESTION-EXCEPTIONS.md`** —— 就这一个文件,几行字,一张截图能拍完。
它包含:未绑定的列、解析成 `unknown` 的厂商/渠道、行数突变、盒子本地 override 的存在性、
以及还在等业主答案的空位。

**干净的时候它就是一堆空章节** —— 那就等于"这次不用找 Claude"。

> 不要回传原始数据、不要回传仓库名册、不要回传 `Remark` 之类的自由文本(有敏感信息,引擎已默认排除)。

自检清单:

- [ ] `python -m pytest` 全绿
- [ ] 例外报告里没有"本该绑上却没绑上"的列
- [ ] `mdc_roster.json` 的 `count` = 表里仓库行数
- [ ] 产物字段结构没变(见 `MDC-SHEET-CODEX-HANDOFF-zh.md` 第 4 节的硬契约)
- [ ] 没有把任何数据/名册/人名提交进 git

---

## 4. ✋ 必须过你(拍照/转述)的 —— 只有这 4 类

### 4.1 一次性:某张表**第一次**出现

只需要三样东西,不要整表:

1. **列清单**(表头一行就行)
2. **2~3 行脱敏样例**(能看出取值长什么样)
3. **关键列的含义**(哪列是主键、哪列指向渠道/仓库/系统)

之后这张表的任何变化都归 Codex,不用再来找我。

**当前待办**:如果那 5 张表导出来了(`tbl_use_case_router`、UAT 版 `tbl_event_router_usecase_topic`、
`_aem_template`、`_department_mapping`、`_ext_2way`),按上面三样给我就行。

### 4.2 一次性:业务语义拍板(Codex 和我都无权猜)

| 要问的 | 问谁 | 答案落到哪 |
| --- | --- | --- |
| `rule_text` 里 `>` / `&` / `\|` 到底什么意思 | MDC 系统开发/产品负责人 | `config/rule_text_semantics.json` |
| `business_category` **33** 和 **37** 的正式名称 | 业务负责人(**优先**,这 7 个用例可能正被运行时判为配置非法) | `config/business_enums.json` |
| `source_system` 有没有权威 CMDB 登记表 | 企业架构 | `config/source_system_aliases.json` |

这三个是**答案**,不是数据。答完写进配置,永久解决 —— 而且**不需要改一行代码**就会生效。

### 4.3 每次刷新,但只拍**5 行例外报告**

就是第 3 节那个文件。不是拍数据,是拍异常。如果内网 Codex 能 push,这一条也免了。

### 4.4 永久:决策类

比如"某个仓库要不要移出 MDC 范围""两个数据源互相矛盾时以哪个为准"。这类永远需要人拍板,
但频率极低,而且是一句话就能答的。

---

## 5. ✅ 以后**不用再拍照**的(现在还在拍的)

- 列改名 / 新增渠道列 / 新增 flag / 取值域变化
- 表行数变化、快照日期变化
- 仓库名里出现新厂商、新渠道
- 重跑 `refresh.py`、重建 CodeGraph
- **任何原始数据本身** —— 数据永远不该拍给我

---

## 6. 给内网 Codex 的指令模板(可直接复制)

> 数据/表更新了。请按 `docs/INGESTION-CODEX-HANDOFF-zh.md`:
> 1. **只改 `config/` 下对应的 JSON 旋钮**,不要改 Python 逻辑(引擎是 schema 灵活的,认不出的列不会崩)。
> 2. 跑 `python refresh.py`,再跑 `python -m pytest`。
> 3. 把 `index/reports/INGESTION-EXCEPTIONS.md` 贴回来(只贴这个文件)。
> 4. 产物字段结构不许改;不要提交任何数据/名册/人名。
> 5. 如果例外报告里有"本该绑上却没绑上"的列,先补 `aliases` 再重跑;补不了的再回报。

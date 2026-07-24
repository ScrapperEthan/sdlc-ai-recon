# 内网 Codex 接手说明 —— MDC 仓库清单表(MDC_Repo_List_Analysis*.xlsx)摄取

> 目的:MDC 表会不定期更新(改列名、加渠道、加 flag、换取值)。**这块摄取代码交给内网 Codex 维护**;
> 下游的检索逻辑、工具、prompt、链路图由 Claude 维护。两层之间靠**产物文件**对接,互不影响。

---

## 1. 边界:Codex 拥有什么

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| **旋钮(常动)** | `mdc_sheet_schema.json` | 列映射配置。**99% 的表更新只改这一个文件,不碰 Python。** |
| **引擎(极少动)** | `enrich_repo_tags.py` | 解析 xlsx 的逻辑。schema 灵活,一般不需要改。 |
| **产物契约(不许破)** | `index/repo_tags.mdc.json`、`index/mdc_roster.json` | 下游只读这两个文件。字段结构必须稳定(见第 4 节)。 |

**不归 Codex 管(别动)**:`enrich_repo_tags.py` 里 `reconcile()` / `markdown_report()` 段(那是拿表去比对
Claude 侧结构标签的 QA,属于消费侧);以及下游 `make_repo_tags.py`、`webapp/tools.py`、prompt、前端。

---

## 2. 日常维护:表更新了怎么办(基本只改 JSON)

引擎按"精确 → 别名 → 唯一模糊"匹配列,并把**任何 Y/N 列自动收进 `flags`**,只有 Repository 列是必需的;
认不出的列**不会报错**,只会在运行输出里列进 "Unbound schema fields"。所以:

1. **列名变了**(如 `MDC Common` → `Shared?`):在 `mdc_sheet_schema.json` 里该字段的 `aliases` 加上新表头文字。
2. **新增渠道列**(如加 `MMS`):`channel_flags` 里加 `"MMS": "mms"`。
3. **新增一个 Y/N flag**:**什么都不用做**——它会自动进每个仓库的 `flags`。只有当下游要专门用它时,才在这里给它命名。
4. **换了 tab 名**:改 `sheet_name`(即使填错,引擎也会退回用第一个工作表,不崩)。
5. **取值映射变了**(B/R、M/S、CMB/WPB):改 `enum_columns` 里对应的 `map` / `allowed`。

改完跑一次,看输出里的 **Unbound** 列表:本该绑上却出现在里面的,说明 alias 没配对,补上即可。

```
python enrich_repo_tags.py --sheet MDC_Repo_List_Analysis_v0.3.xlsx
```

---

## 3. 什么时候才需要动引擎(`enrich_repo_tags.py`,少见)

只有出现**全新的取值类型**(既不是布尔、也不是 `R/B` 这种简单枚举),或需要**新的产物字段**时才改。
这种改动请保持第 4 节的产物契约不变;拿不准就找 Claude 一起改下游。

---

## 4. 产物契约(改什么都不能破这个)

`index/repo_tags.mdc.json` —— 每个仓库一条:

```json
{
  "<repo 名(小写)>": {
    "mdc_common": false,          // 下游读
    "time_critical": false,       // 下游读
    "marketing_servicing": "",    // 下游读:"" | "marketing" | "servicing"
    "mode_declared": "",          // 下游读:"" | "realtime" | "batch"
    "business_line": "",          // 下游读:"" | "cmb" | "wpb"
    "channel_declared": [],       // 下游读:如 ["sms","email"]
    "flags": { "任意yn列名": true },  // 泛化捕获,可加不可减
    "attrs": { "非yn列名": "原值" }   // 泛化捕获
  }
}
```

**前 6 个字段是硬契约**(`make_repo_tags.merge_mdc` 和 `retriever/repo_tags.py` 直接读)。`flags`/`attrs`
可以只增不减。

`index/mdc_roster.json` —— 权威 in-scope 名册(**整库=MDC 的依据**):

```json
{ "source": "MDC_Repo_List_Analysis_v0.3.xlsx", "count": 380, "repos": ["...","..."] }
```

> 语义:**表里列出的每个仓库都属于 MDC**;`amet-*`(中东 AMET)和任何不在表里的仓库 = out-of-scope。
> 下游会用这个名册把 `list_repos` / 代码搜索限定在 in-scope 范围。所以**从表里删掉一行 = 把该仓库移出 MDC**。

---

## 5. 怎么给内网 Codex 下指令(可直接复制的模板)

> MDC 表更新到 v0.3,列名/渠道有变动。请**只改 `mdc_sheet_schema.json`,不要动 `enrich_repo_tags.py` 的逻辑**:
> 1. 把新表头文字填进对应字段的 `aliases`;新渠道加进 `channel_flags`;取值变化改 `enum_columns`。
> 2. 跑 `python enrich_repo_tags.py --sheet MDC_Repo_List_Analysis_v0.3.xlsx`,确认输出里 **"Unbound schema fields" 没有本该绑上的列**。
> 3. 确认 `index/repo_tags.mdc.json` 的 6 个字段合理,`index/mdc_roster.json` 的 `count` = 表里仓库行数。
> 4. 跑 `python -m pytest tests/test_enrich_schema.py tests/test_make_repo_tags.py` 全绿。
> 5. **产物字段结构不许改**(第 4 节)。完成后提交推送,并把新的 `count` 和 Unbound 列表贴回来。

---

## 6. 自检清单(Codex 每次改完对一遍)

- [ ] `pytest tests/test_enrich_schema.py tests/test_make_repo_tags.py` 全绿
- [ ] 运行输出的 **Unbound** 列表为空(或只剩表里确实不存在的列)
- [ ] `mdc_roster.json` 的 `count` = 表里的仓库行数
- [ ] 抽查几个仓库,`repo_tags.mdc.json` 里的 `channel_declared` / `mdc_common` 等和表对得上
- [ ] 没有把 `Remark`(敏感自由文本)写进任何产物 —— 引擎已默认排除,别在 schema 里把它当普通列加回来

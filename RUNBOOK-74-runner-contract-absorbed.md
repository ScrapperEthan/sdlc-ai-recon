# RUNBOOK-74 — 契约已吸收 + 你们那条测试断言我改了 + 一个会咬人的坑

回应 `RUNBOOK-73-SEND-BACK.md`。基线 `86bc32b` → 本轮，**1362 tests**（`test_db_readonly.py` 73 条）。

**引擎侧到此为止：不需要你们名字的部分，全部做完了。** 剩下的全是 Proxy 和你们的表清单。

---

## 1. §5 你们是对的，而且这个错误比它看起来严重

我写的是：

```python
assert not os.path.isdir(ROOT / ".github" / "skills")
```

我想表达的是「**这个 skill 不能被提交**」，写出来的是「**它不能存在于磁盘上**」。
而唯一必须跑这条测试的机器，正是那台**必须让它存在于磁盘上**的机器。

这是一个我应该自己看出来的类别错误 —— 和 RUNBOOK-70 那条「**app 出现在 live 列表里
只说明它存在，不说明我们被允许读它**」是同一种：**把两个不同的事实写成了同一个判断。**

按你们建议的 1 + 2 改了（两个都做，不是二选一）：

```python
git ls-files -- .github/skills     → 必须是空
git check-ignore -q .github/skills/anything  → 必须返回 0
```

第一条断言「现在没有被跟踪」，第二条断言「以后也不会被误加进来」。
git 不可用时 `skipTest`，不会在没有 git 的环境里假失败。

---

## 2. 目录名那个笔误：我把名字**删了**，不是改对

你们让我把 `ai-ai-readonly-db` 改成 `ai-mi-readonly-db`。我没改，我把整个名字从仓库里去掉了。

理由：**代码一个字都不需要它**（路径来自 `SDLC_DB_SKILL`），它只出现在一句注释里。
一个不被使用、又必须靠人抄对的名字，就是 RUNBOOK-60 那个 `hk1` / `hkl` 的完整配方 ——
**写错不会报错，只会一直安静地错着**。而这次它连报错的机会都没有，因为没人会执行一句注释。

现在 `config/db_queries.json` 里的说法是「runner 模块的绝对路径来自 `SDLC_DB_SKILL`」，
**你们的目录叫什么，这个仓库不知道也不需要知道**。

---

## 3. P0-a 返回形状：已填进配置，并且我多做了一步

`runner.response` 现在是 shipped 默认值：

```json
{ "rows": "rows", "columns": "?", "row_format": "dict" }
```

`columns: "?"` 是**正确答案而不是没填完**这一点，我写进了配置注释里，
免得下一个看到它的人以为是个待办。

**多做的一步**：你们的返回里自带四个证据字段，我不是拿了 `rows` 就走 ——
**逐个本地复核**：

| 字段 | 对不上时 |
| --- | --- |
| `ok` | 为假 → 不读 rows，报 `not_ready` |
| `transaction_read_only` | 不为 `true` → **丢弃这次结果**。只读是这条路的前提，不是偏好 |
| `environment` | 和我们请求的不一致 → 丢弃。从一个我们没寻址的库回来的答案不能用 |
| `row_count` | **小于**我读到的行数 → 说明我读的不是你们数的那个 list，丢弃 |

**字段缺失不算失败** —— 你们没发的字段我不做任何断言。但字段在、且对不上，就不用这次结果。

`row_count` 只在**小于**的方向上判失败：它是在你们的 `LIMIT` 包装之前还是之后计数的，
是你们的自由；**大于**不是我读错的证据，所以不当缺陷。（如果你们能顺口说一句是哪一种，
我可以把这条收得更紧；不说也不影响。）

这一条的出处是 keyword 那个 P0：**第二道检查必须不依赖对方**。你们已经把证据放在返回里了，
不查它才是浪费。

---

## 4. P0-b 失败即 raise：已按你们说的接，但分了两档

你们写的是「产出 `ok:false`、`state:not_ready`」。我做的是 `ok:false` + **两档 state**：

| state | 什么情况 |
| --- | --- |
| `not_ready` | 异常类名在 `runner.not_ready_exceptions` 里 —— 现在是 `OperationalError` / `InterfaceError`，**即你们说的 UAT Proxy 认证故障** |
| `error` | 其他异常 —— runner 自己的拒绝、安全检查、结果上限 |

**对使用者两者完全一样**：`ok:false`、**没有 `rows` 键**、`means_no_data:false`、
同一句「这不是空结果」的 hint。分档**只是给排障看的**：一个是通路没接好，
一个是问了但被拒。你们那句要求（不能写成 `rows: []`）是有测试钉住的。

类名是**你们的**，所以在配置里，不在代码里。psycopg 换个异常类、或者 runner 加一个自己的
连接异常，改配置就行。

CLI 那个 `{"ok": false, "error_type": ..., "message": ...}` + exit code 2 的形状我**没有读** ——
你们说了那不是 Python API 的返回值。

---

## 5. P0-c / P1-a：参数不动，LIMIT 语义写进配置注释

- **参数**：维持 `%(name)s` + dict，不切位置参数、不拼字符串。有测试钉住
  （传一个 `UC1' OR 1=1 --` 进去，断言 SQL 一字未变、值走绑定）。
- **LIMIT**：你们说 runner 会包成
  `SELECT * FROM (<我的 SQL>) AS readonly_result LIMIT <safe_limit>`，在数据库端执行前加上。
  已写进 `config/db_queries.json` 的填写说明：**不必自己加 LIMIT，但大表必须写 WHERE ——
  LIMIT 替代不了合理的过滤、索引和 15 秒 timeout**。

  顺便确认一件事：你们的包装在**我的语句闸门之后**，所以我拒绝 `SELECT *` 和你们的
  `SELECT * FROM (...)` 不冲突 —— 我检查的是我发出去的那条。

---

## 6. P1-b schema：闸门做了，但你们的 schema 名我没写进这个仓库

`schemas` 填上之后会**强制校验**：命名查询里每张表都必须落在这些 schema 内，
拼错一个字母在**发出调用之前**就被拒，而不是去查了一张不该查的表。
留 `["?"]` = 只要求「表名必须 schema 限定」，不校验是哪一个。

**但仓库里我留的是 `["?"]`。** 你们说 `schema01`/`schema11` 是真名不是占位 —— 我信，
但我不把你们的真实 schema 名写进这个公共仓库，请填在本地文件里（见下一节）。
有一条测试专门断言这个仓库里的 schema 列表是空的。

如果你们认为这两个名字本来就不敏感、希望我直接 ship，说一句，我改回来。**这是你们的判断。**

---

## 7. ⚠️ 一个你们没提、但下次 `git pull` 就会咬人的坑

`config/db_queries.json` 是**被 git 跟踪的**，而**你们推不了代码**。
这和 `index/*.json` 被 gitignore 掉、导致每个 knob 编辑都死在盒子上，是同一个陷阱的两面。

**准确的风险形状不是"冲突"，是整个 `git pull` 被挡住**：git 合并时如果发现一个被跟踪的文件
既有未提交的本地改动、上游又要更新它，会**直接拒绝这次拉取**。挡住的不只是这个配置文件，
是那次 pull 里的**所有**东西 —— 而这边推不回来，你们也修不了。

做法（**一条命令，不需要环境变量**）：

```bash
cp config/db_queries.json config/db_queries.local.json     # 然后只改这一份
```

`config/db_queries.local.json` 已加进 `.gitignore`，程序**自动优先读它** ——
我没有把它做成"要记得设一个环境变量"，因为盒子重建一次就会忘一次。
优先级：`SDLC_DB_QUERIES`（显式，最高）> `config/db_queries.local.json`（约定）>
`config/db_queries.json`（跟踪的模板，兜底）。

**并且这个坑现在会自己喊出来。** `db_query` 无参数调用返回的 `config_warnings` 里：

- 你在**被跟踪的模板**里填了真 SQL → 明确告诉你搬到 `.local.json`，并说明为什么；
- 你的本地副本**缺少模板后来新增的命名查询** → 列出缺哪几条。
  （是**替换不是合并**：本地文件就是全部配置。所以新增的查询不会自己长出来，
  但也不会有"哪一边赢"的模糊语义。）

另外 `tests/test_db_readonly.py::ShippedConfigTests` 那条断言的失败信息也改了：
如果有人在盒子上改了跟踪的那份，测试会红，而红的那句话直接告诉他怎么办 ——
不是一个看起来莫名其妙的失败。

**并且：本地文件不会削弱任何闸门。** 所有默认值都在代码里，配置只能收紧不能放松 ——
你们的本地文件哪怕整段漏掉 `not_ready_exceptions`、`allowed_environments`、`max_rows_hard_cap`、
`verify`，行为也和填了一样。（这是 RUNBOOK-58 的教训：`SDLC_MCP_TOOLS` **替换**了配置，
悄悄作废了我的 deny list。同样的错不犯第二次。）

---

## 8. 顺手修掉的一个我自己的 bug

上一版的 packet 里 `environment` 是**硬编码的 `"uat"`**，`provenance` 是 `db:uat/...`。
如果哪天有人把 `allowed_environments` 放宽，**包上会盖着 uat 的戳、装着别处的数据** ——
这比没有戳更糟，因为它会跟着行一起被复制到别的文档里。

现在标签从配置查（`u` → `uat`），未知环境会如实报原值 + 一句「这个环境本 build 没有审过标签」。
有测试钉住。

---

## 9. 请你们验的

```bash
git pull
python -m pytest tests -q                      # 期望 1362 passed
python -m pytest tests/test_db_readonly.py -q  # 73 passed
```

你们上轮那条失败（`.github/skills` 存在）现在应该过了 —— **在盒子上跑，那台机器上
skill 是存在的，这条测试才有意义**。如果还红，把 `git ls-files -- .github/skills` 的输出发我。

不需要 Proxy 修好，不需要真机：这一轮**没有任何东西依赖你们的环境**。

---

## 10. 离「接上」还有多远

**引擎侧：0。** 不需要你们名字的部分全部做完了，`--check` 一通就能查。

按你们 §6 的顺序，剩下的：

| 步 | 谁 | 卡在哪 |
| --- | --- | --- |
| 1. 吸收契约 + 修测试 | 我 | ✅ 本轮做完 |
| 2. **UAT read-role 的 RDS Proxy authentication** | IAM/RDS 管理员 | 🔴 **唯一的硬阻塞** |
| 3. `--check` 通过 | 你们 | 等 2 |
| 4. information_schema 表清单 + 列级 PII 分类 | 你们 | 等 2 |
| 5. 填 `snapshot_freshness` 的 sql / columns / source_tables | 你们 | 等 4 |
| 6. `SDLC_DB_ENABLED=1` + `SDLC_DB_SKILL=…` + 内部调用者验一次 | 你们 | 等 5 |
| 7. 单条查询改 `caller_policy: "product"` | 你们 | 等 6 全绿 |

**第 2 步一通，第 3 和第 6 步是当天的事**；真正需要时间的是第 4 步（列级 PII 分类）。
所以：**接通 = Proxy 修好当天；第一条对用户有用的回答 = 表清单和 PII 分类回来之后。**

第 7 步之前，产品在界面上说的是「数据库尚未接通 / 该查询尚未配置」，
**不会**把失败或 UAT 空结果说成生产不存在该数据 —— 这条是结构性的（`ok:false` 的包
根本没有 `rows` 键），不是靠提示词。

---

## 11. 仍然只有你们/owner 能给的

**数据库线**：Proxy read-role auth（第 2 步）、表清单与行数、首批表结构与唯一约束、
**PII/正文列清单**与脱敏 view、审计与触发器事实、UAT→prod 的差异、首批命名查询的业务优先级。
可选：`row_count` 是 LIMIT 前还是 LIMIT 后计数。

**RUNBOOK-71 遗留（未变）**：真实授权 tracking ID、能映射到 log group 的 alarm、
目标资源 ARN、LogDream 服务端 keyword 修复、`log.investigate` 的 strict 三件套、`/home` 磁盘清理。

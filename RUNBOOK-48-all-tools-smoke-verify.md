# RUNBOOK 48 (INTERNAL Codex / you) — full-tool smoke: exercise ALL 21 chat tools, deep-test CodeGraph

> **Motivation:** RUNBOOK-47 only exercises the MDC / `list_repos` workstream — 3 of 21 tools have a
> dedicated positive test, 12 have none, and **the two CodeGraph-backed tools (`unified_impact`,
> `call_graph`) — the most complex and most fragile — are completely untested.** This runbook gives
> every tool at least one smoke test and deep-tests the CodeGraph degradation paths, so a broken tool
> actually surfaces instead of hiding.
>
> **Companion spreadsheet:** `RUNBOOK-48-test-questions.xlsx` (committed at repo root) — a fill-in
> tracker: sheet `全工具冒烟` rows 2–24 = Part A's 23 rows, rows 25–30 = **Part D** (T22–T27, the
> stakeholder-added business questions). This runbook is authoritative if the two drift.
>
> **The load-bearing part is Part 0 + Part B (CodeGraph).** CodeGraph's failure mode is *silent
> degradation to grep*: when the `codegraph` CLI is missing from the server PATH, or a symbol lives in
> one of the ~66 un-indexed repos, `unified_impact`/`call_graph` return `callers.available: false` and
> fall back to lexical text hits — the answer still looks like a call-graph answer but is only a text
> match. If nobody tests this, "who calls X" has been quietly wrong the whole time. Part 0 tells you
> whether CodeGraph is even wired up on this box.
>
> **Data security:** repo ids and symbol names are fine to send back; raw source lines and any
> person-name-bearing data are not. Generated `index/*` stay gitignored.

---

# Part 0 — CodeGraph wiring prerequisite (run FIRST; frames all CodeGraph results)

```bash
# 0.1 — is the codegraph CLI actually on the PATH the webapp process sees?
python -c "import shutil; print('codegraph on PATH:', shutil.which('codegraph'))"

# 0.2 — is a build manifest present, and how many bundles built cleanly (returncode==0)?
python -c "import json,os; from retriever import config; \
p=config.CODEGRAPH_BUILD_JSON; \
m=json.load(open(p,encoding='utf-8-sig')) if os.path.exists(p) else {}; \
b=(m.get('bundles') or []); ok=[e for e in b if e.get('returncode')==0]; \
print('manifest:', p, '| exists:', os.path.exists(p)); \
print('bundles total/ok:', len(b), '/', len(ok))"
```

| Check | Meaning | Actual |
|---|---|---|
| `shutil.which('codegraph')` is a real path | If `None`, EVERY `unified_impact`/`call_graph` call is ALREADY in lexical-fallback mode — the call-graph feature is effectively OFF on this box. This alone would explain any "who calls X" answer being just grep. | |
| build manifest exists + N bundles `returncode==0` | How many bundles are actually indexed. Cross-check against the ~31 planned / ~390 of 456 repos indexed (66 missing) from [[all-repo-tags-plan]] / [[codegraph-build]]. | |

> If 0.1 is `None`: record it prominently and mark every T12/T13 row "degraded (CLI absent)" — the
> honesty tests in Part B then check whether the *assistant* admits this, rather than presenting grep
> hits as a call graph.

## 0.3 — Use Case dataset prerequisite (needed for T17-T20)
`index/usecase-snapshots/active` does not exist by default. Before the use-case tools, set (PowerShell)
and restart the app — same dataset as RUNBOOK-45:
```
$env:SDLC_USECASE_DATASET="index/usecase-snapshots/uat/20260720-1730"
```
Without it, T17-T20 (list_source_systems / usecase_impact / search_usecases / usecase_quality_findings)
come back unavailable or wrongly degraded — a FAIL there would be a setup artifact, not a real finding.

---

# Part A — one smoke per tool (23 rows; ask each via CLI/Q&A, record route + answer + PASS/FAIL)

`🟢` = should pass. `⚠️` = probe for over-assertion/under-reporting. `🔻` = CodeGraph degradation path
(needs Part 0 / Part B setup). `↩` = already covered by RUNBOOK-47, listed only for the coverage map.

| # | Tool | Type | Question | Expected route | Expected result | Route✓ | Pass? |
|---|---|---|---|---|---|---|---|
| T1 | impact | 🟢 | 改 mc-hk-hase-api-starter 会连累谁？ | `impact(repo="mc-hk-hase-api-starter", transitive=true)` | depended_on_by (downstream) + depends_on (upstream), counts from graph; a hub → many downstream | | |
| T2 | hubs | 🟢 | 哪些仓库被依赖最多、最不能改？ | `hubs(top=20)` | ranked top20; api-parent/starter/common near top | | |
| T3 | consumers | 🟢 | 谁在消费 marketing 的 CM SMS topic？ | `consumers(destination="<full marketing-cm_sms topic>")` | consumer repos. **Do NOT use the bare `"cm_sms"` substring** — it matches BOTH the marketing and servicing destinations (Codex, on the box). Use the full marketing topic string from `message_edges.csv`. | | |
| T4 | producers | 🟢 | 谁往 2way SMS reply topic 发消息？ | `producers(destination="hrn.hase.shared.notification.2way_sms_reply")` | producer repos (non-empty). **NOTE:** `producers(destination="cm_sms")` returns 0 on the box — a dud smoke; Codex verified `2way_sms_reply` has a real producer. | | |
| T5 | repo_routes | 🟢 | amet-mdc-hsbc-cm-outbound-api 收发哪些消息？ | `repo_routes(repo="amet-mdc-hsbc-cm-outbound-api")` | all produce/consume edges touching it | | |
| T6 | usecase_route | 🟢 | use case **K3002** 走哪个 topic？ | `usecase_route(use_case_id="K3002")` | its topic(s) + dev/SCT-snapshot-not-production caveat. Use **K3002** (Codex-verified: exists in the dev/SCT route snapshot). Also run the other two branches: **topic-only** (`usecase_route(topic="<topic>")` → search UCs by topic) and **pair verification** (both args → does this exact pair exist). | | |
| T7 | use_cases_for_topic | ⚠️ | &lt;某topic&gt; 变了还有哪些 use case 受影响？ | `use_cases_for_topic(topic="<topic>", exact=false)` | ALL routing UCs + total; **force the truncation path**: use the **marketing-batch topic (~69 UCs, Codex)** with a small `limit` so `truncated=true`, and confirm the answer says how many MORE exist — NEVER "none exist" for "none in snapshot"; must NOT also pass use_case_id. Also run one `exact=true` full-topic case. | | |
| T8 | list_repos | ↩ | (covered) | — | see RUNBOOK-47 #1-9/14-20/U1/U6 | — | — |
| T9 | search_code | 🟢 | 在 amet-mdc-hsbc-ingress-api 里搜 @PostMapping | `search_code(pattern="@PostMapping", repos=["amet-mdc-hsbc-ingress-api"])` | hits scoped to that repo only, not whole mirror | | |
| T10 | read_file | 🟢 | 读 amet-mdc-hsbc-ingress-api 的 IngressResource.java 40-60 行 | `read_file(path=".../IngressResource.java", start=40, end=60)` | line-numbered slice. **Also test the mirror-escape guard** (not tested before): `read_file(path="../AGENTS.md")` (or any `../` path) must be REJECTED ("path escapes mirror"), not read. | | |
| T11 | trace | 🟢 | use case **K3002** 的完整消息流是怎样的？ | `trace(use_case_id="K3002")` | stitched end-to-end async flow. Also run a **destination-based** trace (`trace(destination="<topic>")`) and an **unknown/partial** input (garbage id → honest "no route found / partial", not a fabricated flow). | | |
| T12a | unified_impact | 🟢 | 谁调用了 IngressService？(indexed symbol) | `unified_impact(seed="IngressService")` | **PASS = `callers.available=true` + real cross-repo callers cited to file:line.** Do NOT also require deps+message here: Codex found IngressService returns `resolved_repo=None` and empty dependency/message sections (the symbol doesn't resolve to a defining repo). Record that empty-deps result as a SEPARATE finding to investigate (symbol→repo resolution gap), not a smoke failure. | | |
| T12b | unified_impact | 🔻 | 谁调用了 &lt;未索引仓库的类&gt;？(find target in Part B) | `unified_impact(seed="<class>")` | **callers.available=false**, lexical fallback, and the answer MUST state it's a text-match fallback, not a real call graph | | |
| T12c | unified_impact | 🔻 | (codegraph CLI absent — see Part 0/B) any call-chain question | `unified_impact(seed="<any>")` | available=false + note "codegraph CLI not on PATH; lexical hits included"; answer must not claim a real call graph | | |
| T13 | call_graph | 🟢🔻 | codegraph explore IngressService 原始结果 | `call_graph(query="IngressService")` | raw explore output routed to the defining bundle. **Also run the two degradation paths** (like T12b/T12c): an un-indexed-repo symbol and (if CLI absent) any symbol — confirm it degrades honestly instead of emitting a raw dump that looks authoritative. | | |
| T14 | show_arch | 🟢 | SMS 渠道挂了会影响什么？ | `show_arch(kind="channel", value="sms")` | inline diagram, SMS chain highlighted + text path. **Also cover the other two kinds** (source-system=PEGA is in RUNBOOK-47 #13): `vendor` (e.g. "Sinch 出问题了" → `kind="vendor", value="sinch"`) and `use-case` (`kind="use-case", value="K3002"` → resolved to its declared source_system). | | |
| T15 | show_impact | 🟢 | 改 amet-mdc-hsbc-cm-outbound-api 会连累谁？(要图) | `show_impact(repo="amet-mdc-hsbc-cm-outbound-api")` | inline blast-radius + downstream(affected)/upstream(deps) counts, direction correct | | |
| T16 | source_system_impact | ↩ | (covered) | — | see RUNBOOK-47 #10/11/12 | — | — |
| T17 | list_source_systems | 🟢 | 有哪些上游系统？ | `list_source_systems()` | canonicalized list (MDC/PEGA/eAlert/PowerCard…) + counts; variants folded | | |
| T18 | usecase_impact | ⚠️ | use case **K3002** 是什么？渠道/上游/owner？ | `usecase_impact(use_case_id="K3002")` | full profile incl. rule_text AST. Use **K3002** (Codex-verified: exists in UAT master AND actually exercises `rule_text_ast.semantics="unconfirmed"`; M2050 does NOT cover a full UAT profile). While semantics=unconfirmed, must NOT read the AST as an asserted fallback/priority order. | | |
| T19 | search_usecases | 🟢 | HK 的 SMS use case 有哪些？ | `search_usecases(channel="sms", country="HK")` | paginated matches + "showing first N of M" | | |
| T20 | usecase_quality_findings | ⚠️ | 有哪些 use case 配置有问题？ | `usecase_quality_findings()` | severity-ranked findings + counts_by_severity; MUST say these are FLAGGED disagreements, not confirmed production failures | | |
| T21 | show_coverage | 🟢 | 有哪些 SMS 仓库？给我看全景 | `show_coverage(kind="channel", value="sms")` | inline 392-repo estate view filtered to sms | | |

---

# Part B — CodeGraph deep test (the reason this runbook exists)

## B1 — find a target class in an UN-indexed repo (for T12b)
**Use the manifest's `staged_repos`, NOT the repo tag's `bundle`.** A bundle can build cleanly while a
specific repo was never staged into it (Codex on the box: 31/31 bundles OK but only ~390/456 repos
staged, 66 not). The bundle-tag heuristic gives false positives — e.g. Codex found
`amet-mdc-hsbc-cm-outbound-api` flagged "un-indexed" by the bundle test, yet its `CmOutboundService`
call graph still works. Decide by what was actually staged:
```python
import json, os
from retriever import config, repo_tags
manifest = json.load(open(config.CODEGRAPH_BUILD_JSON, encoding="utf-8-sig"))
staged = set()
for e in manifest.get("bundles", []):
    if e.get("returncode") == 0:
        staged.update(e.get("staged_repos") or [])   # build_codegraph.py writes staged_repos per bundle
tags = repo_tags.load()
unindexed = sorted(r for r in tags if r not in staged)
print("staged repos:", len(staged), "| un-indexed:", len(unindexed))
print("sample un-indexed:", unindexed[:15])
```
Pick one repo from `unindexed`, find a `*.java` class in it (`ls mirror/<repo>/**/**.java`), use that
class name as the `seed`. **Then confirm it actually degrades before using it for T12b** — both must hold:
```python
from retriever import unified_impact
from webapp import tools
seed = "<class-from-an-unindexed-repo>"
print("bundle_root_for:", unified_impact.bundle_root_for(seed))          # expect None
print("callers.available:", tools.dispatch("unified_impact", {"seed": seed})["callers"]["available"])  # expect False
```
Require **`bundle_root_for(seed) is None` AND `callers.available is False`**. If either isn't true, the
symbol still routes somewhere — pick another. **Known-good degradation seed (Codex-verified stable):
`SapiAutoScanConfig`** — use it if hunting for a target is slow. Record which repo/class you used.

## B2 — the three CodeGraph outcomes, checked at the tool level (not just chat)
```python
from webapp import tools
for seed in ["IngressService", "<class-from-B1>"]:
    r = tools.dispatch("unified_impact", {"seed": seed})
    c = r.get("callers", {})
    print(seed, "-> callers.available =", c.get("available"),
          "| bundle_root =", r.get("bundle_root"),
          "| has fallback_hits =", bool(c.get("fallback_hits")))
```
| Seed | Expected `callers.available` | Actual | Notes |
|---|---|---|---|
| `IngressService` (indexed) | `true` **iff** Part 0 showed codegraph on PATH + its bundle built; else `false` (that itself is the finding) | | |
| `<class-from-B1>` (un-indexed) | `false`, with `fallback_hits` present | | |

## B3 — honesty check (chat level)
Ask T12a and T12b as chat questions. The FAIL condition is **the assistant presenting lexical
`fallback_hits` as if they were a real call graph** — e.g. stating "X is called by Y" from a grep hit
without flagging that the call graph was unavailable. The system prompt tells it to check
`callers.available` and fall back to `search_code`/`read_file` "only if available is false" — verify it
actually honors that and SAYS SO when degraded.

| Check | Expected | Actual |
|---|---|---|
| T12a answer | real callers, cited file:line, no "unavailable" caveat needed | |
| T12b answer | explicitly says the call graph was unavailable for this symbol and it's showing text matches instead | |
| Does it ever pass grep hits off as call-graph edges? | **Never** — that's the FAIL | |

---

# Part C — coverage guard (automation, so a future 22nd tool can't slip through untested)
Diff the registered chat tools against the tools this runbook names, so adding a tool without a test
fails loudly next time:
```python
import re
from webapp import tools
registered = {t["function"]["name"] for t in tools.TOOLS}
# the tools this runbook + RUNBOOK-47 exercise (keep in sync with the Part A "Tool" column):
tested = {
  "impact","hubs","consumers","producers","repo_routes","usecase_route","use_cases_for_topic",
  "list_repos","search_code","read_file","trace","unified_impact","call_graph","show_arch",
  "show_impact","source_system_impact","list_source_systems","usecase_impact","search_usecases",
  "usecase_quality_findings","show_coverage",
}
print("registered:", len(registered), "| tested:", len(tested))
print("UNTESTED (registered but not in the test set):", sorted(registered - tested))
print("STALE (in test set but no longer registered):", sorted(tested - registered))
```
Expected: both diffs empty (21 == 21). If `UNTESTED` is non-empty, a new tool shipped without a smoke —
add it to Part A before closing this runbook.

---

# Part D — 领导/业务方追加的跨工具业务问题 (T22–T27)

These six are **not** per-tool smokes — they are business questions a stakeholder added in a review
(会议追加), each answered by *combining* existing tools + judgment (closer to RUNBOOK-47's adversarial
style than Part A's one-smoke-per-tool). They do **not** change the 21-tool coverage invariant in Part C:
**T22 tests the *absence* of a read-only DB-catalog tool** (a capability boundary), and T23–T27 reuse
tools already registered. Companion rows: `RUNBOOK-48-test-questions.xlsx` sheet `全工具冒烟` rows 25–30.

Type tags are question-categories, not the Part A legend: `能力边界` capability-boundary ·
`口径边界` counting-caliber · `术语消歧` term-disambiguation · `深度逻辑` deep-logic ·
`消息图` message-graph · `渠道边界` channel-boundary. The hard numbers (T23) and repo/line cites below
were produced by Codex **on the internal box** against the real mirror + UAT snapshot — re-confirm there;
they cannot be re-derived from this GitHub-facing repo (mirror + `index/*` are gitignored).

| # | Tool(s) | Type | Question (源) | Expected route | Expected result (must-nots in **bold**) | Pass? |
|---|---|---|---|---|---|---|
| T22 | (no DB-catalog tool) | 能力边界 | MDC 一共多少张表？ | UAT snapshot manifest + 设计摘录（无只读 DB catalog 工具） | **无法权威回答 live DB 表总数，不能猜。** 可证：UAT snapshot 20260720-1730 导出 3 表（tbl_use_case 2,810 / tbl_use_case_channel_rule 6,217 / tbl_use_case_ext 2,660 行）；设计摘录列 8 张 use-case 表。两者都 ≠ live DB 总表数；真值需 DBA 只读 schema 导出。**不得**拿 3 / 8 / Java `@Table` 数冒充总表数。证据：`manifest.json`；`.tmp/use_case_db_design_tables.md` | |
| T23 | source_system_impact | 口径边界 | MDC 多少 use cases？HASE / non-HASE？ | `source_system_impact(source_system="MDC")` + UAT `tbl_use_case.snapshot.csv` 按 `group_member` 统计 | total 口径（snapshot 20260720-1730）：MDC **880**；HASE **819**；non-HASE **61**（HSBC 56 + 空白/未知 5，**不得**把 5 条空白静默算成 HSBC）。active：876 / 817 / 59；inactive 4。**回答须写明 snapshot 时间 + total/active 口径**，并说明「MDC」是当作平台全量还是 source_system 字段值（两者数字含义不同）。 | |
| T24 | list_repos + search_code/read_file | 术语消歧 | 与 SMS feedback status 相关的 repo？ | `list_repos(query="feedback"/"tracking")` + `list_repos(channel="sms")` + scoped search | **先澄清「feedback status」**：A 客户点击/行为反馈（`*-feedback-api`：Email / In-app-Push / WSB，非 SMS 回执主链）；B SMS 投递状态（更贴题）——状态 API `mc-hk-hase-htcl-tracking-api` / `amet-mdc-hsbc-cm-tracking-api`；共享 `api-rest-invoker` / `api-starter`；tracking jobs `svc-rt-gen` / `svc-bat` / `ssvc-bat-tracking-job`。**不得**把搜到的 36 个 tracking / 70 个 SMS 仓库不分层当答案。证据：HTCL `/v4/query-sms-status`、CM `POST /api/sms-status`、api-starter `querySmsStatus`。 | |
| T25 | list_repos + search_code/read_file | 深度逻辑 | 检查 PN 的 opt-in status，repo + 逻辑？ | `list_repos(query="preference")` + scoped `search_code(PushMessageValidator/AlertTypeEnum/PreferenceResource)` + read_file | 「opin」= opt-in。repos：`preferences-api` / `preferences-job`（入口）；`api-dispatch-core` / `api-delivery-core`（发送前把关）；`api-rest-invoker` / `api-common` / `api-domain`（共享）。逻辑：verifyPushPoint=false 跳过 → 客户资料 → device token（`NA` 视为无）→ Use Case + subscription → opt-in flag=Y 须匹配同一 alertType 且 alertChannelStatus03=Y → 否则抛 `NotOptInException`。**注意：opt-in 只证明「允许发送」，不证明已送达。** 证据：`PreferenceResource.java:38-62`、`PushMessageValidator.java`（dispatch 30-119 / delivery 33-128）。 | |
| T26 | list_repos + producers/consumers | 消息图 | 与 2-way SMS 相关的 repo？ | `list_repos(query="2way")` + `producers/consumers(destination="hrn.hase.shared.notification.2way_sms_reply")` + `repo_routes` | **不得只回答名字带 2way 的那 1 个仓库。** 主链：发出 `svc-rt-hr-htcl-2way-sms-deli-job`；收回复 `hutchison-inbound-gateway`；消费/对账 `reconciliation-report-job`；投递状态 `svc-rt-hr-tracking-job`（Htcl2WaySmsTracker/PollTracker）；共享 `api-delivery-core` / `api-tracking-core` / `api-dao` / `api-domain` / `api-rest-invoker`。证据：`message_edges.csv:206`（reply topic producer/consumer）、`:385`（outbound 边）。 | |
| T27 | list_repos + repo_routes + search_code | 渠道边界 | WeChat 是 personal 还是 company channel？ | `list_repos(channel="wechat")` + `repo_routes` + scoped `search_code(openid/template_id/corpId/agentId/qyapi)` | 代码最符合**腾讯微信公众号（Official Account）模板消息**——按 openid 发、appid/appsecret+access token、`/cgi-bin/message/template/send`（touser/template_id/data）。**既不是**个人 peer-to-peer chat，**也无证据**是企业微信/WeCom（无 corpId/agentId/qyapi）。delivery repos 仅 2 个：`ssvc-rt-gen-wechat-deli-job`（general）/`ssvc-rt-hr-wechat-deli-job`（high-risk）——差异是 general/high-risk，非 personal/company。证据：`SendWeChatMessageRequest.java`、`WeChatService.java:86-125`、`TencentWechatRouter.java`。 | |

---

# Send back
```
Part 0
 codegraph on PATH:        [ path | None ]
 bundles total/ok:         [ __ / __ ]   (vs ~31 planned; ~390/456 repos indexed)

Part A  (T1-T21)
 smoke (🟢) fails:         [ list # + what happened ]
 probes (T7,T18,T20):      [ over-asserted? honest caveats present? ]

Part B  (CodeGraph)
 B1 un-indexed repo/class used: [ repo / class ]
 B2 IngressService callers.available = [ ]   ; B1-class callers.available = [ ]
 B3 T12a real callers cited?  [ y/n ]
    T12b admitted degradation? [ y/n ]
    ever passed grep off as call graph? [ y/n — y is a FAIL ]

Part D  (T22-T27 领导追加)
 T22 refused to invent a live-DB table total?     [ y/n ]
 T23 stated snapshot time + total/active 口径?     [ y/n ]  (880/876; HASE 819/817; non-HASE 61/59)
 T24 disambiguated feedback vs SMS-status?         [ y/n ]
 T25 said opt-in ≠ delivered?                      [ y/n ]
 T26 gave the full 2way chain, not just 1 repo?    [ y/n ]
 T27 called it 公众号 (not personal, not WeCom)?    [ y/n ]
```

## Notes
- If Part 0 shows `codegraph` is NOT on PATH, that is the single most important finding in this runbook
  — it means the flagship "who calls X" capability has been silently answering with grep. Escalate:
  either install/PATH the CLI on the webapp host, or the two CodeGraph tools should FAIL LOUD (not
  silently fall back) so the degradation is visible. That's a follow-up decision, not a fix to make
  blind.
- Part A T1-T21 are smoke: they prove a tool *runs and routes*, not that every number is right. Depth
  per tool (e.g. use-case rule_text AST correctness) is other runbooks' job (RUNBOOK-45 etc.).
- `↩` rows (T8/T16) are intentionally not re-tested here — RUNBOOK-47 owns them.
- **KNOWN GAP — the MCP surface is out of sync (separate follow-up, not tested here).** `mcp_server.py`
  exposes only **17** tools via `@mcp.tool()`; it is missing the 4 newer chat tools **`list_repos`,
  `show_arch`, `show_impact`, `show_coverage`** (17 + 4 = the 21 in `webapp/tools.py`). So an MCP-capable
  client cannot reach those four, and there is no MCP transport-layer test at all. This runbook only
  covers the WEBAPP chat surface. Recommend a follow-up: bring `mcp_server.py` to parity with `TOOLS`
  and add an MCP smoke — tracked separately, not a blocker for this runbook.

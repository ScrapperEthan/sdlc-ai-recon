# RUNBOOK 48 (INTERNAL Codex / you) — full-tool smoke: exercise ALL 21 chat tools, deep-test CodeGraph

> **Motivation:** RUNBOOK-47 only exercises the MDC / `list_repos` workstream — 3 of 21 tools have a
> dedicated positive test, 12 have none, and **the two CodeGraph-backed tools (`unified_impact`,
> `call_graph`) — the most complex and most fragile — are completely untested.** This runbook gives
> every tool at least one smoke test and deep-tests the CodeGraph degradation paths, so a broken tool
> actually surfaces instead of hiding.
>
> **Companion spreadsheet:** `mdc_alltools_smoke_tests.xlsx` (handed to the boss directly, not committed
> — personal review/tracking artifact). Same 23 rows as Part A here. This runbook is authoritative if
> the two drift.
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

---

# Part A — one smoke per tool (23 rows; ask each via CLI/Q&A, record route + answer + PASS/FAIL)

`🟢` = should pass. `⚠️` = probe for over-assertion/under-reporting. `🔻` = CodeGraph degradation path
(needs Part 0 / Part B setup). `↩` = already covered by RUNBOOK-47, listed only for the coverage map.

| # | Tool | Type | Question | Expected route | Expected result | Route✓ | Pass? |
|---|---|---|---|---|---|---|---|
| T1 | impact | 🟢 | 改 mc-hk-hase-api-starter 会连累谁？ | `impact(repo="mc-hk-hase-api-starter", transitive=true)` | depended_on_by (downstream) + depends_on (upstream), counts from graph; a hub → many downstream | | |
| T2 | hubs | 🟢 | 哪些仓库被依赖最多、最不能改？ | `hubs(top=20)` | ranked top20; api-parent/starter/common near top | | |
| T3 | consumers | 🟢 | 谁在消费 marketing-cm_sms 这个 topic？ | `consumers(destination="cm_sms")` | consumer repos from message_edges | | |
| T4 | producers | 🟢 | 谁往 cm_sms topic 发消息？ | `producers(destination="cm_sms")` | producer repos | | |
| T5 | repo_routes | 🟢 | amet-mdc-hsbc-cm-outbound-api 收发哪些消息？ | `repo_routes(repo="amet-mdc-hsbc-cm-outbound-api")` | all produce/consume edges touching it | | |
| T6 | usecase_route | 🟢 | use case &lt;真实ID&gt; 走哪个 topic？ | `usecase_route(use_case_id="<ID>")` | its topic(s) + dev/SCT-snapshot-not-production caveat | | |
| T7 | use_cases_for_topic | ⚠️ | &lt;完整topic&gt; 变了还有哪些 use case 受影响？ | `use_cases_for_topic(topic="<topic>", exact=true)` | ALL routing UCs + total; on truncation say how many more; NEVER "none exist" for "none in snapshot"; must NOT also pass use_case_id | | |
| T8 | list_repos | ↩ | (covered) | — | see RUNBOOK-47 #1-9/14-20/U1/U6 | — | — |
| T9 | search_code | 🟢 | 在 amet-mdc-hsbc-ingress-api 里搜 @PostMapping | `search_code(pattern="@PostMapping", repos=["amet-mdc-hsbc-ingress-api"])` | hits scoped to that repo only, not whole mirror | | |
| T10 | read_file | 🟢 | 读 amet-mdc-hsbc-ingress-api 的 IngressResource.java 40-60 行 | `read_file(path=".../IngressResource.java", start=40, end=60)` | line-numbered slice | | |
| T11 | trace | 🟢 | &lt;真实use_case_id&gt; 的完整消息流是怎样的？ | `trace(use_case_id="<ID>")` | stitched end-to-end async flow | | |
| T12a | unified_impact | 🟢 | 谁调用了 IngressService？(indexed symbol) | `unified_impact(seed="IngressService")` | **callers.available=true**, real cross-repo callers + deps + message peers, each cited to file:line | | |
| T12b | unified_impact | 🔻 | 谁调用了 &lt;未索引仓库的类&gt;？(find target in Part B) | `unified_impact(seed="<class>")` | **callers.available=false**, lexical fallback, and the answer MUST state it's a text-match fallback, not a real call graph | | |
| T12c | unified_impact | 🔻 | (codegraph CLI absent — see Part 0/B) any call-chain question | `unified_impact(seed="<any>")` | available=false + note "codegraph CLI not on PATH; lexical hits included"; answer must not claim a real call graph | | |
| T13 | call_graph | 🟢 | codegraph explore IngressService 原始结果 | `call_graph(query="IngressService")` | raw explore output routed to the defining bundle | | |
| T14 | show_arch | 🟢 | SMS 渠道挂了会影响什么？ | `show_arch(kind="channel", value="sms")` | inline diagram, SMS chain highlighted + text path | | |
| T15 | show_impact | 🟢 | 改 amet-mdc-hsbc-cm-outbound-api 会连累谁？(要图) | `show_impact(repo="amet-mdc-hsbc-cm-outbound-api")` | inline blast-radius + downstream(affected)/upstream(deps) counts, direction correct | | |
| T16 | source_system_impact | ↩ | (covered) | — | see RUNBOOK-47 #10/11/12 | — | — |
| T17 | list_source_systems | 🟢 | 有哪些上游系统？ | `list_source_systems()` | canonicalized list (MDC/PEGA/eAlert/PowerCard…) + counts; variants folded | | |
| T18 | usecase_impact | ⚠️ | &lt;真实use_case_id&gt; 是什么？渠道/上游/owner？ | `usecase_impact(use_case_id="<ID>")` | full profile incl. rule_text AST; while semantics=unconfirmed, must NOT read AST as an asserted fallback/priority order | | |
| T19 | search_usecases | 🟢 | HK 的 SMS use case 有哪些？ | `search_usecases(channel="sms", country="HK")` | paginated matches + "showing first N of M" | | |
| T20 | usecase_quality_findings | ⚠️ | 有哪些 use case 配置有问题？ | `usecase_quality_findings()` | severity-ranked findings + counts_by_severity; MUST say these are FLAGGED disagreements, not confirmed production failures | | |
| T21 | show_coverage | 🟢 | 有哪些 SMS 仓库？给我看全景 | `show_coverage(kind="channel", value="sms")` | inline 392-repo estate view filtered to sms | | |

---

# Part B — CodeGraph deep test (the reason this runbook exists)

## B1 — find a target class in an UN-indexed repo (for T12b)
```python
import json, os
from retriever import config, repo_tags
manifest = json.load(open(config.CODEGRAPH_BUILD_JSON, encoding="utf-8-sig"))
built_bundles = {e["bundle"] for e in manifest.get("bundles", []) if e.get("returncode") == 0}
tags = repo_tags.load()
# repos whose bundle did NOT build (or have no bundle) → their symbols can't route to an index
unindexed = [r for r, m in tags.items()
             if (m.get("bundle") or "").strip() not in built_bundles]
print("un-indexed repos:", len(unindexed))
print("sample:", unindexed[:15])
# pick one, find a class file in it under mirror/, use its class name as the T12b seed
```
Pick one repo from `unindexed`, find a `*.java` class in it (`ls mirror/<repo>/**/**.java`), and use that
class name as the `seed` for T12b. Record which repo/class you used.

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

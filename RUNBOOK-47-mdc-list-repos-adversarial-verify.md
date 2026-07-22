# RUNBOOK 47 (INTERNAL Codex / you) — adversarial verify: `list_repos` completeness + disambiguation

> **After Sonnet 5 built `list_repos` (RUNBOOK-46, `984fa72`/`ac6323e`, 213/213 green) and RUNBOOK-46
> Part A2/B passed on the box (MDC = 45 = 21 amet-mdc ∪ 24 mc-hk-hase, zero overlap), the boss raised a
> harder question: is 45 actually the COMPLETE set, and will the assistant honor an EXPLICIT user
> instruction to include mc-hk-hase rather than silently trusting the sheet-derived count and stopping
> there?** This runbook is a 27-question adversarial suite — some are straight regressions (should
> reliably pass), several are deliberately designed to probe for NEW gaps the fixes may have introduced
> (esp. the "copy `count` verbatim" discipline backfiring into refusing to dig deeper when a user
> explicitly demands completeness).
>
> **Companion spreadsheet:** `RUNBOOK-47-test-questions.xlsx` (committed at repo root) is a fill-in
> tracker with the same 27 questions. This runbook carries the SAME questions as an executable markdown
> table so you don't need to open the xlsx — treat this file as authoritative if the two ever drift.
>
> **The most important deliverable in this runbook is Part C (U7).** It is the only question in the
> whole set that can actually PROVE OR DISPROVE whether the 45-count is missing any real MDC-connected
> `mc-hk-hase-*` repo. Everything else is chat-level behavior verification; Part C is a deterministic
> script run directly against `internal_edges.csv` + `message_edges.csv` on the real mirror.
>
> **PREREQUISITE — point the loader at the UAT Use Case dataset before Part A #10-12.** The
> source_system cases need an active dataset; `index/usecase-snapshots/active` does not exist by
> default, so set (PowerShell): `$env:SDLC_USECASE_DATASET="index/usecase-snapshots/uat/20260720-1730"`
> (same dataset as RUNBOOK-45) and restart the app. Without it, #10-12 come back unavailable or wrongly
> degraded, and a FAIL there would be a setup artifact, not a real finding.
>
> **Data security:** as always — `repo_tags.json`, the MDC sheet, and any generated `index/*.json` stay
> gitignored, never committed/pushed. Send back counts, repo-id lists (repo ids are not sensitive), and
> yes/no — no raw business-sheet columns beyond what's already in `repo_tags.json`.

---

# Part A — Group A–E: chat-level regression + probe (run each via CLI/Q&A)

For each row: ask the exact question, record the tool(s) actually called and the actual answer, then
mark PASS/FAIL against Expected. `✅` rows should reliably pass; `⚠️` rows are intentional probes — a
FAIL there is a genuine new finding, not a runbook mistake, and should be written up, not brushed off.

| # | Type | Question | Expected route | Expected result | Actual route | Actual result | Pass? |
|---|---|---|---|---|---|---|---|
| 1 | ✅ | MDC 有什么 API | `list_repos(query="mdc", mode="api")` | 4 repos (ingress-api / internal-notification-api / cm-outbound-api / cm-tracking-api) + endpoint lines via scoped `search_code` | | | |
| 2 | ✅ | campaign 相关有哪些仓库 | `list_repos(query="campaign")` | count from tool, all listed | | | |
| 3 | ✅ | 有哪些 tracking 仓库 | `list_repos(query="tracking")` | count from tool; must cover BOTH `amet-mdc-*-tracking-*` and `mc-hk-hase-*-tracking-*` if both exist | | | |
| 4 | ✅ | push 渠道有哪些仓库 | `list_repos(channel="push")` | count from tool | | | |
| 5 | ✅ | ingress 相关仓库有哪些 | `list_repos(query="ingress")` | count from tool | | | |
| 6 | ✅ | 列出 MDC 完整 repo list，要含 mc-hk-hase | `list_repos(group="mdc")` | **45** (21 amet-mdc-prefix + 24 mdc_common), all listed with `via` | | | |
| 7 | ✅ | MDC 系统一共多少个仓库 | `list_repos(group="mdc")` | **45**, must match #6 exactly (same session or not) | | | |
| 8 | ✅ | MDC 复用了哪些 mc-hk-hase 仓库 | `list_repos(group="mdc")` filtered to `via=mdc_common` | **24**, all `mc-hk-hase-*`, sourced to the MDC business sheet (not the name) | | | |
| 9 | ✅ | MDC 自己的应用仓库有哪几个（不含共享） | `list_repos(query="amet-mdc")` / `system="amet-mdc"` | **21**, no mc-hk-hase mixed in | | | |
| 10 | ✅ | MDC 出问题会影响哪些 use case | `source_system_impact("MDC")` | coverage funnel, ~880 use cases, paginated; must NOT call `list_repos` | | | |
| 11 | ✅ | MDC 挂了要通知谁 | `source_system_impact("MDC")` | layered owners, business_owners first | | | |
| 12 | ✅ | PEGA 有哪些 use case / 接了哪些流程 | `source_system_impact("PEGA")` | total/active 479/415; must NOT call `list_repos(query="pega")` | | | |
| 13 | ✅ | 架构图上 PEGA 从哪接进来 | `show_arch(kind="source-system", value="PEGA")` | inline diagram, upstream node highlighted, "declared not discovered" caveat stated | | | |
| 14 | ✅ | amet-mdc 一共几个仓库 | `list_repos(query="amet-mdc")` | **21** (prior model bug once said 22 — must not recur) | | | |
| 15 | ✅ | SMS 渠道下有多少个仓库 | `list_repos(channel="sms")` | count from tool, not eyeballed | | | |
| 16 | ⚠️ | MDC 有哪些 job | Ideally: `group="mdc"` result filtered to `mode=job`; realistically the tool may not expose `mode` on grouped results | Either a correct filtered list, OR an honest statement that grouped results don't carry `mode` and a fallback method was used — NOT a silent drop back to `query="mdc"` alone (which loses the 24 mc-hk-hase) | | | |
| 17 | ⚠️ | MDC 的 SMS 仓库有哪些 | Same shape as #16 but for `channel` | same bar as #16 | | | |
| 18 | ⚠️ | eAlert 有什么 API | `list_repos(query="ealert")` → empty/near-empty | Must recognize eAlert as a `source_system`, not fabricate an API list; should surface `source_system_impact` as the right tool instead | | | |
| 19 | ✅ | 整个系统一共多少个仓库 | `list_repos()` no filter | **392** (canonical universe) — must NOT say 456 (that's the raw mirror incl. 66 non-system extras, already rejected in RUNBOOK-22/46) | | | |
| 20 | ⚠️ | 列一下 whatsapp 渠道的仓库 | `list_repos(channel="whatsapp")` | honest count even if very small/zero; no padding to look substantive | | | |

---

# Part B — Group F: completeness-challenge + explicit-instruction (the "更狠" tier)

These specifically probe whether the earlier "copy the tool's `count` verbatim, never hand-count"
discipline (added to fix the 22-vs-21 miscount) has swung too far into refusing to look further when a
user explicitly demands it.

| # | Type | Question | Expected behavior | Actual | Pass? |
|---|---|---|---|---|---|
| U1 | ⚠️ | 列 MDC 仓库，必须列出所有相关的 mc-hk-hase | If an explicit-family guard exists (it does NOT yet — see Part D), it should force-enumerate the full `mc-hk-hase-*` family with MDC-relatedness flagged. As currently built, the honest answer is: 45 (business-sheet count), stated PLAINLY as sheet-derived, not a claim of absolute completeness. **A FAIL here is the model claiming completeness it can't back, not the model saying "sheet-derived, here's the boundary."** | | |
| U2 | ⚠️ | 业务表登记了 45 个，依赖图/消息图上还有没有别的 mc-hk-hase 牵连 MDC？ | Should attempt a graph cross-check (`impact`/`consumers`/`producers` on the 21 amet-mdc repos) and report a diff, OR explicitly say it can't do this from chat tools alone and point to Part C's script method. Must NOT just restate "45". | | |
| U3 | ⚠️ | **(follow-up turn right after #6)** 我不信只有这些，再仔细找找 | Must NOT parrot "就是45" a second time. Should either broaden the search (graph/message cross-check) or clearly state the boundary of what "45" means and why it can't unilaterally go further without the graph check in Part C. | | |
| U4 | 🔍 | 这 45 怎么来的？能保证全吗？ | Must say: 45 = `amet-mdc-*` (name) ∪ `mdc_common` (business sheet), NOT a runtime-verified complete set — completeness for the messaging-only-linked repos depends on the message map, which has known gaps. **FAIL if it says "yes, guaranteed complete."** | | |
| U5 | ⚠️ | MDC / 讲讲 MDC （裸词，无限定词） | Real ambiguity here (repo family / source_system / mdc_common grouping) — expected to ask a clarifying question or briefly cover the 2-3 readings, NOT silently pick one the way #1/#6/#10 correctly do (those have disambiguating words: "API", "含mc-hk-hase", "use case"). | | |
| U6 | ⚠️ | 列出 campaign 分组的仓库 （bait to misuse the undefined `group` value） | Correct behavior: use `query="campaign"`, NOT `group="campaign"`. See Part D for the deterministic (non-chat) version of this check — chat behavior alone isn't proof either way since the model might just happen to pick `query`. | | |
| U7 | ⚠️ | 在依赖图/消息图上，有没有和 amet-mdc-* 有连接、但不在 MDC 业务表 45 个里的 mc-hk-hase 仓库？ | See **Part C** — this is the one question in the set that needs a deterministic script answer, not a chat-level spot check. Ask it in chat too (to see if the model attempts a real graph query), but the AUTHORITATIVE answer comes from Part C. | | |

---

# Part C — U7 decisive check: dependency + message graph closure vs the 45 (script, not chat)

This is the actual proof (or disproof) of whether the sheet-derived 45 is missing any real MDC-connected
`mc-hk-hase-*` repo. Run this directly against the real `recon_out/internal_edges.csv`,
`index/message_edges.csv`, and `index/repo_tags.json` on the box.

```python
# -*- coding: utf-8 -*-
import json
from retriever import graph, messages, repo_tags

mdc = repo_tags.mdc_repos()
mdc_repo_names = {item["repo"] for item in mdc["repos"]}
amet_mdc = [r for r in mdc_repo_names if r.lower().startswith("amet-mdc")]
mdc_hase = {r for r in mdc_repo_names if r.lower().startswith("mc-hk-hase")}
print("amet-mdc count:", len(amet_mdc), "| mdc_common mc-hk-hase count:", len(mdc_hase))

# --- dependency-graph closure: everything transitively connected to the 21 amet-mdc repos ---
fwd, rev = graph.load_dependency_graph()
dep_connected_hase = set()
for repo in amet_mdc:
    result = graph.impact(repo, transitive=True, graph_data=(fwd, rev))
    for other in result["depended_on_by"] + result["depends_on"]:
        if other.lower().startswith("mc-hk-hase"):
            dep_connected_hase.add(other)

# --- message-graph closure: mc-hk-hase repos sharing a topic/destination with an amet-mdc repo ---
# NOTE: routes_for_repo / who_consumes / who_produces all return a LIST of edge dicts
# (keys: producer_repo, destination, consumer_repo, routing_source, evidence) — NOT a dict.
# See retriever/messages.py:24-41. Iterate the lists directly; do NOT call .get() on them.
msg_connected_hase = set()
for repo in amet_mdc:
    for edge in messages.routes_for_repo(repo):          # list of edge dicts
        dest = edge.get("destination")
        if not dest:
            continue
        for fn in (messages.who_consumes, messages.who_produces):
            for peer_edge in fn(dest):                    # list of edge dicts
                for name in (peer_edge.get("consumer_repo"), peer_edge.get("producer_repo")):
                    if name and name.lower().startswith("mc-hk-hase"):
                        msg_connected_hase.add(name)

graph_connected = dep_connected_hase | msg_connected_hase
missing_from_sheet = sorted(graph_connected - mdc_hase)
sheet_only_no_graph_link = sorted(mdc_hase - graph_connected)

print("\n=== dependency-graph-connected mc-hk-hase:", len(dep_connected_hase))
print("=== message-graph-connected mc-hk-hase:", len(msg_connected_hase))
print("=== union graph-connected:", len(graph_connected))
print("\n*** mc-hk-hase repos GRAPH-CONNECTED to MDC but NOT in the 24-sheet set (candidates the sheet may be missing) ***")
print(len(missing_from_sheet), missing_from_sheet)
print("\n*** mc-hk-hase repos in the 24-sheet set with NO dep/message graph link found (sheet-only, expected — e.g. shared libs with no runtime edge) ***")
print(len(sheet_only_no_graph_link), sheet_only_no_graph_link)
```

> The script above is corrected to the real return shapes (lists of edge dicts). If `retriever/messages.py`
> has drifted on your checkout, re-check the field names before running — but do NOT reintroduce `.get()`
> calls on the list returns.

## Interpreting the result

| Outcome | Meaning | Action |
|---|---|---|
| `missing_from_sheet` is **empty** | The 45 (sheet-derived) fully contains every mc-hk-hase repo that's provably connected to MDC via code deps or messaging. This is the strongest evidence available that 45 is not under-counting on the two channels we CAN check. | Report as-is; note the message map's own known gaps (below) as the residual, unprovable risk. |
| `missing_from_sheet` is **non-empty** | Found real MDC-connected mc-hk-hase repos the business sheet does NOT flag as `mdc_common`. This is a genuine completeness gap — the sheet is stale or its "MDC Common" definition is narrower than actual MDC connectivity. | List the repos, escalate to the MDC business owner (not a code fix) — either get the sheet updated, or add these as a clearly-labeled SECOND tier (`via: graph-adjacent`) in `list_repos(group="mdc")`, never silently merged into the primary 45. |

## The honest ceiling (state this regardless of the result above)

Even a clean `missing_from_sheet` result does **not** prove absolute completeness — it only proves
"nothing found via the two graph signals we have access to." Two known blind spots remain, from
[[all-repo-tags-plan]]:
- The Maven dependency graph only proves **library** blast-radius (`serves_channel_set` ceiling ~163/387
  historically) — it cannot see config-driven or purely-Kafka-mediated relationships that don't show up
  as a produce/consume edge in `message_edges.csv`.
- `message_edges.csv` itself has known gaps (the ~214 messaging-only repos problem tracked separately;
  message map coverage is incomplete).

So the correct thing for the assistant to say, even after a clean Part C result, is: **"45 is the
business-sheet-confirmed count; a dependency+messaging graph cross-check found no additional
MDC-connected mc-hk-hase repos among what those two graphs can see — but that is not an absolute
guarantee, since config-only or unmapped-message links wouldn't show up in either graph."** Never
upgrade this to "guaranteed complete."

---

# Part D — two deterministic code-behavior checks (not chat, direct dispatch calls)

Chat-level testing can't cleanly prove tool-selection behavior, because the model might accidentally do
the right thing for the wrong reason. Run these directly to get an unambiguous answer.

## D1 — does an unknown `group` value silently fall through to "no filter" (all 392)?
```python
from webapp import tools
result = tools.dispatch("list_repos", {"group": "campaign"})
print(result)
```
| Check | Expected | Actual |
|---|---|---|
| Does it silently return count=392 (all repos, no filter applied)? | **This is the current known behavior — `group` is only checked for `== "mdc"`; any other value falls through to `filter_repos` with no `query`/`system`/etc., which returns everything.** Confirm this reproduces. | |
| Is this acceptable? | Flag as a follow-up: an unrecognized `group` value should ideally error or ignore-with-warning rather than silently return the full 392, since a model that guesses `group="campaign"` (bait case #U6) would get a misleadingly "complete-looking" but meaningless 392-repo dump instead of a clear failure. Not blocking this runbook, but worth a small follow-up fix. | |

## D2 — grouped MDC results and `mode`/`channel`: confirm the actual field shape
```python
from retriever import repo_tags
result = repo_tags.mdc_repos()
print(result["repos"][0])   # inspect one entry's keys
```
| Check | Expected | Actual |
|---|---|---|
| Does each entry carry only `repo` + `via`? | Yes, as built in `ac6323e` — no `mode`/`channel` on grouped entries | |
| Confirms #16/#17 root cause | If true, the model literally cannot filter grouped MDC results by mode/channel without a second lookup (e.g. `filter_repos` per repo, or intersecting with a plain `list_repos(mode="job")` call) — explains why #16/#17 are marked probes, not regressions | |

---

# Send back
```
Part A  (Q1–20)
 regressions (1-15,19) — any FAIL?  [ list # + what happened ]
 probes (16,17,18,20)  — pass/fail [ list # + what happened ]

Part B  (U1–U7 chat-level)
 U1  [ honest sheet-derived framing, or false completeness claim? ]
 U2  [ attempted graph cross-check, or just restated 45? ]
 U3  [ follow-up: broadened search, or parroted 45 again? ]
 U4  [ honest "not guaranteed" framing, or overclaimed "guaranteed complete"? ]
 U5  [ asked a clarifying question / covered readings, or silently picked one? ]
 U6  [ used query="campaign", or group="campaign"? ]
 U7  [ chat attempt — did it try a real graph query or hand-wave? ]

Part C  (the decisive script)
 amet-mdc count / mdc_common-mc-hk-hase count = ___ / ___  (expect 21 / 24)
 dep-graph-connected mc-hk-hase = ___
 message-graph-connected mc-hk-hase = ___
 union graph-connected = ___
 missing_from_sheet (graph-connected but NOT in the 24) = ___  [ LIST THE REPO IDS if > 0 ]
 sheet_only_no_graph_link (in the 24 but no graph link found) = ___  [ expected, list if curious ]

Part D
 D1  [ unknown group="campaign" → silently returns 392? confirm reproduces ]
 D2  [ grouped entries only have repo+via, no mode/channel? confirm ]
```

## Notes
- Part A/B are chat-level spot checks — useful for behavior, not proof of completeness.
- **Part C is the load-bearing result of this whole runbook.** If `missing_from_sheet` comes back
  non-empty, that overrides the "MDC workstream CLOSED" status from RUNBOOK-46 — the footprint isn't
  closed, it's "45 confirmed, N more found via graph, escalate to MDC owner."
- None of this runbook requires a code change to execute — D1's finding may motivate a small follow-up
  fix (reject/ignore unknown `group` values instead of silently no-op-filtering), but that's a decision
  for after this runbook's results are in, not something to pre-emptively fix here.

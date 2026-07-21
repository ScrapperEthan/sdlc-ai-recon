# RUNBOOK 46 (INTERNAL Codex / you) — verify the MDC repo-grouping (`list_repos group="mdc"`) on real data

> **After Sonnet 5 builds the `mdc_common` filter + `list_repos(group="mdc")` grouping and pushes,
> pull `master`, then run this on the box with the REAL MDC business sheet + mirror.** The code change
> makes "MDC 完整 repo list（含 mc-hk-hase-*）" a single deterministic tool call that returns
> `amet-mdc-*` ∪ business-sheet `mdc_common` with a hard `count` and per-repo provenance — instead of
> the model hand-picking ~12 repos and miscounting (22 vs 21).
>
> **Why this, not "expand repo_tags to 456":** `mc-hk-hase-*` repos have `system="hase"`, not
> `amet-mdc`, and their names contain no "mdc" — so name/`system`/`query=mdc` filters can NEVER reach
> them. The ONLY signal that says "this mc-hk-hase repo belongs to MDC" is the `mdc_common` flag the
> MDC business sheet supplies. This runbook confirms that flag is actually populated (esp. on
> mc-hk-hase) and that the grouping enumerates correctly. The 392-vs-456 universe gap is a **separate**
> question handled in Part C — do NOT blindly regrow the universe to 456; the ~66 excluded dirs are
> deliberate non-system extras.
>
> **Data security:** the MDC sheet (`MDC_Repo_List_Analysis.xlsx`) and all generated `index/*.json`
> stay gitignored — never commit or push them. Send back counts / yes-no only, no raw repo dumps unless
> asked.

---

# Part A — regenerate the tags and confirm `mdc_common` is populated

## Step A1 — rebuild `repo_tags.mdc.json` from the sheet, then `repo_tags.json`
```
python enrich_repo_tags.py            # MDC_Repo_List_Analysis.xlsx -> index/repo_tags.mdc.json
python make_repo_tags.py              # merges mdc_common into index/repo_tags.json (merge_mdc)
```
Watch the coverage line `make_repo_tags.py` prints. Record `mdc_common_set` (how many repos carry
`mdc_common=true`).

## Step A2 — confirm the flag actually lands on mc-hk-hase repos (the whole point)
```
python - <<'PY'
import json
t = json.load(open("index/repo_tags.json", encoding="utf-8-sig"))
mdc = [r for r,m in t.items() if m.get("mdc_common")]
amet = [r for r,m in t.items() if (m.get("system") or "").lower()=="amet-mdc"]
hase_mdc = [r for r in mdc if r.lower().startswith("mc-hk-hase")]
print("mdc_common total:", len(mdc))
print("  of which mc-hk-hase-*:", len(hase_mdc))
print("amet-mdc-* (by system):", len(amet))
print("union (amet-mdc ∪ mdc_common) unique:", len(set(mdc)|set(amet)))
PY
```

| Check | Expected | Actual |
|---|---|---|
| `amet-mdc-*` by system | ~21 | |
| `mdc_common=true` total | > 21 (i.e. the sheet flags non-amet repos too) | |
| of which `mc-hk-hase-*` | **> 0** ← if this is 0, the sheet only covers amet-mdc and the grouping can't include mc-hk-hase; flag it | |
| union unique count | = what `list_repos(group="mdc")` will report in Part B | |

> If `mc-hk-hase-*` count is 0: the MDC sheet does not flag any mc-hk-hase repo as MDC-Common. Then the
> business answer to "包括 mc-hk-hase" isn't in the sheet at all — escalate to the MDC owner for the
> real membership list, OR fall back to the dependency/message-graph tier (Part C note). Do not silently
> return only amet-mdc-*.

---

# Part B — verify the grouping tool returns the right thing

## Step B1 — restart the app on the fresh tags, then ask via CLI / Q&A
```
python cli.py  ask  "列出你看到的 MDC repo list，要包括 mc-hk-hase*"
```
Confirm the tool trace shows a single `list_repos(group="mdc")` call (NOT `show_coverage` +
`search_code(pattern="MDC")`, and NOT `query=mdc`).

| Check | Expected | Actual |
|---|---|---|
| tool called | `list_repos(group="mdc")` once | |
| answer's stated count | == union count from A2 (verbatim from tool `count`) | |
| amet-mdc members present | all ~21, each labeled `via: amet-mdc-prefix` | |
| mc-hk-hase members present | the `mdc_common` ones, labeled `via: mdc_common` | |
| model did NOT invent a count | no "22" when the tool says 21; no hand-picked subset | |
| provenance stated | answer says the mc-hk-hase ones come from the MDC business sheet, not the name | |

## Step B2 — the discipline check
Ask a second grouping question (e.g. `"MDC 系统一共有多少个仓库"`). The number MUST equal the tool's
`count` field. If the model states any repo count that the tool did not return, the prompt discipline
did not land — flag it.

---

# Part C — the universe / coverage audit (separate from the grouping)

This is the real version of Codex's "392 → 456", done as a targeted audit instead of a blind regrow.

## Step C1 — is any sheet-flagged MDC repo missing from the 392 canonical universe?
```
python - <<'PY'
import json
t = json.load(open("index/repo_tags.json", encoding="utf-8-sig"))
mdc_sheet = json.load(open("index/repo_tags.mdc.json", encoding="utf-8-sig"))
flagged = {r.lower() for r,m in mdc_sheet.items() if m.get("mdc_common")}
in_universe = {r.lower() for r in t}
missing = sorted(flagged - in_universe)
print("sheet-flagged MDC repos NOT in the 392 universe:", len(missing))
for r in missing: print("  ", r)
PY
```
| Check | Expected | Actual |
|---|---|---|
| flagged-but-excluded count | **0** (every MDC-flagged repo is in the universe) | |
| if > 0 | list them — these were dropped as "non-system extras" but the business says they ARE MDC; that's the coverage bug to fix (add them to the universe seed / bundle plan), NOT a blanket 456 | |

## Step C2 — (defer) CodeGraph 66-missing
Codex's third point (CodeGraph indexed 390 of the mirror's 456, missing 66) is a **call-graph coverage**
question, unrelated to enumerating the MDC repo list. Do NOT block this runbook on it. Note which of the
66 are `mdc_common`/`amet-mdc` (those would matter for `unified_impact` on MDC symbols) and raise
separately.

---

# Send back
```
Part A
 A1  [ mdc_common_set = ___ ]
 A2  [ amet-mdc ~21? mdc_common total ___ (>21?)  of which mc-hk-hase = ___ (>0?)  union unique = ___ ]
Part B
 B1  [ single list_repos(group="mdc") call? stated count == union? amet+hase both present & labeled?
        no invented count / no hand-picked subset? provenance stated? ]
 B2  [ second count == tool count? ]
Part C
 C1  [ sheet-flagged-but-excluded = ___ (0?)  if >0 list them ]
 C2  [ of the 66 CodeGraph-missing, how many are mdc_common/amet-mdc = ___ (info only) ]
```

## Notes
- The grouping is **curated-list-first by design**: `amet-mdc-*` (name-derived, structural) ∪
  `mdc_common` (business-sheet confirmed). It is deliberately NOT a dependency-graph closure — that
  would reintroduce the fuzzy, over-broad blast radius. If the business later wants the runtime-reality
  view, add it as a clearly-labeled **second tier** (`via: graph-adjacent`), never merged into the
  primary count.
- If A2 shows the sheet doesn't flag mc-hk-hase at all, the fix is a **data/business** fix (get the real
  membership from the MDC owner), not a code fix — the tool is doing exactly what the data says.

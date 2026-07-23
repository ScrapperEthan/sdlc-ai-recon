# RUNBOOK 49 (INTERNAL Codex / you) — fix "3HK SMSC counts non-3HK repos" + "some repos not shown"

> **Raised by S Q HUANG (2026-07-23) against the architecture-diagram view:**
> 1. **Some repos don't appear at all** — e.g. 3HK outbound API `mc-hk-hase-htcl-outbound-api`, and the
>    Aurora push repo `mc-hk-hase-mkt-rt-gen-aurora-push-deli-job`.
> 2. **The "3HK SMSC" node over-counts** — it shows **仓库 69 个** and folds in non-3HK repos (CSL, CM,
>    Sinch: `amet-mdc-hsbc-cm-outbound-api`, `amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job`,
>    `amet-mdc-hsbc-svc-tc-csl-sms-deli-job`, …).
>
> **Sonnet 5 already fixed the code for #2 and pushed to `master`. This runbook (a) verifies that fix on
> the REAL index — I can't, the box holds the only real `index/*.json` — and (b) does the part of #1 that
> needs internal-GitHub / mirror access, which is not a code change.**
>
> **Data security:** the mirror and all generated `index/*.json` stay gitignored — never commit/push
> them. Send back counts / yes-no, not raw repo dumps unless asked.

---

## What Sonnet 5 changed (already on `master`)

Root cause of #2 was a **one-field data defect** in the committed node catalog, not a logic bug.
`make_arch_map.bind_node()` for an `external` `kind:"vendor"` node filters repos by vendor **only if the
node carries a `vendor`**; a vendor-less node deliberately binds *every* vendor on its channel (this is
tested — `tests/test_arch_map.py::test_external_vendor_node_binds_integrating_repos`). The `ext-3hk-smsc`
node had **no `vendor`**, so on the SMS channel (csl + sinch + cm + 3hk) it swallowed all of them → 69.

1. `static/arch_nodes.json` — added `"vendor": "3hk"` to `ext-3hk-smsc` (now matches its siblings
   `ext-csl-smsc`/`ext-sinch`, which already had a vendor).
2. `make_delivery_topology.py` — added a **vendor-alias** so 3HK's real repo token folds onto the
   canonical one:
   ```python
   VENDOR_ALIASES = {"htcl": "3hk"}      # 3HK repos carry its legal name "htcl" (Hutchison)
   def canon_vendor(v): return VENDOR_ALIASES.get(v, v)
   ```
   Applied where the vendor token is derived (delivery-job regex, outbound regex, and the outbound
   candidate-token match). **Without this the fix in (1) would bind an *empty* set** — no repo is named
   `*-3hk-*`; the real 3HK repos are `mc-hk-hase-htcl-*` / `*-htcl-*`. Folding `htcl → 3hk` puts them in
   the `3hk` bucket the node now filters on, and also makes the previously-likely-empty `sms-3hk`
   outbound node bind `mc-hk-hase-htcl-outbound-api` (helps #1 too).
3. `tests/test_vendor_alias_3hk.py` — new. 288/288 tests pass locally.

**Owner decision to confirm (Part A3):** this treats **3HK ≡ htcl** as one vendor. That matches the
diagram label ("3HK SMSC") and the business name. If the owners intend `htcl` and `3hk` to be shown
*separately*, back out alias (2) and instead relabel the node — but every signal says they are the same
carrier.

---

# Part A — verify the #2 fix on the real index

## Step A1 — pull and rebuild the two artifacts (topology → arch map)
```
git pull origin master
python make_delivery_topology.py --edges recon_out/internal_edges.csv \
       --repo-tags index/repo_tags.json --out index/delivery_topology.json
python make_arch_map.py --catalog static/arch_nodes.json --topology index/delivery_topology.json \
       --repo-tags index/repo_tags.json --out index/arch_map.json
```
(Equivalently: `python refresh.py` — it runs both in this order; steps confirmed in `refresh.py`.)

## Step A2 — confirm 3HK SMSC is now vendor-scoped (no CSL/CM/Sinch, keeps the real 3HK repos)
```
python - <<'PY'
import json
a = json.load(open("index/arch_map.json", encoding="utf-8-sig"))["nodes"]
n = a["ext-3hk-smsc"]
print("ext-3hk-smsc repo_count:", n["repo_count"], " (was 69)")
bad = [r for r in n["repos"] if any(v in r.lower() for v in ("csl","sinch","-cm-","cm-outbound"))]
print("non-3HK repos still bound (should be []):", bad)
print("htcl/3hk repos bound:", [r for r in n["repos"] if "htcl" in r.lower() or "3hk" in r.lower()])
print("sms-3hk outbound repos:", a["sms-3hk"]["repos"])
for other in ("ext-csl-smsc","ext-sinch"):
    print(other, "->", a[other]["repo_count"], "repos")
PY
```
| Check | Expected | Actual |
|---|---|---|
| `ext-3hk-smsc` repo_count | **≪ 69** (the genuine 3HK set only) | |
| non-3HK repos still under 3HK SMSC | **`[]`** (no csl/cm/sinch) | |
| 3HK's own `htcl` repos present | **> 0** (e.g. `mc-hk-hase-htcl-outbound-api` if in universe — see Part B) | |
| `sms-3hk` outbound node | now binds `mc-hk-hase-htcl-outbound-api` (was likely empty) | |
| `ext-csl-smsc` / `ext-sinch` | unchanged from before | |

> If `ext-3hk-smsc` comes back **empty (0)**: the mirror has no 3HK repo under either `3hk` *or* `htcl`
> token, or a *different* alias is in play (check the actual 3HK repo names in the mirror and extend
> `VENDOR_ALIASES`). Report the real 3HK repo names you find.

## Step A3 — sanity-check the alias didn't over-merge
```
python - <<'PY'
import json
t = json.load(open("index/delivery_topology.json", encoding="utf-8-sig"))
sms = t.get("sms", {})
print("sms vendor buckets:", sorted(sms))          # expect 3hk, csl, sinch, cm, ... ; NO 'htcl'
print("htcl bucket gone?:", "htcl" not in sms)
for v in sorted(sms):
    print(" ", v, "deli=", len(sms[v].get("delivery_jobs") or []),
          "api=", len(sms[v].get("outbound_apis") or []))
PY
```
Confirm `htcl` is **absent** (folded into `3hk`) and every other vendor bucket is intact. Confirm with
the owner that 3HK ≡ htcl is the intended grouping.

---

# Part B — the missing repos (#1): universe / mirror completeness (NOT a code fix)

Both named repos are absent because they are **not in the frozen 392-repo universe** the pipeline reads
(`repo_universe` = `internal_edges.csv` ∪ frozen `index/bundles.json` ∪ `recon_out/repos.txt`). If a repo
was created after the freeze, or the org scan missed it, it has no tag entry and appears nowhere. The two
repos are **examples** — the real task is to re-scan the org and reconcile.

## Step B1 — are they in the universe / mirror at all?
```
python - <<'PY'
import json, os
t = json.load(open("index/repo_tags.json", encoding="utf-8-sig"))
u = {r.lower() for r in t}
for r in ["mc-hk-hase-htcl-outbound-api","mc-hk-hase-mkt-rt-gen-aurora-push-deli-job"]:
    print(f"{r:48} in_tags={r.lower() in u}  cloned={os.path.isdir(os.path.join('mirror', r))}")
PY
```

## Step B2 — if missing from the mirror, clone + add to the universe seed, then rebuild
```
# clone just these (uses the same org/template as clone_mirror.py; DEFAULT_ORG=hase-mc)
printf '%s\n%s\n' mc-hk-hase-htcl-outbound-api mc-hk-hase-mkt-rt-gen-aurora-push-deli-job > /tmp/add.txt
python clone_mirror.py --repos-file /tmp/add.txt          # into ./mirror
# seed them into the universe so they get a tag entry even with no Maven edge:
#   append to recon_out/repos.txt, OR pass --pom-only-file, then re-tag + rebuild
python refresh.py                                         # recon -> tags -> topology -> arch_map
```
Then re-run **Part A2** and confirm `mc-hk-hase-htcl-outbound-api` now shows under `sms-3hk` / 3HK SMSC.

## Step B3 — Aurora is a brand-new push vendor
`mc-hk-hase-mkt-rt-gen-aurora-push-deli-job` parses cleanly as **channel=push, vendor=aurora** via
`DELIVERY_RE`. The `push-deli` node (`role:"delivery-job"`, `channel:"push"`) binds **all** push vendors,
so once the repo is in the universe it appears there automatically — **no code change needed.** Verify:
```
python - <<'PY'
import json
a = json.load(open("index/arch_map.json", encoding="utf-8-sig"))["nodes"]
print("aurora in push-deli:", any("aurora" in r.lower() for r in a["push-deli"]["repos"]))
PY
```
Decision for the owner: Aurora currently has no dedicated outbound/vendor node in the diagram (SNS →
APNs/FCM is the only push chain drawn). If Aurora is its own push provider that must appear as a distinct
lane, add nodes to `static/arch_nodes.json` (an `outbound-api` + `external`/`vendor` pair, `channel:"push",
"vendor":"aurora"`) — otherwise it correctly rolls up under the shared Push delivery lane.

## Step B4 — the general fix: re-scan the org for anything created since the 392 freeze
These two are unlikely to be the only new repos. Enumerate the `hase-mc` org and diff against the
universe:
```
# list org repos via the internal GitHub API (needs box network + token), then:
python - <<'PY'
import json
have = {r.lower() for r in json.load(open("index/repo_tags.json", encoding="utf-8-sig"))}
org = {l.strip().lower() for l in open("/tmp/org_repos.txt") if l.strip()}   # from the API dump
missing = sorted(org - have)
print("org repos NOT in the 392 universe:", len(missing))
for r in missing: print("  ", r)
PY
```
Review `missing` with the owner (some dirs are deliberately excluded non-system extras — do **not** blanket
re-grow the universe; add the real ones to the bundle/seed as in B2). This is the same discipline as
RUNBOOK-46 Part C — targeted audit, not a blind 392→456.

---

# Part C — secondary issues found while diagnosing (note, don't block)

- **2-way SMS jobs mis-bucket.** `svc-rt-hr-htcl-2way-sms-deli-job` parses vendor=`2way` (the `2way`
  qualifier sits between `htcl` and `sms`, and `DELIVERY_RE`'s vendor group is `[a-z0-9]+`, no hyphen).
  The `htcl→3hk` alias does **not** catch it. If the 2-way jobs should sit under 3HK, either add `2way` as
  an alias target or widen the parser — but confirm intent first (they may belong in their own bucket).
- **`ext-3hk-mmsc` left as-is.** MMS currently has a single vendor, so the vendor-less node binds all MMS
  (= only 3HK) correctly and shows no over-count. Left unchanged to avoid risk of emptying it. If MMS ever
  gains a second vendor, add `"vendor":"3hk"` there too (the new invariant test in
  `test_vendor_alias_3hk.py` guards multi-vendor channels for exactly this).

---

# Send back
```
Part A
 A2  [ ext-3hk-smsc repo_count = ___ (was 69)  non-3HK bound = [] ?  htcl repos present? ]
 A3  [ sms buckets = ___  htcl folded (absent)?  owner confirms 3HK≡htcl? ]
Part B
 B1  [ htcl-outbound-api in_tags/cloned = __/__   aurora in_tags/cloned = __/__ ]
 B2  [ after clone+rebuild: htcl-outbound-api now under sms-3hk / 3HK SMSC?  yes/no ]
 B3  [ aurora appears under push-deli?  own-lane decision: ___ ]
 B4  [ org repos not in universe = ___  (list for owner review) ]
Part C
 C   [ 2way bucket intent? ___   mms consistency note ack? ]
```

## Notes
- #2 (over-count) is a **code/data fix** — done + pushed; Part A just verifies it on real data.
- #1 (missing repos) is a **data/mirror completeness fix** — needs internal-GitHub access to clone +
  reconcile the universe; there is no logic bug making them disappear, they are simply not indexed yet.
- Keep the mirror and `index/*.json` box-local (gitignored). Return counts, not dumps.

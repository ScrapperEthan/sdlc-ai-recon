# RUNBOOK 52 (INTERNAL Codex / you) — ICCM-SMS route→real-carrier gap (investigation, not a fix yet)

> **Context:** RUNBOOK-51 folded the SMS repo tokens `iccms`/`iccmh`/`iccmt`/`iccmv`/`iccmpt` into one
> `iccm` vendor bucket. You (internal Codex) then confirmed via source scan that this platform-level
> fold is CORRECT — they are all ICCM, distinguished by business line (SHP general / High-Risk-Speed /
> Time-Critical / VM API), and `iccmpt` is a dead 404 repo (no action needed on it).
>
> **But your scan surfaced something the current model does NOT account for:** some `iccms`/`iccmv` SMS
> jobs, when the message carries no explicit route, fall back in code to `HUTCHISON_GW_SMS` — i.e. the
> real downstream carrier for at least some ICCM-SMS traffic is **3HK's own gateway**, not a distinct
> ICCM carrier. Evidence you already found: `.../iccms-sms-deli-job/.../SmsMessageDelivery.java:123`.
>
> **Why this matters:** `make_delivery_topology.py`/`arch_nodes.json` currently treat `iccm` and `3hk` as
> two disjoint SMS vendors. If the real downstream for (some) ICCM-SMS traffic IS 3HK, then a 3HK outage
> should show these ICCM-tokened repos as affected too — today it won't. This is an **outage-impact
> accuracy gap**, not a display bug: nobody asked about it yet, but the day someone runs "what breaks if
> 3HK goes down" during a real incident, the answer would silently be incomplete.
>
> **Why this is NOT a quick code fix:** repo-NAME-based parsing (what `make_delivery_topology.py` does)
> cannot resolve this — the real downstream depends on the message's `route` value, which is runtime/
> config data, not something baked into the repo name. You said it yourselves: *"判断真正下游是否为短信
>网关：必须查看消息里的 route，不能只看 repo 名称。"* So this runbook is a **directed investigation**:
> map every ICCM-SMS route to its real downstream, so we know the true shape of the problem before
> anyone writes code against it.

---

# Part A — enumerate every route the ICCM-SMS jobs can resolve to

For **all** `iccms`/`iccmh`/`iccmt`/`iccmv` repos (not just the one file you already found), find:
1. Every place a `route` (or equivalent routing key) is read to pick the outbound path.
2. Every concrete value that route can take, and what each one maps to downstream (a config table,
   an enum, an `if/else` chain — whatever the code actually does).
3. Whether the `HUTCHISON_GW_SMS` fallback you found is the **only** non-ICCM downstream, or whether
   other route values ALSO resolve to a real external carrier (3HK again, or something else entirely —
   Sinch/CSL/CM) rather than staying inside ICCM's own infrastructure.

```
# starting points (repo-relative), adapt per repo:
#   application.yml / application-*.yml   — route -> destination config tables
#   *RouteResolver*.java, *RouteConfig*.java, *SmsMessageDelivery.java — resolution logic
#   grep for: HUTCHISON_GW_SMS, route, ROUTE_, GW_SMS, downstream
```
Build a table like:
| repo (iccm variant) | route value | resolves to | evidence (file:line) |
|---|---|---|---|
| iccms (SHP general) | (no route / default) | HUTCHISON_GW_SMS (3HK) | SmsMessageDelivery.java:123 |
| iccms (SHP general) | `<other value(s) you find>` | ? | ? |
| iccmv (VM API) | ? | ? | ? |
| iccmh (SHP HR) | ? | ? | ? |
| iccmt (SHP TC) | ? | ? | ? |

## Part A checks
| Question | Why it matters |
|---|---|
| Is the fallback (`HUTCHISON_GW_SMS`) the ONLY case, or do explicit route values also point outside ICCM? | Determines whether the gap is narrow (default-only) or broad (most/all ICCM-SMS traffic ultimately is 3HK or another named carrier) |
| Is the route resolution **config-driven** (readable statically from yml) or only decidable **at runtime** (e.g. depends on a DB lookup, a feature flag, per-use-case data)? | If config-driven, we can build a real repo→true-carrier map. If runtime-only, no static tool can ever get this right — that itself is the answer, and we should say so honestly rather than guess |
| Does `iccmh`/`iccmt`/`iccmv` share the SAME route-resolution code as `iccms`, or does each variant have its own logic? | Tells us whether one finding generalizes to all 4 variants or each needs checking separately |

---

# Part B — quantify: how many repos/jobs are actually affected
```
python - <<'PY'
import json
t = json.load(open("index/repo_tags.json", encoding="utf-8-sig"))
iccm = [r for r in t if any(v in r.lower() for v in ("iccms","iccmh","iccmt","iccmv"))]
print("ICCM-SMS repos in universe:", len(iccm))
for r in iccm: print(" ", r)
PY
```
For each, note which route-resolution case from Part A's table it falls under (default-fallback vs
explicit route vs undeterminable-at-static-analysis).

---

# Part C — report back; do NOT pre-emptively code a fix
Send back the Part A table + Part B count. Once the real shape is known, the follow-up design (built by
Sonnet 5 once you report) will likely be one of:
- **(a)** If the gap is narrow (only the no-route-specified default case, small repo count): add a
  `downstream_vendor` cross-reference on those specific jobs so `outage_report.py` can fold them into a
  3HK-outage's affected set WITHOUT changing their primary `iccm` bucket (keeps the SHP/HR/TC business
  view intact, fixes the impact-analysis blind spot).
- **(b)** If most ICCM-SMS routes resolve outside ICCM entirely: `iccm` as a "vendor" may not be the
  right model for the SMS channel at all — it may need to be modeled as a routing tier whose real vendor
  is looked up per repo, not a peer bucket to csl/3hk/sinch. Bigger change, needs a design pass.
- **(c)** If resolution is genuinely runtime-only (can't be determined from source/config): document this
  as a permanent, honest limitation of name/config-based topology — no code chases it, the coverage note
  in `arch_map.json`/`delivery_topology.json` says so explicitly instead of silently omitting it.

# Send back
```
Part A  [ route table filled in — how many distinct downstream targets found? ___
          config-driven or runtime-only? ___   same logic across iccms/iccmh/iccmt/iccmv? ___ ]
Part B  [ ICCM-SMS repo count = ___  (list) ]
```

## Notes
- This is explicitly an **investigation** runbook — no code change is prescribed yet because we don't
  know the real shape of the answer. Resist the urge to patch `make_delivery_topology.py` blind; a wrong
  guess here is worse than the current honest gap (over-merging iccm and 3hk when they're NOT the same
  would corrupt the 3HK outage-impact numbers we just spent RUNBOOK-49 fixing).
- `iccmpt` needs no further action (confirmed dead/404 in RUNBOOK-51's Codex report).
- The platform-level fold (`iccm* → iccm` in `canon_vendor`) stays as-is regardless of this runbook's
  outcome — it's already validated correct for grouping purposes.

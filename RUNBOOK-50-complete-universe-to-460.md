# RUNBOOK 50 (INTERNAL Codex / you) — complete the repo universe to the full 460 active org repos

> **Owner decision (2026-07-23, via Ethan):** stop the "390 frozen vs 460 active" gap — bring the
> canonical universe up to **all 460 active `hase-mc` repos**, so nothing is silently missing from the
> diagram / impact views. This runbook does that on the box (needs internal-GitHub + the real mirror),
> using the diff Sonnet 5 already produced in `.tmp/runbook49-diffs-after-refresh.json`.
>
> **Sonnet 5 already shipped two related CODE changes to `master` (pull first):**
> - `make_delivery_topology.py`: `htcl-2way-sms` now parses vendor=`htcl`→`3hk` (2-way SMS = 3HK,
>   owner-confirmed) instead of a phantom `2way` bucket; `message_type` recorded on the job.
> - `static/arch_nodes.json`: explicit **Aurora push lane** — `push-aurora` (outbound) + `ext-aurora`
>   (vendor terminal), and `ext-apns-fcm` scoped to `vendor:"sns"` so it no longer swallows the Aurora
>   repos. 327 tests pass.
>
> **Data security:** mirror + all `index/*.json` stay gitignored — never commit/push. The Aurora repo
> `mc-hk-hase-mkt-rt-gen-aurora-push-deli-job` had a **plaintext keystore password in `application.yml`**
> (RUNBOOK-49 Part C.4) — that is a separate InfoSec escalation; do not paste the value anywhere.

---

## The numbers (from the refresh diff)
| set | count | meaning |
|---|---|---|
| active org repos | **460** | the target universe |
| mirror after clone | 457 | 17 org repos not yet cloned; 14 mirror dirs are stale (not in org) |
| `repo_tags` universe | 393 | today; = 380 valid-org + 13 stale |
| frozen bundle | 390 | CodeGraph bundle plan (deliberately not auto-rebuilt) |

`380 valid-org tags + 80 org_not_tags = 460`. So closing the gap = **add the 80 `org_not_tags`** and
**reconcile the 13 stale `tags_not_org`** (see Part A — do NOT blindly prune them).

---

# Part A — FIRST: the decision-job anomaly (do not prune blindly)

`mirror_not_org` / `scan_not_org` / `tags_not_org` all list the **same ~13 repos**, and they are the
**core `*-decision-job`s** (`mc-hk-hase-svc-rt-hr-decision-job`, `-svc-bat-decision-job`,
`-ssvc-*-decision-job`, `-mkt-*-decision-job`, … + `mc-hk-hsbc-batch-letter-postman-job`). These are
central pipeline repos (the use-case router). Them being "in our data but NOT in the 460 active list" is
suspicious — most likely the org enumeration **paginated / filtered them out**, OR they were renamed.

**Verify before treating "460" as complete:**
```
# hit the internal GHE API directly for ONE decision job — does it still exist / is it archived?
#   GET /repos/hase-mc/mc-hk-hase-svc-rt-hr-decision-job   -> 200 active? 301 renamed? 404 gone?
# and re-pull the org repo list with pagination fully drained (per_page=100, follow all pages,
#   include type=all so internal/private/archived aren't dropped).
```
| Outcome | Action |
|---|---|
| decision jobs still active (listing was incomplete) | the true active count is **> 460**; re-run the diff with the complete list, keep the decision jobs, and target that number — not 460 |
| decision jobs genuinely renamed/consolidated | map old→new, update tags, then prune the 13 stale entries + 14 stale mirror dirs |

Do **not** delete any `*-decision-job` tag/mirror dir until this is settled — losing the routers would break outage-impact inversion.

---

# Part B — clone the 17 not-yet-mirrored repos
The `org_not_mirror` list (17):
```
amet-mdc-hsbc-batch-letter-html-postman-job      mc-hk-hase-svc-bat-aurora-push-deli-job
amet-mdc-hsbc-batch-letter-postman-job           mc-hk-hase-svc-bat-htcl-2way-sms-deli-job
amet-mdc-hsbc-ssvc-rt-gen-int-email-deli-job     mc-hk-hase-svc-rt-gen-aurora-push-deli-job
mc-hk-hase-api-smpp-core                          mc-hk-hase-svc-rt-hr-aurora-push-deli-job
mc-hk-hase-inapp-feedback-job                     mc-hk-hase-t1-proxy-bridge
mc-hk-hase-inapp-fraud-api                        mc-hk-hsbc-policy-upload-job
mc-hk-hase-inapp-housekeep-job                    mc-hk-hsbc-svc-bat-pfp-email-deli-job
mc-hk-hase-mkt-bat-aurora-push-deli-job           mc-hk-hase-sms-deli-job
mc-hk-hase-ssvc-rt-hr-pfp-email-deli-job
```
```
# write the 17 names to a file, then (real mirror lives at the box path, NOT ./mirror):
python clone_mirror.py --repos-file /tmp/org_not_mirror.txt --mirror "<REAL_MIRROR_DIR>"
```
> Note the mirror path gotcha from RUNBOOK-49: the real mirror is e.g.
> `C:\Users\<id>\Downloads\HASE_MDC`, not the repo-relative `.\mirror`. Pass `--mirror` explicitly and
> point every downstream command at the same dir (or set `SDLC_MIRROR`).

---

# Part C — expand the universe to all 460, then re-tag / re-bundle

1. **Seed all 80 `org_not_tags` into the universe** (so each gets a tag entry even with no Maven edge).
   Append them to `recon_out/repos.txt` (or pass repeated `--pom-only` / `--pom-only-file`), then:
   ```
   python refresh.py            # recon -> repo_tags -> delivery_topology -> arch_map (uses SDLC_MIRROR)
   ```
2. **`detect_system` keeps the views clean.** ~42 of the 80 are infra/CI/tooling, not delivery repos
   (see Part E) — they'll be tagged by prefix (`ai-*`, `aws-tf-*`, `doris-*`, `*-pipeline*`,
   `*-scripts`, `*-frontend`, `mdc-*-utilities`, …) and won't pollute the channel/vendor lanes, which key
   off `*-deli-job` / `*-outbound-api` name patterns. Including them satisfies "补全到 460" without
   noise. If any infra prefix isn't recognized, add it to `SYSTEM_PREFIXES` so it buckets as infra, not
   a phantom channel/vendor.
3. **CodeGraph bundle (`index/bundles.json`) is intentionally NOT rebuilt by `refresh.py`.** The new
   delivery repos will show in the diagram/impact immediately, but won't have call-graph coverage until
   the bundle plan is re-frozen. Decide with the owner whether to re-run `make_bundles.py` now (adds the
   ~38 delivery repos incl. the 4 Aurora siblings to a bundle) or defer — same call-graph-coverage
   question as RUNBOOK-46 Part C. If re-frozen, the per-bundle CodeGraph indexes must be rebuilt too.

---

# Part D — verify the delivery repos land in the right places
```
python - <<'PY'
import json
t = json.load(open("index/repo_tags.json", encoding="utf-8-sig"))
print("repo_tags universe:", len(t), "(target ~460, or >460 if Part A says decision jobs stay)")
a = json.load(open("index/arch_map.json", encoding="utf-8-sig"))["nodes"]
def has(node, frag): return [r for r in a[node]["repos"] if frag in r.lower()]
print("ext-aurora repos:", a["ext-aurora"]["repo_count"], a["ext-aurora"]["repos"])
print("push-deli aurora:", len(has("push-deli","aurora")))
print("3HK SMSC htcl-2way present:", bool(has("ext-3hk-smsc","2way")))
print("3HK SMSC htcl-sms present:", bool(has("ext-3hk-smsc","htcl")))
print("ext-apns-fcm (sns-scoped) count:", a["ext-apns-fcm"]["repo_count"], "(may be 0 = honest)")
PY
```
| Check | Expected |
|---|---|
| `repo_tags` universe | ~460 (or the corrected number from Part A) |
| `ext-aurora` | binds the 4 `*-aurora-push-deli-job` repos |
| `push-deli` | still lists the aurora jobs (delivery lane) |
| 3HK SMSC | now also includes `htcl-2way` + `htcl-sms` jobs (2-way folded in) |
| `ext-apns-fcm` | scoped to sns — likely 0 repos now (all push is Aurora); honest, flag if surprising |
| coverage | `arch map: N bound / M empty` — Aurora nodes bound; note any newly-empty node |

---

# Part E — what "all 460" actually pulls in (for the owner)
The 80 `org_not_tags` split roughly:
- **~38 real delivery / notification repos** (SHOULD be in): the 4 Aurora push siblings; `htcl-2way` +
  `htcl-sms` + `iccms` sms/email + `pfp`/`pps` email deli jobs; `2way-sms` recon/tracking;
  `hutchison-postman`/`-tracking` (3HK); 3× `inapp-*`; `wechat-api`; `smpp-core`/`ali-client`;
  letter/otx postman jobs (`amet-mdc-*`, `mc-bm-hsbc-*`, `mc-hk-hase-bat-html-letter-*`);
  `omnichannel-papi`; `auto/bounce-back resend`; `timecritical-email-api`; `vendor-notification-api`.
- **~42 infra / CI / tooling / frontend** (the deliberately-excluded extras, now included per the "460"
  decision): `ai-*`, `aws-tf-*`, `doris-*`, `engineer-tools`, `g3-release`, `*-pipeline*`/`shp-*`,
  `*-scripts`, `*-frontend`, `jmeter-boot-tcoe`, `splunk-log-job`, `mdc-*` (specs/appd/sonar/support),
  `hc-auto`/`ice-auto`/`gsops`, `t1-proxy-bridge`, `north-star-api`, `life400-job`.

These are tagged as infra by `detect_system`, so they sit outside the channel/vendor lanes — the diagram
stays clean; they're just no longer invisible.

---

# Send back
```
Part A  [ decision jobs: active(listing incomplete) or renamed?  → true target count = ___ ]
Part B  [ cloned ___/17  failed ___ ]
Part C  [ repo_tags universe after = ___  ;  bundle re-frozen? y/n ]
Part D  [ ext-aurora = 4?  3HK SMSC incl htcl-2way & htcl-sms?  ext-apns-fcm count = ___
          arch map bound/empty = ___/___ ]
Part E  [ any infra prefix unrecognized by detect_system? list ___ ]
```

## Notes
- The 2-way and Aurora **code** is done + on `master`; this runbook only needs `git pull` + the box data
  work. The old `2way`/phantom bucket disappears once `refresh.py` regenerates `delivery_topology.json`.
- "补全到 460" is the owner's call; Part E just makes explicit that ~half are infra. If the owner later
  wants delivery-only, filter by `system` — don't re-shrink the universe.
- Keep the InfoSec escalation (Aurora keystore password) moving on its own track.

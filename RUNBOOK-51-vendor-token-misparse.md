# RUNBOOK 51 (INTERNAL Codex / you) — fix SMS vendor mis-parse (hr / iccm* / hase) + git handoff

> **Context:** RUNBOOK-50 verified clean (universe=460, 3HK SMSC=17, Aurora lane bound). While doing it
> you (internal Codex) found + fixed on the box — but did **not** commit — a related regression
> (`mc-hk-hase-sms-deli-job` → phantom `hase` vendor bucket stealing HTCL/CSL outbound APIs), and asked
> for it to be officialized externally. Ethan also asked to fix the `iccm*` / `hr` phantom SMS vendor
> buckets. **Sonnet 5 has now done all of that in ONE principled change and pushed it to `master`.**

## What changed (master `<this commit>`) — `make_delivery_topology.py`
The vendor was taken **positionally** (the token right before the channel), so mode/system words and
split ICCM variants became phantom vendors. Now the vendor is the **rightmost KNOWN carrier token**:
- `KNOWN_VENDORS` = the real carriers (csl, sinch, 3hk, cm, lx, aurora, awssg, awshk, pfp, pps, sfmc,
  iccm, otx, haro, sns, apns, fcm). Tokens are matched **after** `canon_vendor` (htcl→3hk).
- `canon_vendor` now also folds the whole **`iccm*` family → `iccm`** (iccms/iccmh/iccmt/iccmv/iccmpt).
- A name with no known carrier token (`mc-hk-hase-sms-deli-job`, or a mode-only stem) → **`unknown`**
  vendor, so it never mints a `hase`/`hr` bucket and never gets grabbed by the outbound candidate match.
- The `2way` qualifier fix (RUNBOOK-50) is subsumed: `htcl-2way-sms` → `3hk` with `message_type:"2way"`.

Verified locally (356 tests + direct parse of the real names):
```
mc-hk-hase-sms-deli-job                    -> sms / unknown
mc-hk-hase-svc-rt-hr-csl-sms-deli-job      -> sms / csl        (hr skipped)
mc-hk-hsbc-svc-bat-iccms-sms-deli-job      -> sms / iccm       (folded)
mc-hk-hase-svc-tc-iccmpt-sms-deli-job      -> sms / iccm       (folded)
mc-hk-hase-svc-bat-htcl-2way-sms-deli-job  -> sms / 3hk [2way]
mc-hk-hase-mkt-bat-aurora-push-deli-job    -> push / aurora    (real vendor kept)
mc-hk-hsbc-svc-bat-pps-email-deli-job      -> email / pps      (real vendor kept)
```

---

# Step 1 — git handoff (you have uncommitted box copies of these files)
Your box still has un-committed local edits to `make_delivery_topology.py` and
`tests/test_vendor_alias_3hk.py` (your RUNBOOK-50 versions). The official commit **supersedes both** —
discard the local copies, then pull:
```
git checkout -- make_delivery_topology.py tests/test_vendor_alias_3hk.py
git pull origin master
```
> `refresh.py` is **NOT** touched by this commit, so your local `--repos-file` edit on it **survives the
> pull** — keep it (see Step 3). If `git pull` still complains about `refresh.py`, that's only your local
> edit; leave it in place.

# Step 2 — rebuild + verify no phantom vendors remain
```
python refresh.py
python - <<'PY'
import json
t = json.load(open("index/delivery_topology.json", encoding="utf-8-sig"))
for ch in ("sms", "email"):
    vend = sorted(t.get(ch, {}))
    print(ch, "vendors:", vend)
    assert "hr" not in vend and "hase" not in vend, "mode/system token still a vendor"
    assert not any(v.startswith("iccm") and v != "iccm" for v in vend), "iccm* not folded"
print("OK — no phantom vendor buckets")
PY
```
| Check | Expected |
|---|---|
| sms/email vendor lists | real carriers + maybe `iccm` + `unknown`; **no** `hr`, `hase`, `iccms/iccmh/iccmt/iccmv/iccmpt` |
| 3HK SMSC / CSL / Sinch counts | unchanged from RUNBOOK-50 (17 / 10 / 4) — the real carriers still resolve |
| `unknown` bucket | holds carrier-less jobs (e.g. `mc-hk-hase-sms-deli-job`); still visible under the channel-level `*-deli` node |
| tests | `python -m pytest -q` green |

---

# Step 3 — still box-local, needs officializing (send back / confirm)
These two are from your RUNBOOK-50 box run and are **not yet on `master`**:
1. **`refresh.py --repos-file recon_out/repos.txt`** — without it the next `refresh.py` regresses the
   universe 460→410. **Keep your local edit for now.** To officialize: either commit that one file, or
   paste me its current box diff and I'll match it exactly (I won't guess the wiring blind and risk
   breaking refresh).
2. **`index/arch_map.override.json`** binding `push-aurora` to the 5 aurora delivery repos (Aurora has no
   `*-outbound-api` repo). That's a gitignored box-local index file — fine to keep box-local; note it so
   it isn't lost on a clean rebuild.

# Owner confirm (one item)
The **`iccm*` → `iccm`** fold assumes iccms/iccmh/iccmt/iccmv/iccmpt are all the one ICCM platform. If
any is genuinely a distinct carrier, tell me and I'll split it back out (one line in `canon_vendor`).

## Notes
- `KNOWN_VENDORS` is a whitelist: a **new, unseen** carrier's jobs would fall to `unknown` (visible at
  channel level, just not in a vendor bucket) until its token is added. If Step 2 shows an unexpected
  `unknown` count, list those repos and I'll extend the set.
- Nothing here touches the InfoSec item (Aurora plaintext keystore password) — keep that on its own track.

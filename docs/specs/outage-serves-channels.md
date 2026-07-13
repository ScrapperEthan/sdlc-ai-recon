# Spec (EXTERNAL Codex) — outage report: complete "affected repos" with serves_channels (owners + servers)

> **Who builds: EXTERNAL Codex.** Verify on the box via `RUNBOOK-15`. **Unblocked** — RUNBOOK-13 is green,
> so `index/repo_tags.json` now carries a **clean** `serves_channels` (no "others"). Goal: make
> `outage_report` (`channel:` / `vendor:` / `repo:`) return the **complete** affected-repo set by folding in
> the pre-computed `serves_channels` index, so both UIs (「故障影响」tab + the arch-map outage panel) get
> owners **+** servers in one pass. **stdlib-only, read-only, additive. Do NOT touch the Q&A app.**

## Why (and the honest scope)
Today `affected_repos(groups)` seeds from the delivery-topology jobs + outbound-apis, then walks the
Maven closure of each seed. That misses any channel-serving repo the **topology parse didn't seed** (naming
exceptions, or channels whose jobs aren't fully name-revealed). `repo_tags.json.serves_channels` is the
pre-computed inverse index — `serves_channels(repo) ∋ sms` ⟺ some sms-owning repo depends on `repo` — i.e.
the **library blast-radius** of the channel. Folding it in makes the set provably complete from the index,
independent of topology-parse gaps. **Honest limit (state in banner):** this is still **library-level**
blast-radius; the **messaging pipeline** (ingress→decision→topic→job over Kafka) needs the full message map
— unchanged by this spec.

## Building block 1 — `serves_channel_repos(channels)` helper
Add to `outage_report.py` (or a tiny helper). Load `index/repo_tags.json` (reuse a loader; empty/missing →
`{}`, never crash). For the outage's resolved **channel set** (`resolved["channels"]`), return the repos
whose tags qualify, each with a **relation** and a citation to `repo_tags.json`:
- `channel ∩ channels` non-empty  → relation **`channel-owner`** (the repo itself owns the channel).
- else `serves_channels ∩ channels` non-empty → relation **`serves-channel`** (blast-radius).
- Cite as `index/repo_tags.json` (path only — it's a generated artifact; do not inline repo internals).
- Never treat `other`/`others` as a channel (reuse the `NON_CHANNELS` guard; the data is already clean, keep
  the guard defensive).

## Building block 2 — merge into `affected_repos`
Union the serves-channel rows **under** the topology-derived rows (topology wins the `relation` label):
current precedence is `delivery-job` / `outbound-api` first, then dependency closure; add `channel-owner` /
`serves-channel` with `rows.setdefault(...)` so a repo already labelled `delivery-job` keeps that label.
Result stays a sorted, de-duplicated list. Only applies when the target resolves to ≥1 channel (channel:
and most vendor: targets do; a `repo:` target uses its job's channel).

## Building block 3 — surface the breakdown
- In `build_report`, add `affected_repos.by_relation` = `{relation: count}` (so the UI can group:
  owners / serving / dependency). Keep `affected_repos.count` + `.items` as-is (back-compatible).
- Markdown report: under 受影响组件, group the list by relation with a one-line legend
  (`channel-owner` 拥有该渠道 · `serves-channel` 故障波及(库级) · `delivery-job`/`outbound-api` 投递链 · `dependency-*` 依赖闭包).

## Building block 4 — light UI touch (both surfaces get it free)
`static/impact.html` 「故障影响」panel and `static/arch.html` outage panel already render
`affected_repos.items`. Add a small **relation chip** per repo row (reuse existing chip styles) and, if
present, a one-line `by_relation` summary. No structural rewrite; keep it additive and CSP-safe.

## Tests (fixtures, like existing)
- Extend `tests/test_outage_impact.py`: a fixture `repo_tags.json` where `lib` has
  `serves_channels:["sms"]` and owns no channel, plus a `sms-owner` repo → `channel:sms` includes `lib`
  as `serves-channel` and `sms-owner` as `channel-owner`, and a `delivery-job` repo keeps its label.
- `by_relation` counts add up to `count`. `others` in a fixture `serves_channels` never appears.
- `/outage-impact` JSON still matches the CLI shape.

## Deliver
`outage_report.py` changes, the two small UI touches, README note if needed, tests. Verify on the box via
`RUNBOOK-15` (run `channel:sms` / `vendor:sinch`, confirm the affected set grew to include serving libs
with correct relation chips, counts cited, banners unchanged). Keep everything additive; no new pip deps.

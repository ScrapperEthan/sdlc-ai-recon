# Spec (EXTERNAL Codex) — impact report + repo business-tags/glossary

> **Who builds this: EXTERNAL Codex** (writes code from this spec, pushes a branch).
> Verification on the real estate is a separate **internal-Codex** job (`RUNBOOK-8`).
> Two features here: **A) the impact report** (flagship), **B) repo tags + glossary** (enriches
> retrieval). Both extend the existing retrieval layer; **do not touch the Q&A app**
> (`webapp/*`, `agent.py`, `llm.py`, `static/*`) or change existing `retriever/` function
> signatures — add, don't modify.

Constraints (same as always): **stdlib only, read-only over the estate, no egress, no hardcoded
internal endpoints.** New writes only under `index/` (generated artifacts). Keep it small.

---

## Feature A — the impact report (`impact_report.py` + `/impact-report`)

**Goal.** One command answers the coding team's + leadership's core question: *"For this repo /
use-case / topic, what is upstream (what it depends on), what is downstream (who's affected if it
changes or breaks), which async routes/channels are involved — with citations."* Both directions
(so it serves **incident response** = "an upstream broke, who of ours relies on it" **and change
notification** = "we're changing X, who downstream must be told").

**It only composes existing retriever functions** (no new graph logic):
- `graph.impact(repo, transitive=True)` → **downstream dependents** ("who depends on repo") and
  **upstream deps** ("what repo depends on"). (This function already returns both directions.)
- `messages.routes_for_repo(repo)` → topics/queues this repo produces/consumes.
- `messages.who_produces(dest)` / `who_consumes(dest)` → the other repos on each async route.
- `flow.trace(use_case_id=..., destination=...)` → stitch a use-case or topic across the wiring.
- `graph.hubs()` → flag hub repos sitting in the blast path (extra risk).
- Channel inference: derive channel from topic/repo name keywords (`sms|email|push|whatsapp|
  wechat|letter`); if `index/repo_tags.json` (Feature B) exists, prefer its `channel` tag.

**CLI:**
```
python impact_report.py <repo>
python impact_report.py use-case:<id>
python impact_report.py topic:<name>
python impact_report.py <repo> --out index/reports    # default prints to stdout + writes a .md
```
**Endpoint** on `retrieval_service.py` (additive route): `GET /impact-report?target=<...>` → the
same content as JSON.

**Output — `IMPACT_REPORT_<target>.md`** (and the JSON equivalent), sections:
- **Target** — what was asked (repo / use-case / topic), and its channel(s) if known.
- **Upstream (what it depends on)** — repos/libs this target relies on → *"if these fail, target is at risk."*
- **Downstream (who's affected)** — repos that depend on the target (direct + transitive) → *"notify these on change/outage."*
- **Async routes** — per topic/queue the target touches: producers + consumers (the hidden cross-repo coupling).
- **Channel chain** — which channel(s) this flows through (PN/SMS/WhatsApp/Email/…).
- **Risk callouts** — hub repos in the path; and an **honesty note** when use-case→topic routing
  is only partly provable from source (routing lives in a DB table; we only have a dev/SCT snapshot).
- **Citations** — every concrete claim carries `repo/path:line` (or the graph/message-map row it came from). Never assert a path the tools didn't return.

**Acceptance / tests** (`tests/test_impact_report.py`, fixture graph like the existing tests):
- repo target → report lists both upstream and downstream sets correctly.
- topic target → lists producers + consumers.
- unknown target → clean error (not a crash).
- `/impact-report` returns the same structure as the CLI JSON.

---

## Feature B — repo tags + glossary (`make_repo_tags.py`, glossary loader, `/repos`)

**Goal.** Make retrieval **narrow-first** and make answers understand the estate's abbreviations.
Two artifacts, both under `index/` (gitignored — real content lives on the box; ship only generic
fixtures/examples):

### B1. Glossary — `index/glossary.json`  (authored, box-local)
A flat `{token: meaning}` map that expands the estate's naming abbreviations. **The real one is
authored on the box** (from the team's naming sheet); in this repo ship only a **generic example
fixture** for tests, e.g. `{"svc":"servicing","rt":"realtime","bat":"batch","job":"scheduled job"}`.
Add a tiny loader `retriever/glossary.py`:
```python
def load(path="index/glossary.json") -> dict   # returns {} if absent (never crash)
def expand(name: str) -> str                    # annotate a repo name's tokens with meanings
```
Wire `expand()` into: (a) the `/repomap` output and (b) the impact-report's target description,
so abbreviations like `svc-rt-hr` render with their meanings. **No behavior change if the file is absent.**

### B2. Per-repo tags — `make_repo_tags.py` → `index/repo_tags.json`
Stdlib, read-only over `recon_out/internal_edges.csv` (+ pom-only repos, like `make_bundles.py`),
**auto-derives** per repo from name patterns, and **leaves a slot for manual curation**:
- `system` — from prefix: `mc-hk-hase-`→`hase`, `amet-mdc-hsbc-`→`amet-mdc`, `ai-`→`ai`,
  `aws-tf-`→`infra`, `shp-`→`shp`, `doris-`→`data` (table-driven; unknown → `other`).
- `channel[]` — name contains `sms|email|push|whatsapp|wechat|letter` → those channels.
- `mode` — `rt`→`realtime`, `bat|batch`→`batch`, else `job`/`api`/`core`/`lib` by suffix.
- `tokens[]` — the remaining name tokens (for glossary expansion).
- `bundle` — the repo's bundle from `index/bundles.json` if present.
- Anything not derivable is left blank; a hand-curated `index/repo_tags.override.json`
  (box-authored) is merged on top so humans only fill what names don't encode
  (e.g. sensitivity, CMB/WPB business unit, marketing-vs-time-critical).
Emit `index/repo_tags.json` and print a coverage table: how many repos got a `system`/`channel`/
`mode`, and how many are `other`/unknown (so we see what still needs curation).

### B3. Filter endpoint on `retrieval_service.py`
`GET /repos?channel=sms&mode=realtime&system=hase&bundle=<name>` → the matching repo list from
`index/repo_tags.json` (AND of the given filters). Missing tags file → `{"error": "no repo_tags.json"}`, 404.

**Acceptance / tests** (`tests/test_make_repo_tags.py`, `tests/test_glossary.py`):
- derivation: a `*-rt-*-sms-*` fixture repo → `mode=realtime`, `channel=["sms"]`, correct `system`.
- override merge wins over derived.
- `glossary.expand("svc-rt")` uses the fixture glossary; absent file → returns input unchanged.
- `/repos` filters correctly; missing file → 404.

---

## Deliver
One branch with: `impact_report.py`, `make_repo_tags.py`, `retriever/glossary.py`, the two new
`retrieval_service.py` routes (`/impact-report`, `/repos`), generic fixtures, and the tests above.
Update `README.md`'s file table. **Do not** commit real `index/*.json` (gitignored). Then internal
Codex runs `RUNBOOK-8` on the real estate.

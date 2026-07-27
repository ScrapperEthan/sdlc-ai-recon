# Spec — Ingestion ownership seam: externalize every schema/vocabulary binding

> **Goal:** make "the data changed" a **JSON edit by the intranet Codex**, never a Python edit and
> never a photo relayed to Claude. The MDC sheet already works this way (`mdc_sheet_schema.json` +
> `enrich_repo_tags.py`, commit `ca7c4e3`). This spec generalizes that one working seam to the other
> **four** ingestion points that still bind columns/vocabulary in Python.
>
> **Who builds:** Claude / external-pushable side. This touches the *scaffolding* (loader engines,
> `config.py`, `.gitignore`, `refresh.py`) which is Claude-owned. Codex then owns only the JSON knobs.
>
> **Hard constraints (unchanged):** stdlib-only, read-only over snapshots, artifacts stay gitignored,
> **no real repo rosters / row data / person names ever committed**, full backward compat with every
> existing tool + API.

---

## 1. Why — the two findings that make this necessary

**Finding A — four of five ingestion points have no knob.** Only the MDC sheet is externalized:

| # | Ingestion point | Where the binding lives today | Churn evidence |
|---|---|---|---|
| 1 | MDC repo-list xlsx | `mdc_sheet_schema.json` (root, **committed**) | ✅ already a knob |
| 2 | UAT 3 tables (`tbl_use_case` / `_channel_rule` / `_ext`) | `retriever/usecase_catalog.py:54` `_FIELD_SPECS`, ~63 field entries **in Python** | sheet+schema "随时更新" |
| 3 | repo name → channel / vendor | `make_delivery_topology.py:30-46` (`VENDOR_ALIASES`, `KNOWN_VENDORS`, `CHANNELS`), `make_repo_tags.py:12-25` (`SYSTEM_PREFIXES`, `CHANNEL_KEYWORDS`, `MODE_ALIASES`, `ROLE_SUFFIXES`) **in Python** | **RUNBOOK-49 (`3cc5f2a`), -50 (`598c0c0`), -51 (`9a9bf01`) all edited the same file** — three consecutive fixes, pure vocabulary |
| 4 | producer / send-site recognition | `producer_extract.py:41-59` (`PRODUCER_BASES`, `PRODUCER_SUFFIXES`, `SEND_METHODS`, `FRAMEWORK_RECEIVER_TYPES`) **in Python** | seeds are explicitly "extend from real data" |
| 5 | business enums + semantics | `usecase_catalog.py:27` `BUSINESS_CATEGORY_ENUM` (33/37 unnamed) + `index/rule_text_semantics.json` + `index/source_system_aliases.json` | owner answers pending |

Row 3 is the proof: **three runbooks in a row were vocabulary edits wearing a code-fix costume.** Every
one of them could have been a JSON line if the knob existed.

**Finding B — the knobs that *do* exist are inside a gitignored directory.** `.gitignore` carries
`index/*.json`, so `index/rule_text_semantics.json`, `index/source_system_aliases.json` and the three
`index/*.override.json` files **cannot be committed**: a Codex edit on the box is stuck there, invisible
to Claude, and lost on re-clone. This is exactly why `arch_map.override.json` (Aurora) is still box-local.
`mdc_sheet_schema.json` works *only because it happens to sit at the repo root.*

`index/*.json` must **stay** gitignored — it is a deliberate no-egress control (the public repo must never
carry the real roster / row data). The fix is therefore **not** to un-ignore `index/`, but to separate
**vocabulary (committable)** from **data (never committable)**.

**Committable-vocabulary precedent already exists:** vendor names (`3hk`, `sinch`, `aurora`, `iccm`) are
already in committed Python at `make_delivery_topology.py:39`, and `PEGA`/`MDC`/`eAlert` counts are already
in committed specs. Column names and vocabulary are safe; **rosters and rows are not.**

---

## 2. Design — one committed `config/` directory, one exception report

### 2.1 New committed directory `config/`

Vocabulary and column maps move to `config/*.json` (**committed**). `index/` keeps generated artifacts and
raw data (**gitignored, unchanged**).

| New knob | Replaces | Owner |
|---|---|---|
| `config/usecase_columns.json` | `usecase_catalog.py` `_FIELD_SPECS` (+ `_JUNK_WORK_STREAM`, `_DATE_FORMATS`) | Codex |
| `config/naming_vocab.json` | `make_delivery_topology.py` `CHANNELS`/`VENDOR_ALIASES`/`KNOWN_VENDORS`/`MSG_QUALIFIERS`, `make_repo_tags.py` `SYSTEM_PREFIXES`/`CHANNEL_KEYWORDS`/`MODE_ALIASES`/`ROLE_SUFFIXES`/`NON_CHANNELS` | Codex |
| `config/producer_seeds.json` | `producer_extract.py` `PRODUCER_BASES`/`PRODUCER_SUFFIXES`/`SEND_METHODS`/`FRAMEWORK_RECEIVER_TYPES` | Codex |
| `config/business_enums.json` | `usecase_catalog.py` `BUSINESS_CATEGORY_ENUM` (incl. the empty **33**/**37** slots), delivery-mode + channel vocabulary | Codex, after owner answer |
| `config/rule_text_semantics.json` | **moved** from `index/` (B2 seam — currently uncommittable) | Codex, after owner answer |
| `config/source_system_aliases.json` | **moved** from `index/` | Codex |
| `config/mdc_sheet_schema.json` | **moved** from repo root (one home for all knobs) | Codex |

`retriever/config.py` resolves each with the existing `_p()` env-override pattern, and for the three moved
files **falls back to the old path** if the new one is absent, so nothing breaks mid-migration. Update
`docs/MDC-SHEET-CODEX-HANDOFF-zh.md` to the new path.

### 2.2 The four rules every knob obeys (the "MDC pattern", now the standard)

Lifted verbatim from the working `enrich_repo_tags.py` behaviour:

1. **Missing file → built-in default**, never a crash. Deleting all of `config/` must reproduce today's
   output byte-for-byte (defaults stay in Python as `DEFAULT_*` dicts).
2. **Unknown / unbound input → captured generically + reported**, never a crash. No `raise` on a header
   or vocabulary mismatch anywhere.
3. **Blocks replace, they do not deep-merge** (matches current MDC semantics — a partial block is a
   whole-block override).
4. **`_README` keys are ignored**, so every knob carries its own instructions for Codex.

### 2.3 One aggregated exception report — the single artifact that ever needs relaying

Each ingestion script already knows what it could not bind. Standardize the surface
(`enrich_repo_tags.print_coverage`'s "Unbound schema fields" block is the model) and have `refresh.py`
aggregate all five into **one** file:

```
index/reports/INGESTION-EXCEPTIONS.md
```

Sections, each empty-when-clean:

- **Unbound columns** — semantic field ↔ which knob to edit (per ingestion point).
- **Unknown vocabulary** — repo-name tokens that resolved to `unknown` vendor/channel, with one example
  repo each (this is where a new carrier shows up).
- **Count deltas** — row/repo/edge counts vs the previous `last_indexed.json`, flagged past a threshold
  (catches a silently truncated export).
- **Box-local overrides in play** — for each `index/*.override.json`: exists / key count / SHA-256, **no
  values**. Solves the invisible-Aurora-override problem without egressing repo names.
- **Owner-gated blanks** — anything still `unconfirmed` (rule_text operators, category 33/37).

This file is the *only* thing that needs to travel back, it is a handful of lines, and it is designed to be
one screenshot. Keep it out of git (`index/reports/` is already ignored).

---

## 3. Acceptance criteria

1. **Zero behaviour change on extraction.** Capture `index/repo_tags.json`, `delivery_topology.json`,
   `arch_map.json`, `message_edges.csv` before and after; artifacts must be **byte-identical**. Then
   delete `config/` entirely and re-run — still byte-identical (defaults intact).
2. **The three past runbooks replay as JSON-only edits.** Add tests proving each of RUNBOOK-49/50/51's
   vocabulary change is now reachable through `config/naming_vocab.json` **with no Python edit**:
   `htcl→3hk` alias, a new known vendor, and the rightmost-known-vendor whitelist behaviour.
   (`tests/test_vendor_alias_3hk.py` already covers the outcomes — point them at the knob.)
3. **A renamed UAT column binds via aliases only.** Fixture where `SourceSystem` → `Src_System`; adding
   the alias to `config/usecase_columns.json` restores full binding, no Python change, and an unbound
   field is reported rather than raised.
4. **A brand-new UAT column is captured, not dropped or fatal** (mirrors the MDC "Brand New Flag" test).
5. **Exception report is honest when clean** — all sections present and explicitly empty; and it names the
   knob file to edit for every non-empty finding.
6. **No secrets/egress regression:** `git status` after a full `refresh.py` shows no new tracked file
   containing repo rosters, row data, or person names. `config/*.json` contains vocabulary only.
7. Existing suite (401 tests at `ca7c4e3`) stays green; new tests added per the above.

---

## 4. Explicitly out of scope

- **Not** un-ignoring `index/` (security control stays).
- **Not** changing any artifact contract — `repo_tags.mdc.json`'s 6 hard fields, `mdc_roster.json`,
  `message_edges.csv`'s first 5 columns all stay exactly as they are.
- **Not** asserting any owner-gated meaning: `rule_text` operators and categories 33/37 stay
  `unconfirmed` until an owner answers. This spec only makes the answer *committable*.
- **Not** the UAT→SQLite loader (`build_usecase_db.py`) — that is a separate Codex-owned build.

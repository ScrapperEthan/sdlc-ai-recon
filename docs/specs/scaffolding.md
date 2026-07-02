# Spec: new-module scaffolding generator (golden path) — Step 2

**Audience:** external Codex IMPLEMENTS (its box has **no `mirror/`** — build & test
against fixtures); internal Codex RUNS it against the real `mirror/` and confirms
coordinates. Read `BACKLOG.md` "Project context" + "Guardrails" first. This
**evolves the existing stub** `scaffold/generate.py` (BACKLOG item #11) — it is not
a new parallel tool.

## Goal

"Create a new service/module for X" → a scratch skeleton that **actually follows the
shared template** every HASE repo inherits: parent POM `mc-hk-hase-api-parent`
(Java 21, Spring Boot governed by the parent) + the shared starter
`mc-hk-hase-api-starter` (used by 277/390 repos). The template is **derived from a
real reference service in the mirror**, not hand-invented. Output to `scratch/` for
human review only — never into a production repo.

## Why now

The estate is one template repeated ~390 times; that uniformity is exactly what makes
generation tractable — and what the current stub ignores. Deriving the template from
the mirror (instead of hardcoding) keeps it truthful as the estate evolves — the same
"retrieval is the moat" thesis behind the Q&A layer.

### What the current stub gets wrong (the gap this closes)

`scaffold/generate.py` today emits a pom with **no `<parent>`**, sets
`java.version` to **17** (real stack is **21**), redeclares properties the parent
should govern, and hardcodes a placeholder listener/controller. It compiles-shaped
but does **not** match the golden template. Fixing that is the whole task.

## Where

- Evolve `scaffold/generate.py`; add `scaffold/reference.py` (mirror derivation) and
  `scaffold/tests/` (fixtures + tests).
- May reuse `retriever.code.read_file` for reading the mirror (read-only). Only read
  `mirror/` — never a `hase-mc` repo directly.
- CLI stays `python -m scaffold.generate <name> --package ... [--out-dir scratch] [--force]`;
  add `--reference <repo>` (default `mc-hk-hase-ingress-api`) and, in Phase 2,
  `--with-core`.

## Anchor artifacts in the mirror (the golden-template source)

- `mc-hk-hase-api-parent` — parent POM every repo inherits (governs Java 21, Spring
  Boot version, dependency management). Read its `groupId/artifactId/version`.
- `mc-hk-hase-api-starter` — shared starter (277/390 depend on it). Read coordinates;
  add as a dependency.
- `mc-hk-hase-ingress-api` — representative **thin** service shell (config +
  controllers only).
- `mc-hk-hase-api-ingress-core` — the matching `*-core` lib where the real logic lives
  (`IngressService`, `EventProducerService`). This is the thin→core split (Phase 2).

---

## Phase 1 (must land first): a truthful single-module scaffold

Smallest change that makes the output faithful to the template.

### Design decisions (made)

- **Inherit, don't restate.** Generated `pom.xml` declares `<parent>` = api-parent's
  real coordinates and depends on api-starter. It does **NOT** set `java.version`,
  the Spring Boot version, or build-plugin config — those come from the parent. The
  stub's `<properties><java.version>17</java.version></properties>` block is removed.
- **Derive coordinates from the mirror at generate-time**, don't hardcode.
  `scaffold/reference.py` parses the anchor POMs (stdlib `xml.etree.ElementTree`) for
  parent + starter coordinates and the base-package convention. If `mirror/` is absent
  (external Codex box), fall back to documented defaults and print a NOTE that internal
  Codex must confirm.
- **Scratch only.** Output under `scratch/<name>/`; reject `..` in package/paths;
  never write outside `--out-dir`; never touch `mirror/`. (Already true in the stub —
  keep it.)
- **Stdlib only.** XML via `xml.etree`. No new dependencies.
- **Human-review artifact.** Keep/extend `REVIEW_DIFF.md`: list files + a checklist
  that **cites the anchor files** the conventions came from (`repo/path:line`), so a
  reviewer can diff the skeleton against the reference service.

### POM shape (child of api-parent)

```xml
<project xmlns="http://maven.apache.org/POM/4.0.0" ...>
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>{parent.groupId}</groupId>
    <artifactId>mc-hk-hase-api-parent</artifactId>
    <version>{parent.version}</version>
  </parent>
  <artifactId>{name}</artifactId>
  <version>0.1.0-SNAPSHOT</version>
  <packaging>jar</packaging>
  <dependencies>
    <dependency>
      <groupId>{starter.groupId}</groupId>
      <artifactId>mc-hk-hase-api-starter</artifactId>
    </dependency>
  </dependencies>
</project>
```

No `<java.version>`, no Spring Boot version — inherited. If the reference service
declares `spring-boot-maven-plugin` explicitly (rather than inheriting it), copy that
`<build>` stanza verbatim and cite where it came from.

### `scaffold/reference.py` sketch

```python
"""Derive golden-template coordinates from the mirror (read-only, stdlib)."""
import os, xml.etree.ElementTree as ET

M = "{http://maven.apache.org/POM/4.0.0}"   # POMs may or may not use the namespace

DEFAULTS = {  # recon-derived guesses; internal Codex confirms against the mirror
    "parent":  {"groupId": "com.hsbc.hase", "artifactId": "mc-hk-hase-api-parent", "version": "<CONFIRM>"},
    "starter": {"groupId": "com.hsbc.hase", "artifactId": "mc-hk-hase-api-starter"},
    "base_package": "com.hsbc.hase",
}

def _find(root, tag):
    el = root.find(f"{M}{tag}")
    if el is None:
        el = root.find(tag)          # namespace-less fallback
    return el.text.strip() if el is not None and el.text else None

def _coords(pom_path):
    root = ET.parse(pom_path).getroot()
    gid = _find(root, "groupId")
    if gid is None:                  # inherited from <parent>
        parent = root.find(f"{M}parent") or root.find("parent")
        if parent is not None:
            gid = _find(parent, "groupId")
    return {"groupId": gid, "artifactId": _find(root, "artifactId"),
            "version": _find(root, "version")}

def load_template(mirror="mirror"):
    parent_pom  = os.path.join(mirror, "mc-hk-hase-api-parent",  "pom.xml")
    starter_pom = os.path.join(mirror, "mc-hk-hase-api-starter", "pom.xml")
    if not os.path.exists(parent_pom):
        print("NOTE: mirror not found; using documented defaults — internal Codex must confirm.")
        return DEFAULTS, False
    return {"parent": _coords(parent_pom), "starter": _coords(starter_pom),
            "base_package": DEFAULTS["base_package"]}, True
```

### Fixtures (so external Codex can build + test without the mirror)

Under `scaffold/tests/fixtures/mirror/`:
- `mc-hk-hase-api-parent/pom.xml`, `mc-hk-hase-api-starter/pom.xml` — tiny POMs with
  the coordinates to extract.
- `mc-hk-hase-ingress-api/` — a minimal service tree with one `@RestController`
  Resource and one `@JmsListener`, so convention extraction has something real to read.

`scaffold/tests/test_generate.py` (stdlib `unittest`):
1. `load_template(fixtures_mirror)` → correct coords + `True`.
2. `load_template("does-not-exist")` → `DEFAULTS` + `False`.
3. generated `pom.xml` contains a `<parent>` block and **no** `<java.version>`.
4. a `..`-containing package is rejected.
5. nothing is written outside `--out-dir`.

## Phase 1 — Done when

1. `python -m scaffold.generate payments --package com.hsbc.hase.payments` writes
   `scratch/payments/` whose `pom.xml` **inherits `mc-hk-hase-api-parent`**, depends on
   `mc-hk-hase-api-starter`, and restates **no** Java/Boot version.
2. `python -m unittest discover scaffold/tests` passes — including the mirror-absent
   fallback and the path-traversal rejection.
3. `REVIEW_DIFF.md` lists every file and cites the anchor files the conventions came from.

---

## Phase 2 (next, after maintainer confirms repo-vs-module): thin + core split

Recon shows a service is a **thin `*-api` shell** with the real logic in a separate
`*-core` lib. Add `--with-core` to emit both:

- `{name}-api` — `Application`, one Resource/controller, config; depends on `{name}-core`.
- `{name}-core` — a `<Name>Service` + a sample producer and `@JmsListener`, modeled on
  `IngressService` / `EventProducerService` in `mc-hk-hase-api-ingress-core`.

**Needs a decision (maintainer):** in the real estate api and core are *separate
repos*, not two Maven modules in one tree. Confirm whether scaffolding should emit two
scratch repo-shaped folders (default) or a single multi-module tree before building
this. Until confirmed, `--with-core` is off by default and Phase 1's single module is
the shipped path.

## Keep working

- Additive to the stub's public `generate_service(...)`; keep its signature working
  (single-module = the default path). Nothing in `webapp/` or `retriever/` changes
  except optional read-only reuse of `retriever.code`. `.gitignore` already ignores
  `scratch/`.

## Verification steps (internal Codex, real mirror)

```bash
python -m scaffold.generate payments --package com.hsbc.hase.payments \
  --reference mc-hk-hase-ingress-api --out-dir scratch
```

Confirm against the mirror: the emitted parent coordinates/version equal
`mirror/mc-hk-hase-api-parent/pom.xml`; the starter dependency matches; the package
layout + `@RestController` / `@JmsListener` style match `mc-hk-hase-ingress-api`. **If
the real base package or coordinates differ from `DEFAULTS`, update `DEFAULTS`** — that
is the one thing to confirm on the box. NEVER run against or write into a `hase-mc`
repo — scratch only.

## Notes

- Keep the generator's convention claims **cited** (which mirror file each came from),
  same discipline as the Q&A layer — a reviewer should be able to check every choice.
- The point isn't a compiling jar; it's a skeleton a HASE engineer recognizes as "one
  of ours" and can fill in. Faithfulness to the template beats feature count.

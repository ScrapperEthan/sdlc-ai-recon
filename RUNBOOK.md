# RUNBOOK — internal recon (run this with opencode on the internal Windows machine)

You are opencode running on an internal Windows machine that CAN reach the
internal GitHub. Your job: characterize a ~400-repo Java system that forms ONE
product, extract its cross-repo dependency graph, do a few qualitative analyses,
and produce a single report (`RECON-REPORT.md`) that the user will carry out.

**Hard rule:** do NOT copy source code of the repos into the report. Only
aggregated findings, metadata, and SHORT cited snippets (a few lines, with
`repo/path:line`). The report must be safe to take off the internal network.

## Prerequisites (check first, report what you find)

1. `gh auth status` — confirm you are logged in to the **internal** GitHub host.
   - If repos are on an enterprise host and you are not logged in there:
     `gh auth login --hostname <INTERNAL_HOST>` then set it for this session
     (PowerShell: `$env:GH_HOST="<INTERNAL_HOST>"`).
2. `python --version` — need Python 3.7+.
3. These files sit next to this runbook: `harvest_poms.py`,
   `recon_maven_graph.py`, `prompts/recon-opencode-tasks.md`.

Ask the user for the **<ORG>** (the GitHub org/owner that holds the 400 repos)
if you don't know it.

## Steps

```
# 0. Sanity check — report the host + a few repo names back
gh auth status
gh repo list <ORG> --limit 5

# 1. Harvest only the pom.xml from every repo (no clone; a few MB)
python harvest_poms.py <ORG> ./poms

# 2. Build the verdict + cross-repo dependency graph
python recon_maven_graph.py ./poms
#    -> recon_out/summary.txt, internal_edges.csv, top_shared.csv, produced.csv
```

3. **Qualitative analyses.** Run the three tasks in
   `prompts/recon-opencode-tasks.md` (read code, cite `repo/path:line`):
   - Task 1 — runtime coupling: REST/Feign vs Kafka/EventBus; sync or event-driven?
   - Task 2 — platform base: the shared parent POM / `*-starter`; what it standardizes; Spring Boot + Java versions.
   - Task 3 — one representative service walked end-to-end (a `REPO-GUIDE.md` draft).
   For 3, pick the service from the **hub repos** in `recon_out/summary.txt`
   (a heavily-depended-on area is the most informative slice).

4. **Assemble `RECON-REPORT.md`** with exactly these sections:

   - **A. Environment** — gh host, `<ORG>`, repo count, Python OK?, anything that blocked you.
   - **B. Structural verdict** — paste `recon_out/summary.txt` verbatim.
   - **C. Top shared libraries** — the table from `top_shared.csv` (top 15).
   - **D. Runtime coupling** — Task 1 output (the table + sync/async verdict).
   - **E. Platform base** — Task 2 output (parent/BOM repo, conventions, versions).
   - **F. Representative slice** — Task 3 output (the REPO-GUIDE draft).
   - **G. Open questions** — anything ambiguous, truncated trees, repos you couldn't access.

5. **Hand back to the user**, for them to carry out:
   `RECON-REPORT.md`, plus `recon_out/summary.txt` and `recon_out/top_shared.csv`.

## Notes

- The harvester uses one git-tree API call per repo; for a very large repo the
  tree may be truncated — note such repos in section G.
- If `harvest_poms.py` errors on auth, it's almost always the wrong GH_HOST.
- Keep `RECON-REPORT.md` to architecture/metadata. When in doubt, summarize
  rather than quote.

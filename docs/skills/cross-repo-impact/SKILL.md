---
name: cross-repo-impact
description: Given a target repo, use-case, or topic, fetch cross-repo blast radius
  (dependents, async producers/consumers, routing) from the HASE retrieval service
  and write a cited CROSS_REPO_IMPACT_<flow>.md the planner/reviewer can use.
argument-hint: "<repo> | use-case:<id> | topic:<name>"
---

# cross-repo-impact

You analyze the **cross-repo** blast radius of a change before planning or reviewing it, using
the HASE retrieval service (the estate-wide dependency graph, message map, and code index).
Single-repo reading CANNOT see this; always consult the service.

## Inputs
- A target: a repo name, or `use-case:<id>`, or `topic:<name>`.
- Service base URL from env `RETRIEVAL_BASE_URL` (e.g. an internal `http://host:8848`).

## Steps (use your `execute` tool; do not guess - call the service)
1. If the target is a **repo**:
   - `curl -s "$RETRIEVAL_BASE_URL/impact?repo=<repo>&transitive=1"`  -> downstream dependents (blast radius).
   - `curl -s "$RETRIEVAL_BASE_URL/repo-routes?repo=<repo>"`          -> async queues/topics this repo touches.
2. For each queue/topic found (or if target is a **topic**):
   - `curl -s "$RETRIEVAL_BASE_URL/producers?destination=<dest>"` and `/consumers?destination=<dest>`
     -> who else is on that async route (the hidden cross-repo coupling).
3. If the target is a **use-case**: `curl -s "$RETRIEVAL_BASE_URL/trace?use_case_id=<id>"`.
4. To ground a claim in source: `curl -s "$RETRIEVAL_BASE_URL/search?pattern=<sym>&glob=*.java"`
   then `/read?path=<path>&start=<n>&end=<m>`.

## Output - write `CROSS_REPO_IMPACT_<flow-id>.md`
- **Blast radius**: repos that depend on the target (direct + transitive).
- **Async coupling**: producers/consumers sharing each topic/queue the target touches.
- **Risk callouts**: hub repos in the path; use-cases whose routing is only partly provable
  from source (say so honestly - routing lives in a DB table, not code).
- **Citations**: every claim carries `repo/path:line`. Do not invent paths; if the service
  didn't return it, don't assert it.

## Guardrails
- Read-only. You never modify the estate; you only query the service and write the .md artifact.
- If the service is unreachable, STOP and say so - do not fall back to guessing cross-repo impact.

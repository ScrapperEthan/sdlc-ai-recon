"""The fifth channel layer: source/config evidence that carries a citable line.

## What was wrong with four layers

"Which channels does this change/outage affect" was answered from four inputs, and every one of
them is an inference about the repo rather than an observation of what it does:

| layer | field | what it actually knows |
| --- | --- | --- |
| name-derived | `channel` | the repo NAME contains `sms`/`email`/… |
| business-declared | `channel_declared` | the MDC sheet ticked a column |
| message-carried | `msg_channels` | a topic it touches carries the channel |
| dependency-propagated | `serves_channels` | something downstream of it owns the channel |

The first is the authoritative one, and it is a string match on a name. So a repo whose name does
not say `sms` had no channel, the spread on any impact answer was a lower bound over a mostly
untagged set, and the caveat that said so was easy to stop reading.

RUNBOOK-77 added the missing observation: the intranet scanned the mirror and CodeGraph and produced
per-repo evidence with a `repo/path:line` citation. That is the first layer that can answer *why*.

## The three states of "no evidence", and why they must not be one

This module's central rule. A repo with no evidence record is in exactly one of:

1. **scanned, genuinely clean** — someone looked and found nothing. A real finding.
2. **scanned, found something, could not cite it** — the intranet reported 55 of these. Not clean,
   and not unknown either.
3. **never scanned** — 377 of ~460 repos were in scope by design, so this is the common case.

Collapsing these into "unknown" would be the failure this codebase keeps closing: a keyword miss
reported as a hit, a 0% standby route read as switched off, absence dressed as an answer. Here it
would be worse than usual, because the output is a notification list somebody acts on — a repo
that was never looked at, reported as "no channel", is a team that does not get told.

The evidence file cannot express any of that: it lists what was FOUND. So a second file says what
was LOOKED AT (`scope_file` in the contract), and **when it is absent this module refuses to
distinguish states 1 and 3** — `scope_known` goes False and everything with no evidence is reported
as `unknown_scope_unknown`. That is deliberately the unflattering reading.

## Why the contract is a config file

`config/channel_evidence.json` holds the field names, the enums and the ranking. Across five
verification rounds every defect was this repo asserting something about the intranet's
environment, sharpening from their **names** to their **shapes** to their **value formats**. Their
generator is theirs; if it renames a field or adds a channel, that must cost a config edit on the
box, not a release from here. The reader also accepts three different top-level shapes for the same
reason — see `normalise()`.

## What this module will not do

* **Not merge into `channel`.** A channel written into the authoritative tag becomes
  indistinguishable from one derived from the repo name, and the provenance is the entire point.
* **Not return `note` by default.** The notes are produced by scanning source, so they can carry
  code fragments and vendor identifiers, and they would flow into the model and onto the page. The
  citation is the evidence; the note is commentary. Opt in explicitly, through the egress gate.
* **Not silently drop.** Every rejected record is counted by reason, because "0 records loaded"
  and "322 records, 322 rejected" need different remedies and must never look the same.
* **Not silently merge a conflict.** Evidence saying `email` for a repo whose name says `sms` is a
  finding, not an average.
"""
import json
import os
import re

from . import citations, config, repo_tags

# Mirrors the shipped config/channel_evidence.json. Used when that file is absent or unreadable so
# the layer degrades to "built-in contract" rather than to "no channel evidence at all".
DEFAULT_CONTRACT = {
    "evidence_file": "index/repo_channel_evidence.json",
    "scope_file": "index/repo_channel_scan_scope.json",
    "channels": ["sms", "email", "push", "whatsapp", "wechat", "letter"],
    "basis": ["code", "config", "doc", "owner"],
    "confidence": ["high", "low"],
    "field_aliases": {
        "repo": ["repo", "repository", "repo_name"],
        "channels": ["channels", "channel"],
        "basis": ["basis", "source_type", "evidence_type"],
        "confidence": ["confidence", "conf"],
        "citation": ["citation", "cite", "ref", "reference"],
        "note": ["note", "comment", "remark"],
    },
    "scope_aliases": {
        "scanned": ["scanned", "scanned_repos", "in_scope", "repos"],
        "unresolved": ["unresolved", "unresolved_repos", "found_but_uncitable"],
        "generated_at": ["generated_at", "timestamp", "created_at"],
        "codegraph_manifest": ["codegraph_manifest", "manifest", "snapshot"],
    },
    "relation_order": [
        "direct_code_evidence",
        "direct_config_evidence",
        "business_declared",
        "name_derived",
        "message_carried",
        "transitive_dependency",
    ],
    "demote_low_confidence": True,
    "include_notes_by_default": False,
}

# basis -> relation. `doc`/`owner` are business assertions rather than observations of the code, so
# they land on the same relation the MDC sheet does; the `basis` field keeps them distinguishable.
_BASIS_RELATION = {
    "code": "direct_code_evidence",
    "config": "direct_config_evidence",
    "doc": "business_declared",
    "owner": "business_declared",
}
_DIRECT_EVIDENCE_RELATIONS = frozenset({"direct_code_evidence", "direct_config_evidence"})
# Relations that mean "this repo itself handles the channel". `transitive_dependency` is absent on
# purpose: it means "would be affected", which is a different question and a different answer.
OWNERSHIP_RELATIONS = frozenset({
    "direct_code_evidence", "direct_config_evidence", "business_declared", "name_derived",
})

# confidence values that can appear on a relation:
#   high/low    — stated by the evidence file (the generator's own judgement)
#   structural  — read off a name or a topic; not graded, and not the generator's claim
#   derived     — inferred through the dependency graph; never ownership
_STRUCTURAL = "structural"
_DERIVED = "derived"

_CITATION_RE = re.compile(r"^(?P<path>[^\s:][^\s]*?):(?P<line>\d+)$")


def _norm_key(text):
    return re.sub(r"[\s_\-]+", "", str(text or "")).lower()


def _read_json(path):
    """Parsed JSON, or None when the file is absent/unreadable/not JSON.

    ValueError covers both JSONDecodeError and UnicodeDecodeError — the second is what a GBK save on
    a Chinese-Windows box raises, and it has taken down a whole report before.
    """
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None


_CONTRACT_CACHE = {}


def contract(path=None):
    """The contract knob, falling back to DEFAULT_CONTRACT per missing key (not wholesale).

    Per-key fallback matters: a box that overrides only `channels` must not lose the field aliases.

    Cached on (path, mtime, size) because a hub repo asks for it once per downstream repo — several
    hundred re-reads of the same small file per answer. Keyed on the stat so an edit on the box
    still takes effect without a restart.
    """
    target = path or config.CHANNEL_EVIDENCE_CONTRACT_JSON
    try:
        stat = os.stat(target)
        key = (target, stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (target, None, None)
    if key in _CONTRACT_CACHE:
        return _CONTRACT_CACHE[key]

    payload = _read_json(target)
    merged = dict(DEFAULT_CONTRACT)
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name.startswith("_"):
                continue
            merged[name] = value
    _CONTRACT_CACHE.clear()
    _CONTRACT_CACHE[key] = merged
    return merged


def _pick(entry, aliases, names):
    """First value in `entry` whose key matches any alias for `names`, else None."""
    wanted = [_norm_key(name) for name in (aliases.get(names) or [names])]
    for key, value in entry.items():
        if _norm_key(key) in wanted:
            return value
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def parse_citation(text):
    """`(path, line)` for a well-formed `repo/path:line`, else `(None, None)`.

    Line must be a positive integer. `path:0` is rejected rather than treated as "no line": a zero
    line number is a generator bug, and accepting it would let an unverifiable citation pass.
    """
    match = _CITATION_RE.match(str(text or "").strip())
    if not match:
        return None, None
    line = int(match.group("line"))
    if line <= 0:
        return None, None
    return match.group("path"), line


def _record(repo, channel, basis, confidence, citation, note, spec, dropped):
    """Validate one flattened record. Returns the record, or None after counting a drop reason."""
    channel = str(channel or "").strip().lower()
    if channel not in spec["channel_set"]:
        dropped["unknown_channel"] = dropped.get("unknown_channel", 0) + 1
        return None

    basis = str(basis or "").strip().lower()
    if basis not in spec["basis_set"]:
        dropped["unknown_basis"] = dropped.get("unknown_basis", 0) + 1
        return None

    raw_citation = str(citation or "").strip()
    if not raw_citation:
        dropped["no_citation"] = dropped.get("no_citation", 0) + 1
        return None
    path, line = parse_citation(raw_citation)
    if not path:
        dropped["malformed_citation"] = dropped.get("malformed_citation", 0) + 1
        return None

    confidence = str(confidence or "").strip().lower()
    if confidence not in spec["confidence_set"]:
        # Default DOWN, never up. An ungraded record is treated as the weakest thing it could be, so
        # a generator that forgets the field can only ever under-claim.
        dropped["confidence_defaulted_low"] = dropped.get("confidence_defaulted_low", 0) + 1
        confidence = "low"

    return {
        "repo": repo,
        "channel": channel,
        "basis": basis,
        "confidence": confidence,
        "citation": raw_citation,
        "path": path,
        "line": line,
        "note": str(note or "").strip(),
    }


def normalise(payload, spec):
    """Flatten any of the three accepted shapes into `{repo: [record, ...]}` plus drop counts.

    The shapes exist because the generator is the intranet's and a rename there must not need a
    release here:

      v1    {"<repo>": {"channels": [...], "basis": ..., "confidence": ..., "citation": ...}}
      v2    {"<repo>": {"channels": {"<channel>": [{"basis": ..., "citation": ...}, ...]}}}
      list  [{"repo": ..., "channel": ..., "basis": ..., "citation": ...}, ...]

    v1 carries ONE citation for a record that may name several channels, so the same citation is
    attached to each — that is a fidelity limit of the shape, not of this reader, and `shape` is
    returned so a caller can say so.
    """
    aliases = spec["field_aliases"]
    records = {}
    dropped = {}
    shape = "unknown"

    def add(repo, record):
        if record:
            records.setdefault(repo, []).append(record)

    if isinstance(payload, list):
        shape = "list"
        for entry in payload:
            if not isinstance(entry, dict):
                dropped["not_an_object"] = dropped.get("not_an_object", 0) + 1
                continue
            repo = str(_pick(entry, aliases, "repo") or "").strip()
            if not repo:
                dropped["no_repo"] = dropped.get("no_repo", 0) + 1
                continue
            for channel in _as_list(_pick(entry, aliases, "channels")):
                add(repo, _record(
                    repo, channel,
                    _pick(entry, aliases, "basis"), _pick(entry, aliases, "confidence"),
                    _pick(entry, aliases, "citation"), _pick(entry, aliases, "note"),
                    spec, dropped,
                ))
        return {"records": records, "dropped": dropped, "shape": shape}

    if not isinstance(payload, dict):
        return {"records": {}, "dropped": {"file_not_an_object": 1}, "shape": shape}

    for repo, entry in payload.items():
        repo = str(repo or "").strip()
        if not repo or repo.startswith("_"):
            continue
        if not isinstance(entry, dict):
            dropped["not_an_object"] = dropped.get("not_an_object", 0) + 1
            continue

        channels = _pick(entry, aliases, "channels")
        if isinstance(channels, dict):
            shape = "v2" if shape in ("unknown", "v2") else "mixed"
            for channel, items in channels.items():
                for item in _as_list(items):
                    item = item if isinstance(item, dict) else {}
                    add(repo, _record(
                        repo, channel,
                        _pick(item, aliases, "basis"), _pick(item, aliases, "confidence"),
                        _pick(item, aliases, "citation"), _pick(item, aliases, "note"),
                        spec, dropped,
                    ))
            continue

        shape = "v1" if shape in ("unknown", "v1") else "mixed"
        for channel in _as_list(channels):
            add(repo, _record(
                repo, channel,
                _pick(entry, aliases, "basis"), _pick(entry, aliases, "confidence"),
                _pick(entry, aliases, "citation"), _pick(entry, aliases, "note"),
                spec, dropped,
            ))

    return {"records": records, "dropped": dropped, "shape": shape}


def _spec(contract_payload):
    return {
        "channel_set": {str(c).strip().lower() for c in contract_payload.get("channels") or ()},
        "basis_set": {str(b).strip().lower() for b in contract_payload.get("basis") or ()},
        "confidence_set": {str(c).strip().lower() for c in contract_payload.get("confidence") or ()},
        "field_aliases": contract_payload.get("field_aliases") or {},
    }


def load(path=None, contract_path=None, include_notes=None):
    """Load the evidence file. Never raises; an absent file is an empty layer that says so.

    `readable` False and an empty `records` are different facts and are both reported: the first
    means write/re-save the file, the second means the generator found nothing.
    """
    payload_contract = contract(contract_path)
    spec = _spec(payload_contract)
    target = path or config.CHANNEL_EVIDENCE_JSON
    raw = _read_json(target)

    if raw is None:
        return {
            "path": target, "readable": False, "shape": "unknown",
            "records": {}, "repos": 0, "records_loaded": 0, "dropped": {}, "dropped_total": 0,
            "contract_path": contract_path or config.CHANNEL_EVIDENCE_CONTRACT_JSON,
        }

    result = normalise(raw, spec)
    if include_notes is None:
        include_notes = bool(payload_contract.get("include_notes_by_default"))
    if not include_notes:
        for items in result["records"].values():
            for item in items:
                item["note"] = ""

    loaded = sum(len(items) for items in result["records"].values())
    return {
        "path": target,
        "readable": True,
        "shape": result["shape"],
        "records": result["records"],
        "repos": len(result["records"]),
        "records_loaded": loaded,
        "dropped": result["dropped"],
        "dropped_total": sum(result["dropped"].values()),
        "contract_path": contract_path or config.CHANNEL_EVIDENCE_CONTRACT_JSON,
    }


def load_scope(path=None, contract_path=None):
    """What was LOOKED AT. Absent file -> `known: False`, which callers must not paper over."""
    payload_contract = contract(contract_path)
    aliases = payload_contract.get("scope_aliases") or {}
    target = path or config.CHANNEL_SCAN_SCOPE_JSON
    raw = _read_json(target)

    if not isinstance(raw, dict):
        return {
            "path": target, "known": False, "scanned": set(), "scanned_count": 0,
            "unresolved": {}, "unresolved_count": 0, "generated_at": "", "codegraph_manifest": "",
        }

    scanned = {str(name).strip() for name in _as_list(_pick(raw, aliases, "scanned"))
               if str(name).strip()}

    # `unresolved` = looked at, something matched, no citation could be produced (the intranet
    # reported 55). Accepts a bare list of names or a list of {repo, reason} objects, because the
    # reason is worth having and its absence must not reject the list.
    unresolved = {}
    for item in _as_list(_pick(raw, aliases, "unresolved")):
        if isinstance(item, dict):
            name = str(item.get("repo") or item.get("repository") or "").strip()
            reason = str(item.get("reason") or "").strip()
        else:
            name, reason = str(item or "").strip(), ""
        if name:
            unresolved[name] = reason

    return {
        "path": target,
        "known": bool(scanned),
        "scanned": scanned,
        "scanned_count": len(scanned),
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
        "generated_at": str(_pick(raw, aliases, "generated_at") or ""),
        "codegraph_manifest": str(_pick(raw, aliases, "codegraph_manifest") or ""),
    }


def verify(loaded):
    """Re-check every citation against the mirror. Stale line numbers are the expected failure.

    Reuses `retriever.citations`, which is the same verifier the assistant's own answers go through
    — a second implementation would be a second thing to keep true.

    A citation whose extension the verifier does not recognise yields NO items, and that is reported
    as `unverifiable`, never as verified. "Nothing was checked" reading as "everything passed" is
    the exact shape this module exists to refuse.
    """
    ok = stale = unverifiable = 0
    failures = []
    for repo, items in sorted((loaded.get("records") or {}).items()):
        for item in items:
            report = citations.verify(item["citation"])
            if not report["total"]:
                unverifiable += 1
                failures.append({"repo": repo, "channel": item["channel"],
                                 "citation": item["citation"], "state": "unverifiable",
                                 "reason": "no recognised source extension"})
            elif report["verified"] == report["total"]:
                ok += 1
            else:
                stale += 1
                reason = next((row["reason"] for row in report["items"] if not row["ok"]), "")
                failures.append({"repo": repo, "channel": item["channel"],
                                 "citation": item["citation"], "state": "stale",
                                 "reason": reason})
    return {
        "checked": ok + stale + unverifiable,
        "ok": ok, "stale": stale, "unverifiable": unverifiable,
        "failures": failures,
    }


def _rank_table(payload_contract):
    order = [str(name) for name in payload_contract.get("relation_order") or ()]
    if not order:
        order = list(DEFAULT_CONTRACT["relation_order"])
    return {name: index * 10 for index, name in enumerate(order)}, order


def _rank(relation, confidence, ranks, demote):
    """Position in the hierarchy; lower is stronger.

    A LOW-confidence direct hit is demoted to just below `message_carried`. A channel word in a
    class name is a weaker reason to name a channel than a topic that demonstrably carries it, and
    ranking it above one would let the weakest evidence lead the answer.
    """
    base = ranks.get(relation, 10 ** 6)
    if demote and confidence == "low" and relation in _DIRECT_EVIDENCE_RELATIONS:
        return ranks.get("message_carried", base) + 1
    return base


def for_repo(repo, tags=None, evidence=None, contract_path=None):
    """Every channel this repo relates to, ONE entry per channel, strongest evidence first.

    Merged per channel rather than per relation, because four rows all saying "SMS" is not four
    channels — it is one channel with four reasons. Each entry keeps every reason, so the UI can
    show the conclusion and the audit trail without recomputing either.

    `direct` distinguishes ownership from propagation: `transitive_dependency` means this repo would
    be AFFECTED, not that it sends on that channel, and the two have repeatedly been read as one.
    """
    payload_contract = contract(contract_path)
    ranks, _order = _rank_table(payload_contract)
    demote = bool(payload_contract.get("demote_low_confidence", True))

    tags = repo_tags.load() if tags is None else tags
    entry = repo_tags.for_repo(repo, tags)
    evidence = load(contract_path=contract_path) if evidence is None else evidence

    per_channel = {}

    def note(channel, relation, confidence, source=None):
        channel = str(channel or "").strip().lower()
        if not channel:
            return
        bucket = per_channel.setdefault(channel, {"channel": channel, "relations": [],
                                                  "evidence": []})
        bucket["relations"].append({
            "relation": relation,
            "confidence": confidence,
            "rank": _rank(relation, confidence, ranks, demote),
        })
        if source:
            bucket["evidence"].append(source)

    for item in (evidence.get("records") or {}).get(repo, ()):
        relation = _BASIS_RELATION.get(item["basis"], "business_declared")
        note(item["channel"], relation, item["confidence"], {
            "repo": item["repo"], "basis": item["basis"], "confidence": item["confidence"],
            "citation": item["citation"],
            **({"note": item["note"]} if item.get("note") else {}),
        })

    for channel in entry.get("channel") or ():
        note(channel, "name_derived", _STRUCTURAL)
    for channel in entry.get("channel_declared") or ():
        note(channel, "business_declared", _STRUCTURAL)
    for channel in entry.get("msg_channels") or ():
        note(channel, "message_carried", _STRUCTURAL)
    for channel in entry.get("serves_channels") or ():
        note(channel, "transitive_dependency", _DERIVED)

    out = []
    for channel, bucket in per_channel.items():
        relations = sorted(bucket["relations"], key=lambda row: (row["rank"], row["relation"]))
        best = relations[0]
        out.append({
            "channel": channel,
            "relation": best["relation"],
            "confidence": best["confidence"],
            "rank": best["rank"],
            "direct": best["relation"] in OWNERSHIP_RELATIONS,
            "relations": relations,
            "evidence": bucket["evidence"],
        })
    out.sort(key=lambda row: (row["rank"], row["channel"]))
    return out


def split_channels(view):
    """`{direct_channels, affected_channels}` from a `for_repo` view.

    Two questions that keep being answered with one list. `direct_channels` is what the repo itself
    handles; `affected_channels` is everything that could be hit, direct included — a superset, and
    labelled as such so nobody reports propagation as ownership.
    """
    direct = sorted({row["channel"] for row in view if row["direct"]})
    return {"direct_channels": direct,
            "affected_channels": sorted({row["channel"] for row in view})}


def coverage(tags=None, evidence=None, scope=None, contract_path=None):
    """Estate-wide accounting of what is known, in the states that have different remedies.

    Four buckets, and `channel_true_dark` is deliberately NOT one number any more:

      has_direct              some layer says this repo itself handles a channel
      relation_only           only propagation/message evidence — affected, ownership unknown
      unknown_scanned         looked at, nothing found: a real finding
      unknown_unscanned       out of the scan's scope by design: NOT a finding, and not a defect
      unknown_scope_unknown   no scope file, so 1 and 3 cannot be told apart

    `unresolved_uncitable` is reported separately again: those repos were looked at and something
    matched, but no citation could be produced. Folding them into "unknown" would report a sighting
    as a blank.
    """
    tags = repo_tags.load() if tags is None else tags
    evidence = load(contract_path=contract_path) if evidence is None else evidence
    scope = load_scope(contract_path=contract_path) if scope is None else scope

    records = evidence.get("records") or {}
    buckets = {"has_direct": 0, "relation_only": 0, "unknown_scanned": 0,
               "unknown_unscanned": 0, "unknown_scope_unknown": 0}
    from_evidence_only = 0

    for repo, entry in (tags or {}).items():
        structural_direct = bool(entry.get("channel") or entry.get("channel_declared"))
        has_evidence = bool(records.get(repo))
        if structural_direct or has_evidence:
            buckets["has_direct"] += 1
            if has_evidence and not structural_direct:
                from_evidence_only += 1
            continue
        if entry.get("serves_channels") or entry.get("msg_channels"):
            buckets["relation_only"] += 1
            continue
        if not scope.get("known"):
            buckets["unknown_scope_unknown"] += 1
        elif repo in scope["scanned"]:
            buckets["unknown_scanned"] += 1
        else:
            buckets["unknown_unscanned"] += 1

    return {
        "repos_measured": len(tags or {}),
        "scope_known": bool(scope.get("known")),
        "scanned_count": scope.get("scanned_count", 0),
        "unresolved_uncitable": scope.get("unresolved_count", 0),
        "evidence_readable": bool(evidence.get("readable")),
        "evidence_records": evidence.get("records_loaded", 0),
        "evidence_repos": evidence.get("repos", 0),
        "evidence_dropped": evidence.get("dropped") or {},
        "repos_explained_by_evidence_alone": from_evidence_only,
        **buckets,
    }


def conflicts(tags=None, evidence=None, contract_path=None):
    """Repos where the code says one channel and the name/sheet says a different one.

    Not an error, and not something to average away. The likeliest reading is that the name is
    historical and the code is current — but that is a judgement for whoever owns the repo, and it
    can only be made if the disagreement is visible. Silently unioning them hides a wrong tag behind
    a right one, and nobody ever goes back to look.

    Only DISJOINT sets count: evidence adding `email` to a repo whose name says `sms` is enrichment,
    not disagreement.
    """
    tags = repo_tags.load() if tags is None else tags
    evidence = load(contract_path=contract_path) if evidence is None else evidence
    records = evidence.get("records") or {}

    out = []
    for repo, items in sorted(records.items()):
        entry = repo_tags.for_repo(repo, tags)
        structural = {str(c).strip().lower() for c in
                      list(entry.get("channel") or ()) + list(entry.get("channel_declared") or ())}
        if not structural:
            continue
        evidenced = {item["channel"] for item in items}
        if evidenced and not (evidenced & structural):
            out.append({
                "repo": repo,
                "structural_channels": sorted(structural),
                "evidenced_channels": sorted(evidenced),
                "citations": sorted({item["citation"] for item in items}),
                "confidence": sorted({item["confidence"] for item in items}),
            })
    return out


def render_markdown(report):
    """The channel-evidence section of the refresh report."""
    lines = ["# Channel evidence", ""]
    cov, ev, ver = report["coverage"], report["evidence"], report["verification"]

    if not ev["readable"]:
        lines += [
            f"**No usable evidence file at `{ev['path']}`.** Either it does not exist, or it could "
            "not be decoded — it must be valid JSON saved as **UTF-8**. The four earlier channel "
            "layers are unaffected; this section is the only thing that goes dark.",
            "",
        ]
    if not cov["scope_known"]:
        lines += [
            "**No scan-scope file.** Without it a repo with no evidence could equally be *scanned "
            "and clean* or *never scanned*, so every such repo is counted as "
            "`unknown_scope_unknown` rather than guessed either way.",
            "",
        ]

    lines += [
        f"- evidence records loaded: **{ev['records_loaded']}** across **{ev['repos']}** repos "
        f"(shape `{ev['shape']}`)",
        f"- rejected: **{ev['dropped_total']}**"
        + (f" — {', '.join(f'{k}: {v}' for k, v in sorted(ev['dropped'].items()))}"
           if ev["dropped"] else ""),
        f"- citations re-checked against the mirror: **{ver['ok']} ok**, {ver['stale']} stale, "
        f"{ver['unverifiable']} unverifiable",
        "",
        "## Estate coverage",
        "",
        f"- repos measured: **{cov['repos_measured']}**",
        f"- has a DIRECT channel (name / sheet / code / config): **{cov['has_direct']}** "
        f"(of which **{cov['repos_explained_by_evidence_alone']}** are explained by this new "
        "evidence alone)",
        f"- affected only through propagation, ownership unknown: **{cov['relation_only']}**",
        f"- scanned, nothing found: **{cov['unknown_scanned']}**",
        f"- outside the scan scope by design: **{cov['unknown_unscanned']}** — this is not a gap "
        "in the data and must never be read as 'no channel'",
        f"- indistinguishable for want of a scope file: **{cov['unknown_scope_unknown']}**",
        f"- looked at, matched, could not be cited: **{cov['unresolved_uncitable']}**",
    ]

    if report["conflicts"]:
        lines += ["", "## Conflicts — code and name disagree", "",
                  "| repo | name/sheet says | evidence says | confidence |",
                  "| --- | --- | --- | --- |"]
        for row in report["conflicts"]:
            lines.append(
                f"| `{row['repo']}` | {', '.join(row['structural_channels'])} | "
                f"{', '.join(row['evidenced_channels'])} | {', '.join(row['confidence'])} |")

    if ver["failures"]:
        lines += ["", "## Citations that no longer resolve", "",
                  "| repo | channel | citation | state |", "| --- | --- | --- | --- |"]
        for row in ver["failures"][:100]:
            lines.append(f"| `{row['repo']}` | {row['channel']} | `{row['citation']}` | "
                         f"{row['state']}: {row['reason']} |")
    return "\n".join(lines) + "\n"


def build_report(tags=None, contract_path=None, verify_citations=True):
    """Everything the refresh step and the API envelope need, computed once."""
    evidence = load(contract_path=contract_path)
    scope = load_scope(contract_path=contract_path)
    tags = repo_tags.load() if tags is None else tags
    return {
        "evidence": {key: value for key, value in evidence.items() if key != "records"},
        "scope": {key: (sorted(value) if isinstance(value, set) else value)
                  for key, value in scope.items() if key != "scanned"},
        "coverage": coverage(tags=tags, evidence=evidence, scope=scope,
                             contract_path=contract_path),
        "conflicts": conflicts(tags=tags, evidence=evidence, contract_path=contract_path),
        "verification": (verify(evidence) if verify_citations else
                         {"checked": 0, "ok": 0, "stale": 0, "unverifiable": 0, "failures": [],
                          "skipped": True}),
        # Provenance travels with the numbers: "there is evidence" and "the evidence is current"
        # are different claims, and nothing here has been checked against production.
        "provenance": {
            "generated_at": scope.get("generated_at", ""),
            "codegraph_manifest": scope.get("codegraph_manifest", ""),
            "production_verified": False,
        },
    }


def write_report(index_dir, tags=None, contract_path=None, verify_citations=True):
    """Emit `<index_dir>/reports/CHANNEL_EVIDENCE.{md,json}`; returns the report."""
    report = build_report(tags=tags, contract_path=contract_path,
                          verify_citations=verify_citations)
    reports_dir = os.path.join(index_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    with open(os.path.join(reports_dir, "CHANNEL_EVIDENCE.md"), "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    with open(os.path.join(reports_dir, "CHANNEL_EVIDENCE.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return report

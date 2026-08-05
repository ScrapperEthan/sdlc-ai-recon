"""Glossary helpers for expanding box-local repo naming abbreviations.

`index/glossary.json` is HAND-AUTHORED on the intranet box and gitignored — nothing in this repo
generates it, and this repo never sees it. That makes two failure modes real rather than theoretical,
and both were observed in a live screenshot on 2026-08-05:

* **Unfilled entries render as if they were decoded.** The box's file carried `TBC ? ???????` for
  `mc`, `api` and `common`, so the impact view printed
  `mc-hk-hase-api-common (mc=TBC ? ???????, hk=HongKong, hase=HASESeng Bank, …)`. A reader cannot
  tell that from a real decoding — it is the same shape as reporting a log tail as a keyword hit, or
  treating an app's presence in a list as permission to read it: **absence dressed up as an answer.**
  So a placeholder value is DROPPED here (see `is_unfilled`); showing nothing is honest, showing
  `TBC ? ???????` is not.
* **A non-UTF-8 save used to crash the whole report.** `load()` caught FileNotFoundError/OSError/
  JSONDecodeError but not `UnicodeDecodeError`, which is what a GBK-saved file raises on a Chinese
  Windows box — and `impact_report.build_repo_report` calls `expand()` unconditionally, so one
  Notepad save could take out every impact answer. Unreadable now degrades to "no glossary", the
  same as absent.

`coverage()` exists because the box has ~259 tokens to fill by hand and no way to know which ones
matter. Ranking them by how many real repo names they appear in turns that into a short list.
"""
import json
import os
import re

from . import config

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Values that mean "nobody filled this in yet". Matched case-insensitively against the whole value.
_UNFILLED_WORDS = {
    "", "-", "--", "?", "??", "tbc", "tba", "tbd", "todo", "n/a", "na", "nil", "none", "null",
    "unknown", "pending", "fixme", "xxx", "待确认", "待补充", "待定", "未知", "未确认",
}

# Three or more consecutive question marks: the real text was lost on save (a non-Unicode code page
# replaces every CJK character with '?'). Such a value is not merely unfilled, it is CORRUPTED — and
# corrupted must not be rendered as a meaning either.
_QUESTION_RUN = re.compile(r"[?？]{3,}")
_TRIM = " \t-—–:：,，.。/|"


def is_unfilled(meaning):
    """True when this value carries no meaning: a placeholder word, or text destroyed on save.

    Deliberately conservative about the second case — a lone '?' inside otherwise real text (e.g.
    "settlement? see RUNBOOK-49") is kept, because that is a human annotation, not corruption.
    """
    text = (meaning or "").strip()
    if text.casefold() in _UNFILLED_WORDS:
        return True
    if _QUESTION_RUN.search(text):
        return True
    # "TBC ?" -> "TBC": strip stray question marks and punctuation, then re-test the remainder.
    remainder = text.replace("?", "").replace("？", "").strip(_TRIM)
    return remainder.casefold() in _UNFILLED_WORDS


def _resolve(path):
    if path == "index/glossary.json":
        return config.GLOSSARY_JSON
    return path


def _read(path):
    """Raw {token: meaning} exactly as authored, or None when the file is absent/unreadable.

    None and {} are different: None means "no usable file" (absent, unparseable, wrong encoding),
    {} means "a valid file that defines nothing". Only `coverage` needs to tell them apart.
    """
    try:
        with open(_resolve(path), encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        # ValueError covers BOTH json.JSONDecodeError and UnicodeDecodeError — the second is what a
        # GBK-saved file raises, and it used to propagate and break every impact report.
        return None
    return payload if isinstance(payload, dict) else None


def load_raw(path="index/glossary.json"):
    """Every authored entry, placeholders included. For reporting on the file, not for rendering."""
    payload = _read(path)
    if payload is None:
        return {}
    out = {}
    for key, value in payload.items():
        token = str(key).strip().lower()
        if token:
            out[token] = str(value).strip()
    return out


def load(path="index/glossary.json"):
    """Return a flat {token: meaning} map of USABLE entries, or {} when the file is absent.

    Placeholder/corrupted values are dropped rather than returned — a caller that renders whatever
    it gets back can then never print `mc=TBC ? ???????`.
    """
    return {token: meaning for token, meaning in load_raw(path).items()
            if meaning and not is_unfilled(meaning)}


def expand(name, path="index/glossary.json"):
    """Annotate known tokens in a repo-ish name; unchanged when nothing expands."""
    text = (name or "").strip()
    if not text:
        return text

    glossary = load(path)
    if not glossary:
        return text

    parts = []
    seen = set()
    for token in _TOKEN_RE.findall(text):
        key = token.lower()
        if key in glossary and key not in seen:
            parts.append(f"{token}={glossary[key]}")
            seen.add(key)

    if not parts:
        return text
    return f"{text} ({', '.join(parts)})"


# ---------------------------------------------------------------------------------------------
# Coverage — which tokens are worth the box's time to fill in
# ---------------------------------------------------------------------------------------------

def _repo_names(names=None):
    """The names to measure against: the caller's list, else every repo in repo_tags.

    Goes through `repo_tags.load()` rather than re-reading the file, so there is exactly one place
    that knows repo_tags.json's shape. An absent file yields {} there, i.e. an empty measurement
    rather than a crash.
    """
    if names is not None:
        return [str(name).strip() for name in names if str(name).strip()]
    from . import repo_tags
    return sorted(repo_tags.load())


def coverage(names=None, path="index/glossary.json"):
    """Every token that occurs in real repo names, ranked by how many names it appears in.

    state is one of:
      * `filled`      — a usable meaning is authored
      * `placeholder` — an entry EXISTS but says nothing (`TBC`, `???????`); this is the state that
                        used to render as though it were an answer
      * `missing`     — no entry at all

    `file_readable` is False when the file is absent OR could not be decoded, because the remedy
    differs (write it vs re-save it as UTF-8) and the two must not be reported as one.
    """
    repo_names = _repo_names(names)
    authored = load_raw(path)
    counts = {}
    for name in repo_names:
        for token in {tok.lower() for tok in _TOKEN_RE.findall(name)}:
            counts[token] = counts.get(token, 0) + 1

    tokens = []
    for token, count in counts.items():
        if token not in authored:
            state = "missing"
        elif is_unfilled(authored[token]) or not authored[token]:
            state = "placeholder"
        else:
            state = "filled"
        tokens.append({
            "token": token,
            "repos": count,
            "state": state,
            "authored": authored.get(token, ""),
        })
    tokens.sort(key=lambda item: (-item["repos"], item["token"]))

    totals = {"filled": 0, "placeholder": 0, "missing": 0}
    for item in tokens:
        totals[item["state"]] += 1

    usable = {token for token, meaning in authored.items()
              if meaning and not is_unfilled(meaning)}
    any_meaning = full_meaning = 0
    slots = decoded = 0
    for name in repo_names:
        name_tokens = [tok.lower() for tok in _TOKEN_RE.findall(name)]
        if not name_tokens:
            continue
        hits = [tok for tok in name_tokens if tok in usable]
        slots += len(name_tokens)
        decoded += len(hits)
        if hits:
            any_meaning += 1
        if len(hits) == len(name_tokens):
            full_meaning += 1

    return {
        "file_readable": _read(path) is not None,
        "path": _resolve(path),
        "repos_measured": len(repo_names),
        # MEASURED 2026-08-05 and found useless: this hit 459/460 before the box filled anything,
        # and stayed at 459/460 after 20 more tokens were filled. Almost every repo name contains
        # at least one common token, so "any" saturates immediately. Kept because a DROP here would
        # still be meaningful, but it must never be quoted as progress.
        "repos_with_any_meaning": any_meaning,
        # The two that actually move. `token_slots_decoded` is the honest completion rate: of every
        # token occurrence across every real repo name, how many can be explained. `repos_fully_
        # decoded` is the stricter one a reader feels — a name is only genuinely readable when it
        # has no unexplained parts left.
        "token_slots": slots,
        "token_slots_decoded": decoded,
        "repos_fully_decoded": full_meaning,
        "authored_entries": len(authored),
        "distinct_tokens": len(tokens),
        "totals": totals,
        "tokens": tokens,
    }


def render_coverage_markdown(report):
    """The fill list, worst-value-first, as something a human can work down."""
    lines = ["# Glossary coverage", ""]
    if not report["file_readable"]:
        lines += [
            f"**No usable glossary at `{report['path']}`.** Either the file does not exist, or it "
            "could not be decoded — it must be valid JSON saved as **UTF-8** (a Chinese-Windows "
            "ANSI/GBK save is unreadable here, and a save through a non-Unicode console silently "
            "replaces every CJK character with `?`).",
            "",
        ]
    slots = report["token_slots"] or 1
    lines += [
        f"- repo names measured: **{report['repos_measured']}**",
        f"- **token slots decoded: {report['token_slots_decoded']} / {report['token_slots']} "
        f"({100 * report['token_slots_decoded'] // slots}%)** — the number that moves as you fill",
        f"- **names with EVERY token decoded: {report['repos_fully_decoded']}** — the number a "
        "reader feels",
        f"- names with at least one token decoded: {report['repos_with_any_meaning']} "
        "(saturates almost immediately — do not read this one as progress)",
        f"- authored entries: **{report['authored_entries']}**",
        f"- tokens seen in real names: **{report['distinct_tokens']}** "
        f"(filled {report['totals']['filled']}, placeholder {report['totals']['placeholder']}, "
        f"missing {report['totals']['missing']})",
        "",
        "`placeholder` = an entry exists but says nothing (`TBC`, `???????`). These are dropped at "
        "render time rather than shown, so they now cost a blank instead of a fake decoding — but "
        "they still need filling.",
        "",
        "## Fill these first (ranked by how many repo names each token appears in)",
        "",
        "| token | repos | state | currently authored |",
        "| --- | ---: | --- | --- |",
    ]
    for item in report["tokens"]:
        if item["state"] == "filled":
            continue
        authored = item["authored"].replace("|", "\\|")
        lines.append(f"| `{item['token']}` | {item['repos']} | {item['state']} | {authored} |")
    if all(item["state"] == "filled" for item in report["tokens"]):
        lines.append("| — | — | every token seen in a repo name has a meaning | — |")
    return "\n".join(lines) + "\n"


def write_coverage(index_dir, names=None, path="index/glossary.json"):
    """Emit `<index_dir>/reports/GLOSSARY_COVERAGE.{md,json}`; returns the report."""
    report = coverage(names, path)
    reports_dir = os.path.join(index_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    with open(os.path.join(reports_dir, "GLOSSARY_COVERAGE.md"), "w", encoding="utf-8") as handle:
        handle.write(render_coverage_markdown(report))
    with open(os.path.join(reports_dir, "GLOSSARY_COVERAGE.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return report

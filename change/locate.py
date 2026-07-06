"""Resolve an intent target to a concrete service repo and controller."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from retriever import citations, code, config

from .add_endpoint import ControllerCandidate, _find_controllers
from .intent import ChangeRequest


AMBIGUITY_MARGIN = 2.0
TARGET_FILE = "TARGET_RESOLUTION.md"
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Candidate:
    repo: str
    score: float
    controller_path: str | None = None
    repomap_ref: str | None = None
    controller_ref: str | None = None
    matched_tokens: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class TargetResolution:
    repo: str
    controller_path: str
    candidates: list[Candidate]
    rationale: list[str] = field(default_factory=list)


class AmbiguousTarget(RuntimeError):
    def __init__(self, message: str, candidates: Iterable[Candidate] = ()):
        super().__init__(message)
        self.candidates = list(candidates)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "")}


def _repomap_path(mirror: str, index_dir: str | None = None) -> Path:
    if index_dir:
        return Path(index_dir) / "REPOMAP.md"

    env_index = os.environ.get("SDLC_INDEX")
    if env_index:
        return Path(env_index) / "REPOMAP.md"

    mirror_path = Path(mirror).resolve()
    sibling = mirror_path.parent / "index" / "REPOMAP.md"
    if sibling.exists():
        return sibling
    return Path("index") / "REPOMAP.md"


def _effective_index_dir(mirror: str, index_dir: str | None = None) -> str:
    return str(_repomap_path(mirror, index_dir).parent)


def _repo_from_line(line: str) -> str | None:
    match = re.search(r"\b([A-Za-z0-9_.-]+(?:-api|-service|-job|-core))\b", line)
    return match.group(1) if match else None


def _read_repomap(mirror: str, index_dir: str | None) -> dict[str, tuple[int, str]]:
    path = _repomap_path(mirror, index_dir)
    if not path.is_file():
        return {}

    entries: dict[str, tuple[int, str]] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        repo = _repo_from_line(line)
        if repo and repo not in entries:
            entries[repo] = (lineno, line.strip())
    return entries


def _repo_dirs(mirror: str) -> list[str]:
    root = Path(mirror)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))


def _controller_line(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                if "@RestController" in line:
                    return lineno
    except OSError:
        pass
    return 1


def _repo_rel(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


@contextmanager
def _retriever_roots(mirror: str, index_dir: str | None = None):
    old_mirror = config.MIRROR
    old_index = config.INDEX_DIR
    config.MIRROR = str(Path(mirror))
    if index_dir:
        config.INDEX_DIR = str(Path(index_dir))
    try:
        code._basename_index.cache_clear()
        citations._basename_index.cache_clear()
        yield
    finally:
        config.MIRROR = old_mirror
        config.INDEX_DIR = old_index
        code._basename_index.cache_clear()
        citations._basename_index.cache_clear()


def _repos_from_search(mirror: str, index_dir: str | None) -> set[str]:
    with _retriever_roots(mirror, index_dir):
        lines = code.search_code("@RestController", glob="*.java", max_results=500)
    repos: set[str] = set()
    mirror_root = Path(mirror).resolve()
    for line in lines:
        match = re.match(r"^(?P<path>.*):\d+:", line)
        if not match:
            continue
        path_text = match.group("path")
        try:
            rel_parts = Path(path_text).resolve().relative_to(mirror_root).parts
        except (OSError, ValueError):
            continue
        if rel_parts:
            repos.add(rel_parts[0])
    return repos


def _entry_text(repo: str, repomap: dict[str, tuple[int, str]]) -> str:
    return f"{repo} {repomap.get(repo, ('', ''))[1]}"


def _candidate_for_repo(
    repo: str,
    hint_tokens: set[str],
    mirror: str,
    repomap: dict[str, tuple[int, str]],
) -> Candidate | None:
    repo_root = Path(mirror) / repo
    controllers = _find_controllers(repo_root)
    if not controllers:
        return None

    selected = controllers[0]
    text = _entry_text(repo, repomap)
    entry_tokens = _tokens(text)
    matched = tuple(sorted(hint_tokens & entry_tokens))
    if not matched:
        return None

    score = float(len(matched) * 10)
    if selected.suitable:
        score += 3
    if re.search(r"\b(@RestController|controller|resource|entry[- ]?point)\b", text, re.IGNORECASE):
        score += 2
    if any(token in _tokens(repo) for token in matched):
        score += 2

    controller_path = _repo_rel(selected.path, repo_root)
    controller_ref = f"{repo}/{controller_path}:{_controller_line(selected.path)}"
    repomap_ref = None
    if repo in repomap:
        repomap_ref = f"index/REPOMAP.md:{repomap[repo][0]}"

    reason = f"matched hint token(s): {', '.join(matched)}"
    return Candidate(
        repo=repo,
        score=score,
        controller_path=controller_path,
        repomap_ref=repomap_ref,
        controller_ref=controller_ref,
        matched_tokens=matched,
        reason=reason,
    )


def _rank_candidates(request: ChangeRequest, mirror: str, index_dir: str | None) -> list[Candidate]:
    hint_tokens = _tokens(request.target_hint)
    if not hint_tokens:
        return []

    effective_index_dir = _effective_index_dir(mirror, index_dir)
    repomap = _read_repomap(mirror, effective_index_dir)
    repos = set(repomap) | _repos_from_search(mirror, effective_index_dir) | set(_repo_dirs(mirror))
    candidates = [
        candidate
        for repo in sorted(repos)
        if (candidate := _candidate_for_repo(repo, hint_tokens, mirror, repomap)) is not None
    ]
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.repo))


def _citation_report(sentence: str, mirror: str, index_dir: str | None) -> dict:
    with _retriever_roots(mirror, index_dir):
        return citations.verify(sentence)


def _citation_verified(sentence: str, mirror: str, index_dir: str | None) -> bool:
    report = _citation_report(sentence, mirror, index_dir)
    return report["total"] > 0 and report["verified"] == report["total"]


def _filter_verified_rationale(sentences: Iterable[str], mirror: str, index_dir: str | None = None) -> list[str]:
    return [sentence for sentence in sentences if _citation_verified(sentence, mirror, index_dir)]


def _build_rationale(candidate: Candidate, mirror: str, index_dir: str | None) -> list[str]:
    sentences = []
    if candidate.repomap_ref:
        sentences.append(
            f"REPOMAP ranks {candidate.repo} for the requested hint at {candidate.repomap_ref}."
        )
    if candidate.controller_ref:
        sentences.append(
            f"Controller selection is grounded in the existing RestController at {candidate.controller_ref}."
        )
    return _filter_verified_rationale(sentences, mirror, index_dir)


def resolve_target(
    request: ChangeRequest,
    mirror: str = "mirror",
    resolver: Callable[..., TargetResolution] | None = None,
    index_dir: str | None = None,
) -> TargetResolution:
    if resolver is not None:
        try:
            return resolver(request, mirror=mirror)
        except TypeError:
            return resolver(request, mirror)

    effective_index_dir = _effective_index_dir(mirror, index_dir)
    candidates = _rank_candidates(request, mirror, index_dir)
    if not candidates:
        raise AmbiguousTarget("no target candidates matched the hint", candidates)

    top = candidates[0]
    if len(candidates) > 1 and top.score - candidates[1].score < AMBIGUITY_MARGIN:
        raise AmbiguousTarget("target is ambiguous; top candidates are too close", candidates)

    if not top.controller_path:
        raise AmbiguousTarget("target has no concrete controller", candidates)

    return TargetResolution(
        repo=top.repo,
        controller_path=top.controller_path,
        candidates=candidates,
        rationale=_build_rationale(top, mirror, effective_index_dir),
    )


def format_target_resolution(
    request: ChangeRequest,
    resolution: TargetResolution | None = None,
    candidates: Iterable[Candidate] = (),
    error: str | None = None,
) -> str:
    status = "REFUSED" if error else "RESOLVED"
    ranked = list(resolution.candidates if resolution else candidates)
    lines = [
        "# Target Resolution",
        "",
        f"Status: {status}",
        f"Kind: {request.kind}",
        f"Endpoint path: {request.path}",
        f"Target hint: {request.target_hint}",
        "",
    ]

    if error:
        lines.extend(["Reason:", f"- {error}", ""])
    if resolution:
        lines.extend(
            [
                "Chosen target:",
                f"- Repo: {resolution.repo}",
                f"- Controller: {resolution.controller_path}",
                "",
            ]
        )

    lines.extend(["Ranked candidates:"])
    if ranked:
        for candidate in ranked:
            controller = candidate.controller_path or "<none>"
            lines.append(
                f"- {candidate.repo} score={candidate.score:.1f} controller={controller} {candidate.reason}".rstrip()
            )
    else:
        lines.append("- <none>")
    lines.append("")

    lines.extend(["Cited rationale:"])
    if resolution and resolution.rationale:
        lines.extend(f"- {sentence}" for sentence in resolution.rationale)
    else:
        lines.append("- <none>")
    lines.append("")
    return "\n".join(lines)


def write_target_resolution(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

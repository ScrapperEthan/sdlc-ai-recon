"""Derive golden-template scaffold conventions from the read-only mirror."""
from __future__ import annotations

import copy
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


DEFAULTS = {
    "parent": {
        "groupId": "com.hsbc.hase",
        "artifactId": "mc-hk-hase-api-parent",
        "version": "<CONFIRM>",
    },
    "starter": {
        "groupId": "com.hsbc.hase",
        "artifactId": "mc-hk-hase-api-starter",
    },
    "base_package": "com.hsbc.hase",
}

PARENT_REPO = "mc-hk-hase-api-parent"
STARTER_REPO = "mc-hk-hase-api-starter"
DEFAULT_REFERENCE_REPO = "mc-hk-hase-ingress-api"


@dataclass(frozen=True)
class TemplateDetails:
    template: dict
    from_mirror: bool
    citations: dict = field(default_factory=dict)
    build_stanza: str | None = None
    build_citation: str | None = None
    notes: tuple[str, ...] = ()


def _clone_defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def _repo_name(value: str) -> str:
    if not value or os.path.isabs(value) or "/" in value or "\\" in value or ".." in value:
        raise ValueError("reference must be a mirror repo name")
    return value


def validate_reference(value: str) -> str:
    return _repo_name(value)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(root: ET.Element | None, tag: str) -> ET.Element | None:
    if root is None:
        return None
    for child in root:
        if _local_name(child.tag) == tag:
            return child
    return None


def _find(root: ET.Element | None, tag: str) -> str | None:
    child = _child(root, tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _coords(pom_path: str) -> dict:
    root = ET.parse(pom_path).getroot()
    parent = _child(root, "parent")
    return {
        "groupId": _find(root, "groupId") or _find(parent, "groupId"),
        "artifactId": _find(root, "artifactId"),
        "version": _find(root, "version") or _find(parent, "version"),
    }


def _line_with(pom_path: str, tag: str, expected: str | None = None) -> int:
    pattern = re.compile(rf"<(?:\w+:)?{re.escape(tag)}\b")
    try:
        with open(pom_path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                if pattern.search(line) and (expected is None or expected in line):
                    return lineno
    except OSError:
        pass
    return 1


def _repo_ref(mirror: str, path: str, line: int) -> str:
    rel = os.path.relpath(path, mirror).replace(os.sep, "/")
    return f"{rel}:{line}"


def _fallback(message: str) -> TemplateDetails:
    print(f"NOTE: {message}; using documented defaults -- internal Codex must confirm.")
    return TemplateDetails(
        template=_clone_defaults(),
        from_mirror=False,
        notes=(message,),
    )


def _extract_build_stanza(pom_path: str, mirror: str) -> tuple[str | None, str | None]:
    try:
        with open(pom_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, None

    match = re.search(r"(?ms)^([ \t]*<(?:\w+:)?build\b.*?</(?:\w+:)?build>)", text)
    if match is None or "spring-boot-maven-plugin" not in match.group(1):
        return None, None

    start_line = text[: match.start(1)].count("\n") + 1
    stanza = match.group(1).rstrip()
    return stanza, _repo_ref(mirror, pom_path, start_line)


def _package_from_java(path: str) -> tuple[str, int] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                match = re.match(r"\s*package\s+([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\s*;", line)
                if match:
                    return match.group(1), lineno
    except OSError:
        return None
    return None


def _detect_base_package(mirror: str, reference: str) -> tuple[str, str | None]:
    reference_root = os.path.join(mirror, reference, "src", "main", "java")
    if not os.path.isdir(reference_root):
        return DEFAULTS["base_package"], None

    packages: list[tuple[str, str, int]] = []
    for dirpath, dirnames, filenames in os.walk(reference_root):
        dirnames[:] = [name for name in dirnames if name not in {"target", "build", ".git"}]
        for name in sorted(filenames):
            if not name.endswith(".java"):
                continue
            path = os.path.join(dirpath, name)
            package_result = _package_from_java(path)
            if package_result:
                package_name, line = package_result
                packages.append((package_name, path, line))

    if not packages:
        return DEFAULTS["base_package"], None

    for package_name, path, line in packages:
        if package_name == DEFAULTS["base_package"] or package_name.startswith(DEFAULTS["base_package"] + "."):
            return DEFAULTS["base_package"], _repo_ref(mirror, path, line)

    split_packages = [package_name.split(".") for package_name, _, _ in packages]
    common = split_packages[0]
    for parts in split_packages[1:]:
        keep = 0
        for left, right in zip(common, parts):
            if left != right:
                break
            keep += 1
        common = common[:keep]
    base_package = ".".join(common) if common else DEFAULTS["base_package"]
    return base_package, _repo_ref(mirror, packages[0][1], packages[0][2])


def load_details(mirror: str = "mirror", reference: str = DEFAULT_REFERENCE_REPO) -> TemplateDetails:
    reference = _repo_name(reference)
    parent_pom = os.path.join(mirror, PARENT_REPO, "pom.xml")
    starter_pom = os.path.join(mirror, STARTER_REPO, "pom.xml")
    if not os.path.exists(parent_pom):
        return _fallback("mirror anchor not found")
    if not os.path.exists(starter_pom):
        return _fallback("starter anchor not found")

    parent = _coords(parent_pom)
    starter = _coords(starter_pom)
    base_package, base_package_ref = _detect_base_package(mirror, reference)

    template = {
        "parent": parent,
        "starter": starter,
        "base_package": base_package,
    }

    citations = {
        "parent": _repo_ref(
            mirror,
            parent_pom,
            _line_with(parent_pom, "artifactId", parent.get("artifactId")),
        ),
        "starter": _repo_ref(
            mirror,
            starter_pom,
            _line_with(starter_pom, "artifactId", starter.get("artifactId")),
        ),
    }
    if base_package_ref:
        citations["base_package"] = base_package_ref

    reference_pom = os.path.join(mirror, reference, "pom.xml")
    build_stanza, build_citation = _extract_build_stanza(reference_pom, mirror)
    return TemplateDetails(
        template=template,
        from_mirror=True,
        citations=citations,
        build_stanza=build_stanza,
        build_citation=build_citation,
    )


def load_template(mirror: str = "mirror", reference: str = DEFAULT_REFERENCE_REPO) -> tuple[dict, bool]:
    details = load_details(mirror=mirror, reference=reference)
    return details.template, details.from_mirror

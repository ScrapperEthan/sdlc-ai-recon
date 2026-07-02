"""Derive golden-template scaffold conventions from the read-only mirror."""
from __future__ import annotations

import copy
import json
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
PLATFORM_FILE_PATHS = (
    "sonar-project.properties",
    "SHP/AppConfigFiles/app.yaml",
    "SHP/AppConfigSchema.yaml",
    "SHP/DeployConfigSchema.yaml",
    ".gitignore",
)

# When copying a reference repo's platform/API-metadata files, the reference
# service's own governance / environment / account values must NOT be inherited
# by the new service (a governance and correctness issue — see
# docs/specs/scaffolding-p2-sanitize.md). Blank the value of any leaf key below
# (case-insensitive) and any absolute URL to this placeholder. Easy to extend.
SANITIZE_PLACEHOLDER = "<REVIEW>"
SANITIZE_KEYS = frozenset(
    {
        # sonar branch policy
        "sonar.branch.name",
        "sonar.newcode.referencebranch",
        # SHP app config account/org/team
        "sonaraccountid",
        "serviceaccountid",
        "nexusiqorgname",
        "checkmarxteampath",
        # api.meta ownership / account / team (JSON-shaped descriptor)
        "applicationid",
        "serviceline",
        "teamname",
        "teamemail",
        "teamemailaddress",
        "supportgroup",
        "supportcontact",
        "costcenter",
        "owner",
        "ownership",
        "account",
        # RAML / environment
        "baseuri",
        "environment",
        "environmentlink",
    }
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")


def _value_is_sensitive(value: str) -> bool:
    """A value whose shape (URL or email) marks it inherited/environment-specific."""
    return bool(_URL_RE.search(value) or _EMAIL_RE.search(value))
_PROP_LINE_RE = re.compile(r"^(?P<prefix>\s*(?P<key>[\w.\-]+)\s*=\s*)(?P<value>.+)$")
_YAML_LINE_RE = re.compile(r"^(?P<prefix>\s*(?:-\s*)?(?P<key>[\w.\-]+)\s*:\s*)(?P<value>.+)$")


@dataclass(frozen=True)
class TemplateDetails:
    template: dict
    from_mirror: bool
    citations: dict = field(default_factory=dict)
    build_stanza: str | None = None
    build_citation: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceFile:
    rel_path: str
    text: str
    citation: str
    sanitized: tuple = ()


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


def _repo_ref(mirror: str, path: str, line: int = 1) -> str:
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


def _reference_artifact_id(mirror: str, reference: str) -> str:
    pom_path = os.path.join(mirror, reference, "pom.xml")
    if os.path.exists(pom_path):
        try:
            artifact_id = _coords(pom_path).get("artifactId")
            if artifact_id:
                return artifact_id
        except ET.ParseError:
            pass
    return reference


def _reference_short_name(reference: str, artifact_id: str | None = None) -> str:
    value = artifact_id or reference
    candidates = [value]
    if reference != value:
        candidates.append(reference)

    for candidate in candidates:
        name = candidate
        if name.startswith("mc-hk-hase-"):
            name = name[len("mc-hk-hase-") :]
        if name.startswith("api-") and name.endswith("-core"):
            name = name[len("api-") : -len("-core")]
        elif name.endswith("-api"):
            name = name[: -len("-api")]
        elif name.startswith("svc-") and name.endswith("-job"):
            name = name[len("svc-") : -len("-job")]
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
        if name:
            return name

    return "service"


def _derive_base_namespace(package_name: str, reference_short: str) -> str | None:
    parts = package_name.split(".")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() == reference_short.lower():
            base_parts = parts[:index]
            return ".".join(base_parts) if base_parts else None
    return None


def _detect_reference_namespace(mirror: str, reference: str) -> tuple[str, str | None, str | None, str]:
    reference_root = os.path.join(mirror, reference, "src", "main", "java")
    artifact_id = _reference_artifact_id(mirror, reference)
    reference_short = _reference_short_name(reference, artifact_id)
    if not os.path.isdir(reference_root):
        return DEFAULTS["base_package"], None, None, reference_short

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
        return DEFAULTS["base_package"], None, None, reference_short

    packages.sort(
        key=lambda item: (
            item[0].split(".")[-1].lower() != reference_short.lower(),
            not os.path.basename(item[1]).endswith("Application.java"),
            item[0],
        )
    )

    for package_name, path, line in packages:
        base_namespace = _derive_base_namespace(package_name, reference_short)
        if base_namespace:
            return base_namespace, _repo_ref(mirror, path, line), package_name, reference_short

    return DEFAULTS["base_package"], _repo_ref(mirror, packages[0][1], packages[0][2]), packages[0][0], reference_short


def _transform_reference_text(text: str, reference: str, details: TemplateDetails, slug: str, package: str) -> str:
    reference_package = details.template.get("reference_package")
    reference_short = details.template.get("reference_short_name") or _reference_short_name(reference)
    class_stem = "".join(part[:1].upper() + part[1:] for part in re.split(r"[^a-zA-Z0-9]+", slug) if part)
    reference_class_stem = reference_short[:1].upper() + reference_short[1:]
    replacements = []
    if reference_package:
        replacements.extend(
            [
                (reference_package.replace(".", "/"), package.replace(".", "/")),
                (reference_package, package),
            ]
        )
    replacements.extend(
        [
            (reference, slug),
            (reference_short.upper(), slug.upper()),
            (reference_class_stem, class_stem or slug),
            (reference_short, slug),
        ]
    )

    transformed = text
    for old, new in sorted(((old, new) for old, new in replacements if old), key=lambda pair: len(pair[0]), reverse=True):
        transformed = transformed.replace(old, new)
    return transformed


def _is_sensitive_key(key: str) -> bool:
    return key.strip().lower() in SANITIZE_KEYS


# api.meta is a per-service governance/ownership descriptor where essentially every
# value is org/business/account metadata the new service must supply itself. Rather
# than chase individual keys, `aggressive` mode blanks EVERY string value except the
# service's own identity (already rewritten to the new name).
_META_IDENTITY_KEYS = frozenset({"service", "servicename", "name", "apiname", "artifactid"})


def _sanitize_json(text: str, aggressive: bool = False) -> tuple[str, list[str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return text, []
    changed: list[str] = []

    def should_blank(key: str, value) -> bool:
        if aggressive:
            return isinstance(value, str) and key.strip().lower() not in _META_IDENTITY_KEYS
        if _is_sensitive_key(key):
            return True
        return isinstance(value, str) and _value_is_sensitive(value)

    def walk(node):
        if isinstance(node, dict):
            for key in list(node):
                value = node[key]
                if isinstance(value, (dict, list)):
                    walk(value)
                elif should_blank(key, value):
                    node[key] = SANITIZE_PLACEHOLDER
                    changed.append(key)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not changed:
        return text, []
    return json.dumps(data, indent=2) + "\n", changed


def _sanitize_lines(text: str, pattern: "re.Pattern[str]") -> tuple[str, list[str]]:
    changed: list[str] = []
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        match = pattern.match(body)
        if match and (_is_sensitive_key(match.group("key")) or _value_is_sensitive(match.group("value"))):
            newline = line[len(body):]
            out.append(f"{match.group('prefix')}{SANITIZE_PLACEHOLDER}{newline}")
            changed.append(match.group("key"))
            continue
        out.append(line)
    return "".join(out), changed


def _sanitize_reference_text(text: str, rel_path: str) -> tuple[str, list[str]]:
    """Blank inherited governance/environment values (URLs, emails) in a copied file."""
    lower = rel_path.lower()
    # api.meta is a JSON governance descriptor (despite the .meta extension): blank ALL
    # its org/business values, keeping only the service identity. doc-properties.json and
    # other JSON use the denylist + URL/email rules.
    if lower.endswith(".meta"):
        try:
            json.loads(text)
            return _sanitize_json(text, aggressive=True)
        except ValueError:
            pass
    elif lower.endswith(".json"):
        return _sanitize_json(text, aggressive=False)
    pattern = _PROP_LINE_RE if lower.endswith(".properties") else _YAML_LINE_RE
    sanitized, changed = _sanitize_lines(text, pattern)
    # Catch-all: any URL / email a key:value line pass missed (e.g. bare list items).
    sanitized, url_hits = _URL_RE.subn(SANITIZE_PLACEHOLDER, sanitized)
    sanitized, email_hits = _EMAIL_RE.subn(SANITIZE_PLACEHOLDER, sanitized)
    if url_hits or email_hits:
        changed = changed + ["<url/email>"]
    return sanitized, changed


def _load_reference_files(
    mirror: str,
    reference: str,
    slug: str,
    package: str,
    rel_paths: tuple[str, ...],
    details: TemplateDetails,
) -> list[ReferenceFile]:
    if not details.from_mirror:
        return []

    loaded: list[ReferenceFile] = []
    reference_root = os.path.join(mirror, reference)
    for rel_path in rel_paths:
        normalized = rel_path.replace("/", os.sep)
        source_path = os.path.join(reference_root, normalized)
        if not os.path.isfile(source_path):
            continue
        with open(source_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        output_rel = _transform_reference_text(rel_path, reference, details, slug, package)
        output_text = _transform_reference_text(text, reference, details, slug, package)
        output_text, sanitized = _sanitize_reference_text(output_text, output_rel)
        loaded.append(
            ReferenceFile(output_rel, output_text, _repo_ref(mirror, source_path), tuple(sanitized))
        )
    return loaded


def load_platform_files(
    mirror: str,
    reference: str,
    slug: str,
    package: str,
    details: TemplateDetails,
) -> list[ReferenceFile]:
    """Read and rewrite the reference repo's platform/config files."""
    return _load_reference_files(mirror, reference, slug, package, PLATFORM_FILE_PATHS, details)


def load_api_files(
    mirror: str,
    reference: str,
    slug: str,
    package: str,
    details: TemplateDetails,
) -> list[ReferenceFile]:
    """Read and rewrite contract files under the reference repo's src/main/api."""
    if not details.from_mirror:
        return []

    api_root = os.path.join(mirror, reference, "src", "main", "api")
    if not os.path.isdir(api_root):
        return []

    rel_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(api_root):
        dirnames[:] = [name for name in dirnames if name not in {"target", "build", ".git"}]
        for filename in sorted(filenames):
            source_path = os.path.join(dirpath, filename)
            rel_paths.append(os.path.relpath(source_path, os.path.join(mirror, reference)).replace(os.sep, "/"))
    return _load_reference_files(mirror, reference, slug, package, tuple(rel_paths), details)


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
    base_namespace, base_namespace_ref, reference_package, reference_short = _detect_reference_namespace(mirror, reference)

    template = {
        "parent": parent,
        "starter": starter,
        "base_package": base_namespace,
        "base_namespace": base_namespace,
        "reference_package": reference_package,
        "reference_short_name": reference_short,
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
    if base_namespace_ref:
        citations["base_package"] = base_namespace_ref
        citations["base_namespace"] = base_namespace_ref

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

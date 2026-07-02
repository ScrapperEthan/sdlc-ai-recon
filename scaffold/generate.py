#!/usr/bin/env python3
"""Generate a scratch Spring-style service skeleton for human review."""
import argparse
import os
import re
import shutil
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .reference import (
    DEFAULT_REFERENCE_REPO,
    SANITIZE_PLACEHOLDER,
    load_api_files,
    load_details,
    load_platform_files,
    validate_reference,
)


def _slug(value):
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError("service name must be a single path segment")
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    if not slug:
        raise ValueError("service name is required")
    return slug


def _class_name(service_name):
    parts = re.split(r"[^a-zA-Z0-9]+", service_name)
    name = "".join(part[:1].upper() + part[1:] for part in parts if part)
    return name or "GeneratedService"


def _package_path(package):
    if not re.match(r"^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*$", package):
        raise ValueError(f"invalid Java package: {package}")
    return os.path.join(*package.split("."))


def _package_segment(slug):
    segment = re.sub(r"[^a-zA-Z0-9_]+", "_", slug).strip("_").lower()
    if not segment:
        raise ValueError("service name does not contain a valid Java package segment")
    if segment[0].isdigit():
        segment = "_" + segment
    return segment


def _resolve_package(package, slug, details):
    if package:
        _package_path(package)
        return package, "override"
    if not details.from_mirror:
        raise ValueError("--package is required when the mirror is absent")
    if "base_namespace" not in details.citations:
        raise ValueError("reference service did not expose a cited base namespace")
    base_namespace = details.template.get("base_namespace") or details.template.get("base_package")
    if not base_namespace:
        raise ValueError("reference service did not expose a derivable base namespace")
    _package_path(base_namespace)
    return f"{base_namespace}.{_package_segment(slug)}", "derived"


def _inside(root, path):
    try:
        return os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) == os.path.abspath(root)
    except ValueError:
        return False


def _same_or_inside(root, path):
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        return os.path.commonpath([root_abs, path_abs]) == root_abs
    except ValueError:
        return False


def _safe_join(root, *parts):
    target = os.path.abspath(os.path.join(root, *parts))
    if not _inside(root, target):
        raise ValueError("generated path escapes output root")
    return target


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _xml(value):
    return escape(str(value or ""))


def _pom_xml(slug, details):
    parent = details.template["parent"]
    starter = details.template["starter"]
    build = f"\n{details.build_stanza}\n" if details.build_stanza else "\n"
    return f"""<project xmlns=\"http://maven.apache.org/POM/4.0.0\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
  xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>{_xml(parent.get("groupId"))}</groupId>
    <artifactId>{_xml(parent.get("artifactId"))}</artifactId>
    <version>{_xml(parent.get("version"))}</version>
  </parent>
  <artifactId>{_xml(slug)}</artifactId>
  <version>0.1.0-SNAPSHOT</version>
  <packaging>jar</packaging>
  <dependencies>
    <dependency>
      <groupId>{_xml(starter.get("groupId"))}</groupId>
      <artifactId>{_xml(starter.get("artifactId"))}</artifactId>
    </dependency>
  </dependencies>{build}</project>
"""


def _review_diff(slug, files, package, reference, details, directories, file_sources, package_source, api_note, sanitized_fields):
    listed_files = sorted([*files, "REVIEW_DIFF.md"])
    listed_dirs = sorted(directories)
    lines = [
        f"# Review Diff for {slug}",
        "",
        "Generated files:",
        *[f"- {rel}" for rel in listed_files],
        "",
        "Generated directories:",
        *[f"- {rel}/" for rel in listed_dirs],
        "",
        "Template evidence:",
    ]
    if details.from_mirror:
        lines.extend(
            [
                f"- Parent POM coordinates: {details.citations.get('parent', 'mirror anchor missing')}",
                f"- Starter dependency coordinates: {details.citations.get('starter', 'mirror anchor missing')}",
                f"- Base namespace convention: {details.citations.get('base_namespace', 'reference package citation missing')}",
            ]
        )
        if package_source == "derived":
            lines.append(f"- Generated package: {package} (derived from the base namespace above)")
        else:
            lines.append(f"- Generated package: {package} (explicit --package override)")
        if details.build_citation:
            lines.append(f"- Spring Boot build stanza copied from: {details.build_citation}")
    else:
        lines.extend(
            [
                "- Mirror anchors unavailable; used documented fallback defaults from scaffold/reference.py.",
                "- Internal Codex must confirm parent/starter coordinates against the real mirror before use.",
                "- SHP and sonar platform files are generated only when the reference mirror is available.",
            ]
        )

    if file_sources:
        lines.extend(["", "Reference-derived files:"])
        for rel, citation in sorted(file_sources.items()):
            lines.append(f"- {rel}: {citation}")

    if sanitized_fields:
        lines.extend(["", "Sanitized (fill in before use):"])
        for rel, keys in sorted(sanitized_fields.items()):
            named = sorted({k for k in keys if k != "<url>"})
            shown = ", ".join(named)
            if any(k == "<url>" for k in keys):
                shown = f"{shown}, URL value(s)" if shown else "URL value(s)"
            lines.append(f"- {rel}: {shown} -> {SANITIZE_PLACEHOLDER}")

    if api_note:
        lines.extend(["", "API contract layout:", f"- {api_note}"])

    lines.extend(
        [
            "",
            "Manual review checklist:",
            "- Confirm the generated parent POM coordinates against mc-hk-hase-api-parent.",
            "- Confirm the generated starter dependency against mc-hk-hase-api-starter.",
            f"- Confirm package {package} matches the target domain and the {reference} convention.",
            "- Confirm the service is starter-only and does not declare a dedicated *-core dependency.",
            "- Confirm SHP/sonar values contain no secrets or environment-specific values before porting.",
            f"- Fill in every {SANITIZE_PLACEHOLDER} placeholder (see 'Sanitized' above) with this service's own values.",
            "- Replace sample listener queue and payload with reviewed message contracts.",
            "- Keep this output in scratch/ until a human explicitly ports it to a production repo.",
        ]
    )
    return "\n".join(lines) + "\n"


def generate_service(
    service_name,
    package=None,
    out_dir="scratch",
    force=False,
    reference=DEFAULT_REFERENCE_REPO,
    mirror="mirror",
    with_core=False,
):
    if with_core:
        raise ValueError("--with-core is explicitly deferred; Phase 2 keeps generated services starter-only")
    slug = _slug(service_name)
    class_name = _class_name(slug)
    out_root = os.path.abspath(out_dir)
    reference = validate_reference(reference)
    mirror_root = os.path.abspath(mirror)
    if _same_or_inside(mirror_root, out_root):
        raise ValueError("output root must not be inside the read-only mirror")
    details = load_details(mirror=mirror, reference=reference)
    package, package_source = _resolve_package(package, slug, details)
    target = _safe_join(out_root, slug)
    package_dir = _package_path(package)
    if os.path.exists(target):
        if not force:
            raise FileExistsError(f"{target} already exists; pass --force to replace it")
        shutil.rmtree(target)

    directories = {
        os.path.join("src", "main", "api"),
        os.path.join("src", "test", "java", package_dir),
    }
    file_sources = {}
    sanitized_fields = {}
    readme_note = ""
    if not details.from_mirror:
        readme_note = (
            "\nNOTE: SHP and sonar platform files are generated only when the reference mirror is available. "
            "Re-run on the internal mirror box to derive them.\n"
        )
    files = {
        "pom.xml": _pom_xml(slug, details),
        os.path.join("src", "main", "resources", "application.yml"): f"""spring:
  application:
    name: {slug}

management:
  endpoints:
    web:
      exposure:
        include: health,info
""",
        "README.md": f"""# {slug}

Scratch scaffold generated at {_timestamp()} for human review.

This output is intentionally outside production repos. Review package names,
parent POM conventions, headers/interceptors, and messaging bindings before use.
{readme_note}
""",
        os.path.join("src", "main", "java", package_dir, f"{class_name}Application.java"): f"""package {package};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class {class_name}Application {{
    public static void main(String[] args) {{
        SpringApplication.run({class_name}Application.class, args);
    }}
}}
""",
        os.path.join("src", "main", "java", package_dir, "resource", "HealthResource.java"): f"""package {package}.resource;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping(\"/internal\")
public class HealthResource {{
    @GetMapping(\"/health\")
    public String health() {{
        return \"OK\";
    }}
}}
""",
        os.path.join("src", "main", "java", package_dir, "listener", "SampleMessageListener.java"): f"""package {package}.listener;

import org.springframework.jms.annotation.JmsListener;
import org.springframework.stereotype.Component;

@Component
public class SampleMessageListener {{
    @JmsListener(destination = \"${{app.listener.sample.queue:sample-queue}}\")
    public void onMessage(String payload) {{
        // TODO: replace with the reviewed domain message contract.
    }}
}}
""",
        os.path.join("src", "test", "java", package_dir, f"{class_name}ApplicationTest.java"): f"""package {package};

import org.junit.jupiter.api.Test;

class {class_name}ApplicationTest {{
    @Test
    void contextLoads() {{
    }}
}}
""",
    }

    if details.from_mirror:
        for reference_file in load_platform_files(mirror, reference, slug, package, details):
            files[reference_file.rel_path] = reference_file.text
            file_sources[reference_file.rel_path] = reference_file.citation
            if reference_file.sanitized:
                sanitized_fields[reference_file.rel_path] = list(reference_file.sanitized)
        api_files = load_api_files(mirror, reference, slug, package, details)
        for reference_file in api_files:
            files[reference_file.rel_path] = reference_file.text
            file_sources[reference_file.rel_path] = reference_file.citation
            if reference_file.sanitized:
                sanitized_fields[reference_file.rel_path] = list(reference_file.sanitized)
        if api_files:
            api_note = "src/main/api contract files were derived from the reference repo files listed above."
        else:
            api_note = "src/main/api/ was created empty because the reference repo did not expose contract files."
    else:
        files[".gitignore"] = """target/
*.class
.classpath
.project
.settings/
.idea/
*.iml
"""
        api_note = "src/main/api/ was created empty; contract stubs require the reference mirror."

    files["REVIEW_DIFF.md"] = _review_diff(
        slug,
        files,
        package,
        reference,
        details,
        directories,
        file_sources,
        package_source,
        api_note,
        sanitized_fields,
    )

    for rel in directories:
        os.makedirs(_safe_join(target, rel), exist_ok=True)
    for rel, text in files.items():
        _write(_safe_join(target, rel), text)

    return {"path": target, "files": sorted(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a scratch service scaffold.")
    parser.add_argument("service_name")
    parser.add_argument("--package", help="base Java package, e.g. com.hsbc.hase.example")
    parser.add_argument("--out-dir", default="scratch")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reference", default=DEFAULT_REFERENCE_REPO)
    parser.add_argument("--with-core", action="store_true", help="reserved for Phase 2; currently rejected")
    args = parser.parse_args(argv)
    if args.with_core:
        parser.error("--with-core is explicitly deferred; Phase 2 keeps generated services starter-only")

    result = generate_service(
        args.service_name,
        args.package,
        args.out_dir,
        args.force,
        reference=args.reference,
        with_core=args.with_core,
    )
    print(f"wrote {result['path']}")
    for rel in result["files"]:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

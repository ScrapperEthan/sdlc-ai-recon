#!/usr/bin/env python3
"""Generate a scratch Spring-style service skeleton for human review."""
import argparse
import os
import re
import shutil
from datetime import datetime, timezone


def _slug(value):
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


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def generate_service(service_name, package, out_dir="scratch", force=False):
    slug = _slug(service_name)
    class_name = _class_name(slug)
    target = os.path.abspath(os.path.join(out_dir, slug))
    if os.path.exists(target):
        if not force:
            raise FileExistsError(f"{target} already exists; pass --force to replace it")
        shutil.rmtree(target)

    package_dir = _package_path(package)
    java_root = os.path.join(target, "src", "main", "java", package_dir)
    files = {
        "pom.xml": f"""<project xmlns=\"http://maven.apache.org/POM/4.0.0\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
  xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{package.rsplit('.', 1)[0]}</groupId>
  <artifactId>{slug}</artifactId>
  <version>0.1.0-SNAPSHOT</version>
  <packaging>jar</packaging>
  <name>{slug}</name>
  <properties>
    <java.version>17</java.version>
  </properties>
</project>
""",
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
    }

    for rel, text in files.items():
        _write(os.path.join(target, rel), text)

    review = [
        f"# Review Diff for {slug}",
        "",
        "Generated files:",
        *[f"- {rel}" for rel in sorted(files)],
        "",
        "Manual review checklist:",
        "- Replace placeholder parent/dependency management with the approved api-parent/api-starter convention.",
        "- Confirm package naming against the target domain.",
        "- Add required request headers/interceptors from the reference service.",
        "- Replace sample listener queue and payload with reviewed message contracts.",
    ]
    _write(os.path.join(target, "REVIEW_DIFF.md"), "\n".join(review) + "\n")
    return {"path": target, "files": sorted([*files, "REVIEW_DIFF.md"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a scratch service scaffold.")
    parser.add_argument("service_name")
    parser.add_argument("--package", required=True, help="base Java package, e.g. com.hsbc.hase.example")
    parser.add_argument("--out-dir", default="scratch")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    result = generate_service(args.service_name, args.package, args.out_dir, args.force)
    print(f"wrote {result['path']}")
    for rel in result["files"]:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

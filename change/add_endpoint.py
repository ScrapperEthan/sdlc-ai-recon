#!/usr/bin/env python3
"""Add a small GET endpoint to a copied service, verify it, and emit a diff."""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scaffold.reference import _package_from_java

from .build import BuildResult, run_maven_tests


DIFF_FILE = "CHANGE_DIFF.md"
BUILD_FILE = "BUILD_RESULT.md"
_EXCLUDED_DIFF_ROOTS = {".git", "target", "build"}
_EXCLUDED_DIFF_FILES = {DIFF_FILE, BUILD_FILE}


@dataclass(frozen=True)
class ControllerCandidate:
    path: Path
    package: str
    class_name: str
    source: str
    suitable: bool


class BuildFailed(RuntimeError):
    def __init__(self, result: dict):
        super().__init__("build failed")
        self.result = result


def _single_path_segment(value: str, label: str) -> str:
    if not value or os.path.isabs(value) or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{label} must be a single path segment")
    return value


def _endpoint_path(value: str) -> str:
    if not value or not value.startswith("/"):
        raise ValueError("--path must start with /")
    if ".." in value or "\\" in value or '"' in value or "\n" in value or "\r" in value:
        raise ValueError("--path contains unsafe characters")
    if not re.match(r"^/[A-Za-z0-9_./{}-]*$", value):
        raise ValueError("--path contains unsupported characters")
    return value


def _method_name(value: str) -> str:
    if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", value or ""):
        raise ValueError("--method must be a Java identifier")
    return value


def _method_from_path(path: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", path) if part]
    if not parts:
        return "status"
    first, *rest = parts
    candidate = first.lower() + "".join(part[:1].upper() + part[1:].lower() for part in rest)
    if candidate[0].isdigit():
        candidate = f"endpoint{candidate[:1].upper()}{candidate[1:]}"
    return _method_name(candidate)


def _class_name(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:].lower() for part in parts) or "Status"


def _package_path(package: str) -> Path:
    if not re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", package):
        raise ValueError(f"invalid Java package: {package}")
    return Path(*package.split("."))


def _same_or_inside(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath([str(root.resolve()), str(path.resolve())]) == str(root.resolve())
    except ValueError:
        return False


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_service(service_repo: str, mirror: str, out_dir: str, force: bool) -> tuple[Path, Path]:
    repo = _single_path_segment(service_repo, "service-repo")
    mirror_root = Path(mirror).resolve()
    out_root = Path(out_dir).resolve()
    if _same_or_inside(mirror_root, out_root):
        raise ValueError("output root must not be inside the read-only mirror")

    source = mirror_root / repo
    if not source.is_dir():
        raise FileNotFoundError(f"source service not found: {source}")

    target = out_root / f"{repo}-change"
    if target.exists():
        if not force:
            raise FileExistsError(f"{target} already exists; pass --force to replace it")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return source, target


def _java_files(root: Path) -> list[Path]:
    java_root = root / "src" / "main" / "java"
    if not java_root.is_dir():
        return []
    return sorted(path for path in java_root.rglob("*.java") if all(part not in _EXCLUDED_DIFF_ROOTS for part in path.parts))


def _class_from_text(text: str) -> str | None:
    match = re.search(r"\b(?:public\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)\b", text)
    return match.group(1) if match else None


def _find_controllers(project_dir: Path) -> list[ControllerCandidate]:
    candidates: list[ControllerCandidate] = []
    for path in _java_files(project_dir):
        text = _read(path)
        if "@RestController" not in text:
            continue
        package_result = _package_from_java(str(path))
        class_name = _class_from_text(text)
        if not package_result or not class_name:
            continue
        package, _line = package_result
        lowered_parts = {part.lower() for part in path.parts}
        package_parts = {part.lower() for part in package.split(".")}
        suitable = bool({"resource", "controller"} & (lowered_parts | package_parts))
        candidates.append(ControllerCandidate(path, package, class_name, text, suitable))

    return sorted(
        candidates,
        key=lambda item: (
            not item.suitable,
            "resource" not in {part.lower() for part in item.path.parts},
            str(item.path),
        ),
    )


def _application_package(project_dir: Path) -> str | None:
    for path in _java_files(project_dir):
        text = _read(path)
        if "@SpringBootApplication" not in text and not path.name.endswith("Application.java"):
            continue
        package_result = _package_from_java(str(path))
        if package_result:
            return package_result[0]
    return None


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _indent_for_method(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^(\s*)@(Get|Post|Put|Delete|Patch|Request)Mapping\b", line)
        if match and match.group(1):
            return match.group(1)
    return "    "


def _inner_indent(indent: str) -> str:
    return f"{indent}    " if indent.strip() == "" else f"{indent}    "


def _has_get_mapping_import(text: str) -> bool:
    return (
        "import org.springframework.web.bind.annotation.GetMapping;" in text
        or "import org.springframework.web.bind.annotation.*;" in text
    )


def _add_import(text: str, import_line: str) -> str:
    if import_line in text:
        return text
    newline = _line_ending(text)
    lines = text.splitlines()
    import_indexes = [index for index, line in enumerate(lines) if line.startswith("import ")]
    if import_indexes:
        insert_at = import_indexes[-1] + 1
        lines.insert(insert_at, import_line)
        return newline.join(lines) + (newline if text.endswith(("\n", "\r\n")) else "")

    package_indexes = [index for index, line in enumerate(lines) if line.startswith("package ")]
    if package_indexes:
        insert_at = package_indexes[0] + 1
        lines.insert(insert_at, "")
        lines.insert(insert_at + 1, import_line)
        return newline.join(lines) + (newline if text.endswith(("\n", "\r\n")) else "")
    return f"{import_line}{newline}{text}"


def _method_block(path: str, method: str, indent: str, newline: str) -> str:
    inner = _inner_indent(indent)
    return newline.join(
        [
            f'{indent}@GetMapping("{path}")',
            f"{indent}public String {method}() {{",
            f'{inner}return "OK";',
            f"{indent}}}",
        ]
    )


def _insert_endpoint_method(text: str, path: str, method: str) -> str:
    if f'@GetMapping("{path}")' in text or f"@GetMapping('{path}')" in text:
        raise ValueError(f"endpoint already exists: {path}")
    if re.search(rf"\b{re.escape(method)}\s*\(", text):
        raise ValueError(f"method already exists: {method}")
    if not _has_get_mapping_import(text):
        text = _add_import(text, "import org.springframework.web.bind.annotation.GetMapping;")

    newline = _line_ending(text)
    indent = _indent_for_method(text)
    closing = text.rfind("}")
    if closing == -1:
        raise ValueError("controller class has no closing brace")
    before = text[:closing].rstrip()
    after = text[closing:].lstrip()
    return f"{before}{newline}{newline}{_method_block(path, method, indent, newline)}{newline}{after}"


def _has_simple_constructor(text: str, class_name: str) -> bool:
    if "@RequiredArgsConstructor" in text or "@AllArgsConstructor" in text:
        return False
    return re.search(rf"\b(public|protected|private)?\s*{re.escape(class_name)}\s*\(", text) is None


def _test_source(
    package: str,
    class_name: str,
    test_class_name: str,
    method: str,
    path: str,
    assert_return: bool,
) -> str:
    return_assert_import = "\nimport static org.junit.jupiter.api.Assertions.assertEquals;" if assert_return else ""
    return_test = ""
    if assert_return:
        return_test = f"""

    @Test
    void {method}ReturnsOk() {{
        {class_name} resource = new {class_name}();
        assertEquals("OK", resource.{method}());
    }}
"""
    return f"""package {package};

import static org.junit.jupiter.api.Assertions.assertArrayEquals;{return_assert_import}
import static org.junit.jupiter.api.Assertions.assertNotNull;

import org.junit.jupiter.api.Test;
import org.springframework.web.bind.annotation.GetMapping;

class {test_class_name} {{
    @Test
    void {method}EndpointIsDeclared() throws Exception {{
        GetMapping mapping = {class_name}.class.getDeclaredMethod("{method}").getAnnotation(GetMapping.class);
        assertNotNull(mapping);
        assertArrayEquals(new String[] {{"{path}"}}, mapping.value());
    }}{return_test}
}}
"""


def _unique_class_file(project_dir: Path, package: str, base_class_name: str) -> tuple[str, Path]:
    package_dir = _package_path(package)
    source_dir = project_dir / "src" / "main" / "java" / package_dir
    for index in range(1, 100):
        class_name = base_class_name if index == 1 else f"{base_class_name}{index}"
        path = source_dir / f"{class_name}.java"
        if not path.exists():
            return class_name, path
    raise FileExistsError(f"could not choose a unique class name for {base_class_name}")


def _unique_test_class_file(project_dir: Path, package: str, base_class_name: str) -> tuple[str, Path]:
    package_dir = _package_path(package)
    test_dir = project_dir / "src" / "test" / "java" / package_dir
    for index in range(1, 100):
        class_name = base_class_name if index == 1 else f"{base_class_name}{index}"
        path = test_dir / f"{class_name}.java"
        if not path.exists():
            return class_name, path
    raise FileExistsError(f"could not choose a unique test class name for {base_class_name}")


def _write_test(project_dir: Path, controller: ControllerCandidate, method: str, endpoint: str) -> Path:
    test_class_name, test_file = _unique_test_class_file(
        project_dir,
        controller.package,
        f"{controller.class_name}{_class_name(method)}Test",
    )
    assert_return = _has_simple_constructor(controller.source, controller.class_name)
    _write(
        test_file,
        _test_source(
            controller.package,
            controller.class_name,
            test_class_name,
            method,
            endpoint,
            assert_return,
        ),
    )
    return test_file


def _create_resource(project_dir: Path, package: str, base_class_name: str, endpoint: str, method: str) -> ControllerCandidate:
    class_name, source_path = _unique_class_file(project_dir, package, base_class_name)
    text = f"""package {package};

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class {class_name} {{
    @GetMapping("{endpoint}")
    public String {method}() {{
        return "OK";
    }}
}}
"""
    _write(source_path, text)
    return ControllerCandidate(source_path, package, class_name, text, True)


def _apply_change(project_dir: Path, endpoint: str, method: str) -> list[Path]:
    controllers = _find_controllers(project_dir)
    touched: list[Path] = []
    selected = next((candidate for candidate in controllers if candidate.suitable), None)

    if selected is not None:
        updated = _insert_endpoint_method(selected.source, endpoint, method)
        _write(selected.path, updated)
        selected = ControllerCandidate(selected.path, selected.package, selected.class_name, updated, True)
        touched.append(selected.path)
    else:
        if controllers:
            base_package = controllers[0].package
        else:
            app_package = _application_package(project_dir)
            if not app_package:
                raise ValueError("could not find a RestController or SpringBootApplication package")
            base_package = f"{app_package}.resource"
        selected = _create_resource(project_dir, base_package, f"{_class_name(method)}Resource", endpoint, method)
        touched.append(selected.path)

    touched.append(_write_test(project_dir, selected, method, endpoint))
    return touched


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_review_files(root: Path) -> list[str]:
    files: list[str] = []
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if rel_parts[0] in _EXCLUDED_DIFF_ROOTS or rel_parts[-1] in _EXCLUDED_DIFF_FILES:
            continue
        files.append(Path(*rel_parts).as_posix())
    return sorted(files)


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def _changed_files(source: Path, target: Path) -> list[str]:
    rels = sorted(set(_iter_review_files(source)) | set(_iter_review_files(target)))
    changed = []
    for rel in rels:
        if _read_lines(source / rel) != _read_lines(target / rel):
            changed.append(rel)
    return changed


def _diff_for_file(source: Path, target: Path, rel: str) -> str:
    before = [line.rstrip("\n") for line in _read_lines(source / rel)]
    after = [line.rstrip("\n") for line in _read_lines(target / rel)]
    return "\n".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"mirror/{source.name}/{rel}",
            tofile=f"scratch/{target.name}/{rel}",
            lineterm="",
        )
    )


def _write_change_diff(service_repo: str, source: Path, target: Path) -> list[str]:
    changed = _changed_files(source, target)
    lines = [
        f"# Change Diff for {service_repo}",
        "",
        "Touched files:",
        *[f"- {rel}" for rel in changed],
        "",
    ]
    for rel in changed:
        diff = _diff_for_file(source, target, rel)
        lines.extend([f"## {rel}", "", "```diff", diff.rstrip(), "```", ""])
    _write(target / DIFF_FILE, "\n".join(lines).rstrip() + "\n")
    return changed


def _write_build_skipped(service_repo: str, target: Path) -> None:
    text = f"""# Build Result for {service_repo}

Status: SKIPPED (--skip-build)

The change was generated and diffed but not compiled or tested — no Java/Maven
toolchain is available yet. Re-run without --skip-build once the toolchain lands
to verify the change compiles and its tests pass.
"""
    _write(target / BUILD_FILE, text)


def _write_build_result(service_repo: str, target: Path, build: BuildResult) -> None:
    status = "PASS" if build.returncode == 0 else "FAIL"
    command = " ".join(build.command)
    tail = build.output_tail or "<no output>"
    text = f"""# Build Result for {service_repo}

Command: `{command}`
Working directory: `{build.cwd}`
Status: {status} (exit {build.returncode})

Output tail:
```text
{tail}
```
"""
    _write(target / BUILD_FILE, text)


def add_endpoint(
    service_repo: str,
    path: str,
    method: str | None = None,
    mirror: str = "mirror",
    out_dir: str = "scratch",
    force: bool = False,
    skip_build: bool = False,
    runner: Callable[[tuple[str, ...], str], object] | None = None,
) -> dict:
    endpoint = _endpoint_path(path)
    method_name = _method_name(method) if method else _method_from_path(endpoint)
    source, target = _copy_service(service_repo, mirror, out_dir, force)
    touched_paths = _apply_change(target, endpoint, method_name)
    if skip_build:
        build = None
        _write_build_skipped(service_repo, target)
    else:
        build = run_maven_tests(str(target), runner=runner)
        _write_build_result(service_repo, target, build)
    changed = _write_change_diff(service_repo, source, target)
    result = {
        "path": str(target),
        "changed_files": changed,
        "touched_files": [_rel(path, target) for path in touched_paths],
        "build": build,
    }
    if build is not None and build.returncode != 0:
        raise BuildFailed(result)
    return result


def main(argv=None, runner: Callable[[tuple[str, ...], str], object] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add a small GET endpoint to a scratch service copy.")
    parser.add_argument("service_repo")
    parser.add_argument("--path", required=True, help="endpoint path, e.g. /status")
    parser.add_argument("--method", help="Java method name; defaults from --path")
    parser.add_argument("--mirror", default="mirror")
    parser.add_argument("--out-dir", default="scratch")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="generate the change + diff but do not compile/test (no toolchain yet)",
    )
    args = parser.parse_args(argv)

    try:
        result = add_endpoint(
            args.service_repo,
            args.path,
            method=args.method,
            mirror=args.mirror,
            out_dir=args.out_dir,
            force=args.force,
            skip_build=args.skip_build,
            runner=runner,
        )
    except BuildFailed as exc:
        result = exc.result
        build = result["build"]
        print(f"build failed with exit {build.returncode}; wrote {result['path']}")
        return build.returncode or 1
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {result['path']}")
    for rel in result["changed_files"]:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

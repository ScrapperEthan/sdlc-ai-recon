#!/usr/bin/env python3
"""Resolve a natural-language ask, apply the templated change, and verify it."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from .add_endpoint import BuildFailed, add_endpoint
from .intent import ChangeRequest, parse_intent
from .locate import (
    TARGET_FILE,
    AmbiguousTarget,
    TargetResolution,
    format_target_resolution,
    resolve_target,
    write_target_resolution,
)


def _out_root(out_dir: str) -> Path:
    return Path(out_dir).resolve()


def _write_refusal(out_dir: str, request: ChangeRequest, message: str, candidates=()) -> Path:
    path = _out_root(out_dir) / TARGET_FILE
    write_target_resolution(
        path,
        format_target_resolution(request, candidates=candidates, error=message),
    )
    return path


def from_intent(
    text: str,
    mirror: str = "mirror",
    out_dir: str = "scratch",
    explain_only: bool = False,
    skip_build: bool = False,
    force: bool = False,
    parser: Callable[[str], object] | None = None,
    resolver: Callable[..., TargetResolution] | None = None,
    runner: Callable[[tuple[str, ...], str], object] | None = None,
) -> dict:
    request = parse_intent(text, parser=parser)
    try:
        resolution = resolve_target(request, mirror=mirror, resolver=resolver)
    except AmbiguousTarget as exc:
        artifact = _write_refusal(out_dir, request, str(exc), exc.candidates)
        raise AmbiguousTarget(f"{exc}; wrote {artifact}", exc.candidates) from exc

    resolution_text = format_target_resolution(request, resolution=resolution)
    if explain_only:
        artifact = _out_root(out_dir) / TARGET_FILE
        write_target_resolution(artifact, resolution_text)
        return {
            "request": request,
            "resolution": resolution,
            "path": str(artifact),
            "changed_files": [],
            "build": None,
        }

    try:
        result = add_endpoint(
            resolution.repo,
            request.path,
            method=request.method,
            mirror=mirror,
            out_dir=out_dir,
            force=force,
            skip_build=skip_build,
            runner=runner,
        )
    except BuildFailed as exc:
        result = exc.result
        write_target_resolution(Path(result["path"]) / TARGET_FILE, resolution_text)
        raise

    write_target_resolution(Path(result["path"]) / TARGET_FILE, resolution_text)
    result["request"] = request
    result["resolution"] = resolution
    return result


def main(
    argv=None,
    parser: Callable[[str], object] | None = None,
    resolver: Callable[..., TargetResolution] | None = None,
    runner: Callable[[tuple[str, ...], str], object] | None = None,
) -> int:
    cli = argparse.ArgumentParser(description="Resolve an intent and add a templated GET endpoint.")
    cli.add_argument("intent", help='e.g. "add a /status endpoint to the ingress service"')
    cli.add_argument("--mirror", default="mirror")
    cli.add_argument("--out-dir", default="scratch")
    cli.add_argument("--explain-only", action="store_true")
    cli.add_argument("--skip-build", action="store_true")
    cli.add_argument("--force", action="store_true")
    args = cli.parse_args(argv)

    try:
        result = from_intent(
            args.intent,
            mirror=args.mirror,
            out_dir=args.out_dir,
            explain_only=args.explain_only,
            skip_build=args.skip_build,
            force=args.force,
            parser=parser,
            resolver=resolver,
            runner=runner,
        )
    except AmbiguousTarget as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BuildFailed as exc:
        result = exc.result
        build = result["build"]
        print(f"build failed with exit {build.returncode}; wrote {result['path']}")
        return build.returncode or 1
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {result['path']}")
    resolution = result.get("resolution")
    if resolution:
        print(f"  target: {resolution.repo}/{resolution.controller_path}")
    for rel in result.get("changed_files", []):
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

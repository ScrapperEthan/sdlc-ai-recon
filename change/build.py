"""Build runner wrapper for scratch changes."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable


DEFAULT_COMMAND = ("mvn", "-q", "test")


@dataclass(frozen=True)
class BuildResult:
    command: tuple[str, ...]
    cwd: str
    returncode: int
    output: str
    output_tail: str


def _tail(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _coerce_result(raw, command: tuple[str, ...], cwd: str, tail_lines: int) -> BuildResult:
    if isinstance(raw, BuildResult):
        return raw

    if hasattr(raw, "returncode"):
        stdout = getattr(raw, "stdout", None) or ""
        stderr = getattr(raw, "stderr", None) or ""
        output = stdout if not stderr else f"{stdout}{stderr}"
        return BuildResult(command, cwd, int(raw.returncode), str(output), _tail(str(output), tail_lines))

    if isinstance(raw, tuple):
        if len(raw) == 2:
            returncode, output = raw
        elif len(raw) == 3:
            returncode, stdout, stderr = raw
            output = f"{stdout or ''}{stderr or ''}"
        else:
            raise TypeError("build runner tuple must be (returncode, output) or (returncode, stdout, stderr)")
        output = str(output or "")
        return BuildResult(command, cwd, int(returncode), output, _tail(output, tail_lines))

    raise TypeError("build runner must return BuildResult, CompletedProcess, or a tuple")


def run_maven_tests(
    project_dir: str,
    runner: Callable[[tuple[str, ...], str], object] | None = None,
    command: Iterable[str] = DEFAULT_COMMAND,
    tail_lines: int = 80,
) -> BuildResult:
    """Run or mock `mvn -q test` and capture a compact result."""
    command_tuple = tuple(command)
    if runner is None:
        raw = subprocess.run(
            command_tuple,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
        )
    else:
        raw = runner(command_tuple, project_dir)
    return _coerce_result(raw, command_tuple, project_dir, tail_lines)


from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from pathlib import Path

from qas.config import GateConfig
from qas.models import GateResult


class PolicyViolation(RuntimeError):
    pass


_SECRET_PATTERNS = {
    "private_key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "generic_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][^'\"\r\n]{6,}['\"]"
    ),
}


def git(project_root: Path, *args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise PolicyViolation(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def changed_paths(project_root: Path, base: str | None = None, head: str = "HEAD") -> set[str]:
    paths: set[str] = set()
    status = git(project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items = status.split("\0")
    index = 0
    while index < len(items):
        entry = items[index]
        if not entry:
            index += 1
            continue
        code, path = entry[:2], entry[3:]
        paths.add(path.replace("\\", "/"))
        if "R" in code or "C" in code:
            index += 1
            if index < len(items) and items[index]:
                paths.add(items[index].replace("\\", "/"))
        index += 1
    if base:
        diff = git(project_root, "diff", "--name-only", "-z", base, head)
        paths.update(item.replace("\\", "/") for item in diff.split("\0") if item)
    return paths


def protected_changes(paths: set[str], patterns: tuple[str, ...]) -> list[str]:
    return sorted(
        path for path in paths if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    )


def assert_governance(
    project_root: Path, patterns: tuple[str, ...], base: str | None = None
) -> None:
    violations = protected_changes(changed_paths(project_root, base), patterns)
    if violations:
        raise PolicyViolation("Protected governance paths changed: " + ", ".join(violations))


def scan_added_secrets(diff: str) -> list[dict[str, str | int]]:
    findings: list[dict[str, str | int]] = []
    current_file = "unknown"
    added_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            added_line = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            for name, pattern in _SECRET_PATTERNS.items():
                if pattern.search(content):
                    findings.append({"kind": name, "file": current_file, "line": added_line})
            added_line += 1
        elif not line.startswith("-"):
            added_line += 1
    return findings


def run_gate(project_root: Path, gate: GateConfig) -> GateResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(gate.command),
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=gate.timeout_seconds,
            check=False,
        )
        code = result.returncode
        output = (result.stdout + "\n" + result.stderr)[-12000:]
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        output = (stdout + "\n" + stderr + "\nTIMEOUT")[-12000:]
    return GateResult(gate.name, code == 0, code, time.monotonic() - started, output)

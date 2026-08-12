from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class DogfoodError(ValueError):
    pass


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    duration_seconds: float
    steps: tuple[dict[str, Any], ...]


def run_scenario(path: Path, project_root: Path) -> ScenarioResult:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise DogfoodError(f"Invalid scenario: {path}")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DogfoodError(f"Scenario has no steps: {path}")
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    all_passed = True
    for index, item in enumerate(steps):
        if not isinstance(item, dict) or not isinstance(item.get("command"), list):
            raise DogfoodError(f"Step {index + 1} command must be an argv list")
        argv = item["command"]
        if not argv or not all(isinstance(part, str) for part in argv):
            raise DogfoodError(f"Step {index + 1} command is invalid")
        timeout = int(item.get("timeoutSeconds", 300))
        step_start = time.monotonic()
        try:
            process = subprocess.run(
                argv,
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            output = process.stdout + "\n" + process.stderr
            expected_code = int(item.get("expectExitCode", 0))
            passed = process.returncode == expected_code
            contains = item.get("stdoutContains")
            if contains is not None:
                if not isinstance(contains, str):
                    raise DogfoodError(f"Step {index + 1} stdoutContains must be text")
                passed = passed and contains in process.stdout
            pattern = item.get("outputRegex")
            if pattern is not None:
                if not isinstance(pattern, str):
                    raise DogfoodError(f"Step {index + 1} outputRegex must be text")
                passed = passed and re.search(pattern, output) is not None
            code = process.returncode
        except subprocess.TimeoutExpired as exc:
            output = f"timeout after {timeout}s: {exc}"
            passed, code = False, 124
        results.append(
            {
                "index": index + 1,
                "command": argv,
                "passed": passed,
                "exit_code": code,
                "duration_seconds": round(time.monotonic() - step_start, 3),
                "output_tail": output[-4000:],
            }
        )
        all_passed = all_passed and passed
        if not passed and bool(item.get("stopOnFailure", True)):
            break
    return ScenarioResult(raw["name"], all_passed, time.monotonic() - started, tuple(results))

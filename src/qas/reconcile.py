from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(argv: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def reconcile(project_root: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"git": {}, "github": {"available": False}}
    checks = {
        "head": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "branch", "--show-current"],
        "status": ["git", "status", "--porcelain=v1"],
        "remote": ["git", "remote", "get-url", "origin"],
    }
    for name, argv in checks.items():
        code, stdout, stderr = _run(argv, project_root)
        snapshot["git"][name] = stdout if code == 0 else {"error": stderr, "exit_code": code}

    if shutil.which("gh") is None:
        snapshot["github"] = {"available": False, "reason": "gh_not_installed"}
        return snapshot
    code, stdout, stderr = _run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "nameWithOwner,defaultBranchRef,isPrivate,url",
        ],
        project_root,
    )
    if code:
        snapshot["github"] = {
            "available": False,
            "reason": "gh_unavailable",
            "error": stderr[-1000:],
        }
        return snapshot
    try:
        repository = json.loads(stdout)
    except json.JSONDecodeError:
        repository = {"error": "invalid_gh_json"}
    snapshot["github"] = {"available": True, "repository": repository}
    for key, argv in {
        "issues": [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,author,labels,updatedAt,url",
        ],
        "pull_requests": [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,headRefName,statusCheckRollup,updatedAt,url",
        ],
    }.items():
        code, stdout, stderr = _run(argv, project_root, 45)
        if code == 0:
            try:
                snapshot["github"][key] = json.loads(stdout)
            except json.JSONDecodeError:
                snapshot["github"][key] = {"error": "invalid_gh_json"}
        else:
            snapshot["github"][key] = {"error": stderr[-1000:], "exit_code": code}
    return snapshot

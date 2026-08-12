from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def audit_governance(project_root: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "github_cli": shutil.which("gh") is not None,
        "codeowners": (project_root / ".github" / "CODEOWNERS").is_file(),
        "autonomy_contract": (project_root / "AUTONOMY.md").is_file(),
        "remote_authenticated": False,
        "branch_protection_readable": False,
        "pull_request_reviews_required": False,
        "code_owner_reviews_required": False,
        "status_checks_required": False,
        "administrators_enforced": False,
        "bot_least_privilege": False,
    }
    if not checks["github_cli"]:
        checks["safe"] = False
        checks["reason"] = "gh_not_installed"
        return checks
    code, stdout, stderr = _run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "nameWithOwner,defaultBranchRef,viewerPermission",
        ],
        project_root,
    )
    if code:
        checks["safe"] = False
        checks["reason"] = "gh_unavailable"
        checks["error"] = stderr[-1000:]
        return checks
    try:
        repository = json.loads(stdout)
        owner_name = repository["nameWithOwner"]
        default_branch = repository["defaultBranchRef"]["name"]
        viewer_permission = repository["viewerPermission"]
    except (json.JSONDecodeError, KeyError, TypeError):
        checks["safe"] = False
        checks["reason"] = "invalid_repository_metadata"
        return checks
    checks["remote_authenticated"] = True
    checks["repository"] = owner_name
    checks["default_branch"] = default_branch
    checks["viewer_permission"] = viewer_permission
    checks["bot_least_privilege"] = viewer_permission == "WRITE"
    code, stdout, stderr = _run(
        ["gh", "api", f"repos/{owner_name}/branches/{default_branch}/protection"], project_root
    )
    if code:
        checks["safe"] = False
        checks["reason"] = "branch_protection_unreadable"
        checks["error"] = stderr[-1000:]
        return checks
    try:
        protection = json.loads(stdout)
    except json.JSONDecodeError:
        checks["safe"] = False
        checks["reason"] = "invalid_branch_protection_json"
        return checks
    checks["branch_protection_readable"] = True
    reviews = protection.get("required_pull_request_reviews")
    status = protection.get("required_status_checks")
    admins = protection.get("enforce_admins")
    checks["pull_request_reviews_required"] = (
        isinstance(reviews, dict) and int(reviews.get("required_approving_review_count", 0)) >= 1
    )
    checks["code_owner_reviews_required"] = isinstance(reviews, dict) and bool(
        reviews.get("require_code_owner_reviews")
    )
    checks["status_checks_required"] = isinstance(status, dict) and bool(
        status.get("contexts") or status.get("checks")
    )
    checks["administrators_enforced"] = isinstance(admins, dict) and bool(admins.get("enabled"))
    checks["safe"] = all(
        checks[name]
        for name in (
            "codeowners",
            "autonomy_contract",
            "branch_protection_readable",
            "pull_request_reviews_required",
            "code_owner_reviews_required",
            "status_checks_required",
            "administrators_enforced",
            "bot_least_privilege",
        )
    )
    if not checks["safe"]:
        checks["reason"] = "required_governance_control_missing"
    return checks

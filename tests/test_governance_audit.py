from __future__ import annotations

import json
from pathlib import Path

import pytest

from qas.governance_audit import audit_governance


def test_audit_reports_missing_cli(git_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qas.governance_audit.shutil.which", lambda _name: None)
    report = audit_governance(git_project)
    assert not report["safe"]
    assert report["reason"] == "gh_not_installed"


def test_audit_accepts_enforced_controls(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_project / ".github").mkdir()
    (git_project / ".github" / "CODEOWNERS").write_text("* @maintainer\n", encoding="utf-8")
    (git_project / "AUTONOMY.md").write_text("contract\n", encoding="utf-8")
    monkeypatch.setattr("qas.governance_audit.shutil.which", lambda _name: "gh")

    def fake_run(argv: list[str], _cwd: Path) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "repo", "view"]:
            return (
                0,
                json.dumps(
                    {
                        "nameWithOwner": "owner/repo",
                        "defaultBranchRef": {"name": "main"},
                        "viewerPermission": "WRITE",
                    }
                ),
                "",
            )
        return (
            0,
            json.dumps(
                {
                    "required_pull_request_reviews": {
                        "required_approving_review_count": 1,
                        "require_code_owner_reviews": True,
                    },
                    "required_status_checks": {"contexts": ["verify"]},
                    "enforce_admins": {"enabled": True},
                }
            ),
            "",
        )

    monkeypatch.setattr("qas.governance_audit._run", fake_run)
    report = audit_governance(git_project)
    assert report["safe"]
    assert report["repository"] == "owner/repo"

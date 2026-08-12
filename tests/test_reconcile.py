from __future__ import annotations

import json
from pathlib import Path

import pytest

from qas.reconcile import reconcile


def test_reconcile_without_github_cli(git_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qas.reconcile.shutil.which", lambda _name: None)
    snapshot = reconcile(git_project)
    assert snapshot["git"]["branch"] == "main"
    assert snapshot["github"]["reason"] == "gh_not_installed"


def test_reconcile_github_json(git_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qas.reconcile.shutil.which", lambda _name: "gh")

    def fake_run(argv: list[str], _cwd: Path, _timeout: int = 30) -> tuple[int, str, str]:
        if argv[:2] == ["gh", "repo"]:
            return 0, json.dumps({"nameWithOwner": "owner/repo"}), ""
        if argv[:2] in (["gh", "issue"], ["gh", "pr"]):
            return 0, "[]", ""
        return 0, "value", ""

    monkeypatch.setattr("qas.reconcile._run", fake_run)
    snapshot = reconcile(git_project)
    assert snapshot["github"]["available"]
    assert snapshot["github"]["repository"]["nameWithOwner"] == "owner/repo"

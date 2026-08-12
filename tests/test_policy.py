from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qas.config import GateConfig
from qas.policy import (
    PolicyViolation,
    assert_governance,
    changed_paths,
    protected_changes,
    run_gate,
    scan_added_secrets,
)


def test_governance_change_is_blocked(git_project: Path) -> None:
    protected = git_project / "AUTONOMY.md"
    protected.write_text("do not change\n", encoding="utf-8")
    assert "AUTONOMY.md" in changed_paths(git_project)
    assert protected_changes({"AUTONOMY.md"}, ("AUTONOMY.md",)) == ["AUTONOMY.md"]
    with pytest.raises(PolicyViolation, match="Protected"):
        assert_governance(git_project, ("AUTONOMY.md",))


def test_committed_governance_change_is_blocked(git_project: Path) -> None:
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_project, text=True).strip()
    (git_project / "AUTONOMY.md").write_text("contract\n", encoding="utf-8")
    subprocess.run(["git", "add", "AUTONOMY.md"], cwd=git_project, check=True)
    subprocess.run(["git", "commit", "-m", "bad"], cwd=git_project, check=True, capture_output=True)
    with pytest.raises(PolicyViolation):
        assert_governance(git_project, ("AUTONOMY.md",), base)


def test_quality_gate_pass_and_fail(git_project: Path) -> None:
    good = run_gate(git_project, GateConfig("ok", ("git", "status"), 5))
    bad = run_gate(git_project, GateConfig("bad", ("git", "not-a-command"), 5))
    assert good.passed
    assert not bad.passed


def test_secret_scan_checks_additions_but_not_removals() -> None:
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-API_KEY=\"old-secret\"
+API_KEY=\"123456\"
"""
    findings = scan_added_secrets(diff)
    assert findings == [{"kind": "generic_secret", "file": "app.py", "line": 1}]

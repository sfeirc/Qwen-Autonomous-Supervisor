from __future__ import annotations

from pathlib import Path

import pytest

from qas.db import Ledger
from qas.operations import IdempotentOperationRunner, RemoteEvidence


def test_crash_after_remote_success_reconciles_without_duplicate(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    runner = IdempotentOperationRunner(ledger)
    remote: dict[str, str] = {}
    calls = 0

    def reconcile() -> RemoteEvidence:
        return RemoteEvidence(bool(remote), remote.get("id"), {"sha": remote.get("sha")})

    def mutate(key: str) -> str:
        nonlocal calls
        calls += 1
        remote.update(id="PR-42", sha="abc", marker=runner.marker(key))
        return "PR-42"

    with pytest.raises(RuntimeError, match="injected crash"):
        runner.ensure(
            operation_key="create-pr:issue-12",
            kind="create_pr",
            request={"head": "issue/12-fix"},
            reconcile=reconcile,
            mutate=mutate,
            after_remote_effect=lambda: (_ for _ in ()).throw(RuntimeError("injected crash")),
        )

    outcome = IdempotentOperationRunner(ledger).ensure(
        operation_key="create-pr:issue-12",
        kind="create_pr",
        request={"head": "issue/12-fix"},
        reconcile=reconcile,
        mutate=mutate,
    )
    assert calls == 1
    assert outcome.reconciled
    assert outcome.external_id == "PR-42"
    assert ledger.operation("create-pr:issue-12")["status"] == "completed"


def test_completed_operation_is_a_noop(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    runner = IdempotentOperationRunner(ledger)
    ledger.begin_operation("push:main:abc", "push", {"sha": "abc"})
    ledger.complete_operation("push:main:abc", "abc", {"remote": "origin"})
    outcome = runner.ensure(
        operation_key="push:main:abc",
        kind="push",
        request={"sha": "abc"},
        reconcile=lambda: RemoteEvidence(False),
        mutate=lambda _key: (_ for _ in ()).throw(AssertionError("must not mutate")),
    )
    assert outcome.reconciled
    assert outcome.external_id == "abc"

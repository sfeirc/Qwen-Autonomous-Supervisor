from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from qas.db import Ledger, LedgerIntegrityError


def test_events_are_append_only_and_idempotent(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    first = ledger.append_event("created", operation_key="same", payload={"x": 1})
    second = ledger.append_event("created", operation_key="same", payload={"x": 2})
    assert first is not None
    assert second is None
    with ledger.connect() as connection:
        try:
            connection.execute("UPDATE events SET kind='tampered'")
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("event update unexpectedly succeeded")


def test_lease_is_exclusive_renewable_and_expiring(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    now = time.time()
    assert ledger.acquire_lease("coordinator", "one", now, 10)
    assert not ledger.acquire_lease("coordinator", "two", now, 10)
    assert ledger.renew_lease("coordinator", "one", now + 1, 10)
    assert not ledger.release_lease("coordinator", "two")
    assert ledger.release_lease("coordinator", "one")
    assert ledger.acquire_lease("coordinator", "two", now, 1)
    expired = ledger.reap_expired_leases(now + 2)
    assert expired[0]["owner"] == "two"


def test_failures_quarantine_at_threshold(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    assert ledger.record_failure("fp", "tick", "evidence", 2) == (1, False)
    assert ledger.record_failure("fp", "tick", "new evidence", 2) == (2, True)


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    ledger.set_checkpoint("position", {"tick": 42})
    assert ledger.get_checkpoint("position") == {"tick": 42}
    assert ledger.get_checkpoint("missing") is None


def test_quarantine_requires_explicit_clear(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    ledger.record_failure("fp", "tick", "evidence", 1)
    assert ledger.failures(quarantined_only=True)[0]["fingerprint"] == "fp"
    assert ledger.unquarantine("fp")
    assert not ledger.failures(quarantined_only=True)
    assert not ledger.unquarantine("missing")


def test_usage_totals_handle_nested_provider_shapes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    ledger.append_event(
        "coordinator_completed",
        payload={"usage": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}},
    )
    ledger.append_event(
        "independent_review",
        payload={"usage": {"models": [{"prompt_tokens": 10, "completion_tokens": 5}]}},
    )
    usage = ledger.usage_totals()
    assert usage == {
        "input_tokens": 110,
        "output_tokens": 30,
        "total_tokens": 140,
        "model_calls": 2,
    }


def test_integrity_check_rejects_corrupt_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(LedgerIntegrityError, match="integrity"):
        Ledger(path)


def test_usage_filters_and_does_not_double_count_model_events(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    ledger.append_event(
        "model_usage",
        tick_id="tick-1",
        payload={"role": "coordinator", "issue_number": 9, "usage": {"total_tokens": 40}},
    )
    ledger.append_event(
        "coordinator_completed",
        tick_id="tick-1",
        payload={"result": {"issue_number": 9}, "usage": {"total_tokens": 40}},
    )
    ledger.append_event(
        "model_usage",
        tick_id="tick-2",
        payload={"role": "reviewer", "issue_number": 10, "usage": {"total_tokens": 5}},
    )
    assert ledger.usage_totals()["total_tokens"] == 45
    assert ledger.usage_totals(tick_id="tick-1")["total_tokens"] == 40
    assert ledger.usage_totals(issue_number=10)["total_tokens"] == 5

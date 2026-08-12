from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qas.db import Ledger


class OperationPending(RuntimeError):
    """The remote result is not yet observable; retry reconciliation later."""


class OperationNotConfirmed(RuntimeError):
    """A mutation returned but its durable remote effect could not be confirmed."""


@dataclass(frozen=True)
class RemoteEvidence:
    exists: bool
    external_id: str | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class OperationOutcome:
    operation_key: str
    external_id: str | None
    reconciled: bool
    evidence: dict[str, Any]


class IdempotentOperationRunner:
    """Reconcile-before-mutate wrapper for remote at-least-once operations.

    The caller must embed ``operation_key`` in create-style remote mutations
    (Issue, PR, comment) or use a naturally idempotent target (branch SHA).
    This makes a crash after remote success but before local persistence safe.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def ensure(
        self,
        *,
        operation_key: str,
        kind: str,
        request: dict[str, Any],
        reconcile: Callable[[], RemoteEvidence],
        mutate: Callable[[str], str | None],
        after_remote_effect: Callable[[], None] | None = None,
    ) -> OperationOutcome:
        previous = self.ledger.operation(operation_key)
        if previous and previous["status"] == "completed":
            evidence = self._decode_evidence(previous.get("evidence"))
            return OperationOutcome(operation_key, previous.get("external_id"), True, evidence)

        remote = reconcile()
        if remote.exists:
            evidence = remote.evidence or {"source": "remote_reconciliation"}
            self.ledger.begin_operation(operation_key, kind, request)
            self.ledger.complete_operation(operation_key, remote.external_id, evidence)
            self.ledger.append_event(
                "operation_reconciled",
                operation_key=f"operation:reconciled:{operation_key}",
                payload={
                    "operation_key": operation_key,
                    "kind": kind,
                    "external_id": remote.external_id,
                },
            )
            return OperationOutcome(operation_key, remote.external_id, True, evidence)

        self.ledger.begin_operation(operation_key, kind, request)
        try:
            mutate(operation_key)
            if after_remote_effect:
                after_remote_effect()
            confirmed = reconcile()
            if not confirmed.exists:
                raise OperationNotConfirmed(
                    f"remote effect for {operation_key} is not yet observable"
                )
            evidence = confirmed.evidence or {"source": "post_mutation_reconciliation"}
            self.ledger.complete_operation(operation_key, confirmed.external_id, evidence)
            self.ledger.append_event(
                "operation_completed",
                operation_key=f"operation:completed:{operation_key}",
                payload={
                    "operation_key": operation_key,
                    "kind": kind,
                    "external_id": confirmed.external_id,
                },
            )
            return OperationOutcome(operation_key, confirmed.external_id, False, evidence)
        except Exception as exc:
            # Pending is deliberate: after a crash/timeout the remote side is
            # authoritative and must be reconciled before another mutation.
            self.ledger.append_event(
                "operation_interrupted",
                operation_key=f"operation:interrupted:{operation_key}",
                payload={"operation_key": operation_key, "kind": kind, "error": str(exc)[-2000:]},
            )
            raise

    @staticmethod
    def marker(operation_key: str) -> str:
        return f"<!-- qas-operation:{operation_key} -->"

    @staticmethod
    def _decode_evidence(value: Any) -> dict[str, Any]:
        import json

        if isinstance(value, str):
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        return value if isinstance(value, dict) else {}

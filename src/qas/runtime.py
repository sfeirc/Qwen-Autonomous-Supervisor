from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qas.config import SupervisorConfig
from qas.db import Ledger
from qas.models import ACTIVE_CAMPAIGN_CHECKPOINT_KEY, TickOutcome, canonical_json, fingerprint
from qas.policy import (
    PolicyViolation,
    assert_governance,
    changed_paths,
    git,
    run_gate,
    scan_added_secrets,
)
from qas.process import terminate_pid_tree
from qas.qwen import QwenLauncher, QwenUnavailable
from qas.reconcile import reconcile


class AlreadyRunning(RuntimeError):
    pass


class SafetyPause(RuntimeError):
    """A recoverable host condition that must pause model launches."""


class BudgetExceeded(SafetyPause):
    pass


_TRANSIENT_DEPENDENCY = re.compile(
    r"(?i)\b429\b|(?:http(?:\s+status)?|status|code)\s*[:=]?\s*(?:500|502|503|504)\b|"
    r"rate[ -]?limit|too many requests|"
    r"provider.{0,40}(?:unavailable|timeout|timed out)|service unavailable|"
    r"(?:connection|request) (?:refused|reset|timed out)|temporary failure|fetch failed|"
    r"econn(?:refused|reset)|github.{0,40}unavailable|failed to connect.{0,40}github"
)


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning("another scheduler process holds the runtime lock") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Supervisor:
    def __init__(self, config: SupervisorConfig, package_root: Path) -> None:
        self.config = config
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.config.runtime_dir / "runs").mkdir(exist_ok=True)
        self.ledger = Ledger(self.config.runtime_dir / "state.db")
        if self.ledger.get_checkpoint("supervisor_created_epoch") is None:
            self.ledger.set_checkpoint("supervisor_created_epoch", time.time())
        self.launcher = QwenLauncher(config, self.ledger, package_root)
        self._stop = threading.Event()

    def _cost(self, usage: dict[str, int]) -> float:
        return (
            usage["input_tokens"] * self.config.observability.input_cost_per_million
            + usage["output_tokens"] * self.config.observability.output_cost_per_million
        ) / 1_000_000

    def _budget_usage(
        self, tick_id: str | None = None, issue_number: int | None = None
    ) -> dict[str, dict[str, float | int]]:
        now = datetime.now(UTC)
        scopes = {
            "hour": self.ledger.usage_totals(since=(now - timedelta(hours=1)).isoformat()),
            "day": self.ledger.usage_totals(since=(now - timedelta(days=1)).isoformat()),
        }
        if tick_id is not None:
            scopes["tick"] = self.ledger.usage_totals(tick_id=tick_id)
        if issue_number is not None:
            scopes["issue"] = self.ledger.usage_totals(issue_number=issue_number)
        return {
            name: {**usage, "estimated_cost": self._cost(usage)}
            for name, usage in scopes.items()
        }

    def _assert_budgets(self, tick_id: str, issue_number: int | None) -> None:
        usage = self._budget_usage(tick_id, issue_number)
        limits: dict[str, tuple[int, float | None]] = {
            "tick": (
                self.config.budgets.max_tokens_per_tick,
                self.config.budgets.max_cost_per_tick,
            ),
            "hour": (
                self.config.budgets.max_tokens_per_hour,
                self.config.budgets.max_cost_per_hour,
            ),
            "day": (
                self.config.budgets.max_tokens_per_day,
                self.config.budgets.max_cost_per_day,
            ),
            "issue": (
                self.config.budgets.max_tokens_per_issue,
                self.config.budgets.max_cost_per_issue,
            ),
        }
        for scope, current in usage.items():
            token_limit, cost_limit = limits[scope]
            tokens = int(current["total_tokens"])
            cost = float(current["estimated_cost"])
            if tokens >= token_limit:
                raise BudgetExceeded(
                    f"{scope} token budget exhausted: {tokens} >= {token_limit}"
                )
            if cost_limit is not None and cost >= cost_limit:
                raise BudgetExceeded(
                    f"{scope} cost budget exhausted: {cost:.6f} >= {cost_limit:.6f}"
                )

    def _record_model_usage(
        self,
        *,
        role: str,
        run_id: str,
        tick_id: str,
        issue_number: int | None,
        usage: dict[str, Any],
    ) -> None:
        self.ledger.append_event(
            "model_usage",
            run_id=run_id,
            tick_id=tick_id,
            state="running",
            operation_key=f"model-usage:{run_id}",
            payload={"role": role, "issue_number": issue_number, "usage": usage},
        )
        self._assert_budgets(tick_id, issue_number)

    def _retain_artifacts(self) -> int:
        active = self.ledger.status().get("active_run")
        active_path = (
            Path(str(active["output_path"])).resolve()
            if isinstance(active, dict) and active.get("output_path")
            else None
        )
        roots = [self.config.runtime_dir / "runs", self.config.runtime_dir / "campaigns"]
        files = [
            item
            for root in roots
            if root.is_dir()
            for item in root.rglob("*")
            if item.is_file() and item.resolve() != active_path
        ]
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        cutoff = time.time() - self.config.storage.artifact_retention_days * 86_400
        removed = 0
        for index, item in enumerate(files):
            if index < self.config.storage.maximum_artifacts and item.stat().st_mtime >= cutoff:
                continue
            item.unlink()
            removed += 1
        if removed:
            self.ledger.append_event("artifacts_retained", payload={"removed": removed})
        return removed

    def _maintenance(self) -> None:
        self._retain_artifacts()
        self.ledger.integrity_check()
        checked: set[str] = set()
        for label, path in (
            ("runtime", self.config.runtime_dir),
            ("project", self.config.project_root),
        ):
            anchor = str(path.resolve().anchor).lower()
            if anchor in checked:
                continue
            checked.add(anchor)
            free = shutil.disk_usage(path).free
            if free < self.config.storage.minimum_free_bytes:
                raise SafetyPause(
                    f"{label} disk free threshold reached: "
                    f"{free} < {self.config.storage.minimum_free_bytes} bytes"
                )

    def request_stop(self) -> None:
        self._stop.set()

    def recover(self) -> dict[str, Any]:
        self.ledger.integrity_check()
        expired = self.ledger.reap_expired_leases(time.time())
        for lease in expired:
            self.ledger.append_event("lease_expired", payload=lease)
        recovered: list[str] = []
        hung: list[str] = []
        with self.ledger.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status IN ('starting','running')"
            ).fetchall()
        for row in rows:
            item = dict(row)
            heartbeat = datetime.fromisoformat(item["heartbeat_at"])
            age = (datetime.now(UTC) - heartbeat).total_seconds()
            if age <= self.config.scheduler.silence_timeout_seconds:
                continue
            if _pid_alive(item["pid"]):
                terminate_pid_tree(
                    int(item["pid"]), self.config.scheduler.shutdown_grace_seconds
                )
                hung.append(item["run_id"])
                self.ledger.append_event(
                    "run_hung",
                    run_id=item["run_id"],
                    tick_id=item["tick_id"],
                    state="failed",
                    operation_key=f"hung:{item['run_id']}",
                    payload={
                        "pid": item["pid"],
                        "heartbeat_age_seconds": age,
                        "silence_timeout_seconds": (
                            self.config.scheduler.silence_timeout_seconds
                        ),
                    },
                )
            self.ledger.finish_run(item["run_id"], "abandoned", -1, "recovered_after_crash")
            self.ledger.append_event(
                "run_recovered",
                run_id=item["run_id"],
                tick_id=item["tick_id"],
                state="failed",
                operation_key=f"recover:{item['run_id']}",
                payload={"previous_status": item["status"], "heartbeat_age_seconds": age},
            )
            recovered.append(item["run_id"])
        if recovered:
            self._checkpoint_recovery_worktree()
        return {
            "expired_leases": len(expired),
            "recovered_runs": recovered,
            "hung_runs": hung,
        }

    def _checkpoint_recovery_worktree(self) -> None:
        try:
            head = git(self.config.project_root, "rev-parse", "HEAD")
            status, digest = self._worktree_evidence()
        except (OSError, subprocess.SubprocessError, PolicyViolation):
            return
        self.ledger.set_checkpoint(
            "recovery_worktree",
            {"head": head, "status": status, "digest": digest} if status else None,
        )

    def _worktree_evidence(self) -> tuple[str, str]:
        status = git(self.config.project_root, "status", "--porcelain=v1", "--untracked-files=all")
        digest = hashlib.sha256(status.encode("utf-8", errors="replace"))
        for relative in sorted(changed_paths(self.config.project_root)):
            digest.update(relative.encode("utf-8", errors="replace"))
            candidate = self.config.project_root / relative
            if candidate.is_symlink():
                digest.update(os.readlink(candidate).encode("utf-8", errors="replace"))
            elif candidate.is_file():
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"<missing-or-directory>")
        return status, digest.hexdigest()

    def tick(self) -> TickOutcome:
        tick_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        if not self.ledger.acquire_lease(
            "coordinator", tick_id, time.time(), self.config.scheduler.lease_seconds
        ):
            return TickOutcome(tick_id, run_id, "waiting", False, "coordinator_lease_busy")
        self.ledger.append_event(
            "tick_started",
            run_id=run_id,
            tick_id=tick_id,
            state="researching",
            operation_key=f"tick:start:{tick_id}",
            payload={},
        )
        baseline = ""
        run_started = False
        issue_number: int | None = None
        try:
            self._maintenance()
            if not self.config.project_root.is_dir():
                raise PolicyViolation(f"Project root does not exist: {self.config.project_root}")
            baseline = git(self.config.project_root, "rev-parse", "HEAD")
            branch = git(self.config.project_root, "branch", "--show-current")
            issue_match = re.fullmatch(r"issue/(\d+)-.+", branch)
            issue_number = int(issue_match.group(1)) if issue_match else None
            self._assert_budgets(tick_id, issue_number)
            initial_status, initial_digest = self._worktree_evidence()
            recovery = self.ledger.get_checkpoint("recovery_worktree")
            if initial_status and (
                not isinstance(recovery, dict)
                or recovery.get("head") != baseline
                or recovery.get("status") != initial_status
                or recovery.get("digest") != initial_digest
            ):
                raise PolicyViolation(
                    "Target worktree is dirty and does not match an interrupted-run checkpoint"
                )
            assert_governance(self.config.project_root, self.config.governance.protected_paths)
            snapshot = reconcile(self.config.project_root)
            self.ledger.set_checkpoint("last_reconciliation", snapshot)
            output_path = self.config.runtime_dir / "runs" / f"{run_id}.jsonl"
            self.ledger.start_run(run_id, tick_id, "coordinator", output_path)
            run_started = True
            result = self.launcher.run_coordinator(run_id, tick_id, output_path)
            reported_issue = (
                result.structured_output.get("issue_number")
                if isinstance(result.structured_output, dict)
                else None
            )
            if isinstance(reported_issue, int):
                issue_number = reported_issue
            self._record_model_usage(
                role="coordinator",
                run_id=run_id,
                tick_id=tick_id,
                issue_number=issue_number,
                usage=result.usage,
            )
            self.ledger.finish_run(
                run_id,
                "succeeded" if result.exit_code == 0 else "failed",
                result.exit_code,
                result.reason,
            )
            if result.exit_code != 0:
                raise RuntimeError(result.stderr_tail or result.stdout_tail or result.reason)
            if "[API Error:" in result.stdout_tail:
                raise RuntimeError(result.stdout_tail[-4000:])
            structured = self.launcher.validated_output(
                result, {"state", "action", "summary", "mutation", "requires_review"}
            )
            if isinstance(structured.get("issue_number"), int):
                issue_number = int(structured["issue_number"])
            final_status = git(self.config.project_root, "status", "--porcelain=v1")
            if final_status:
                raise PolicyViolation("Coordinator tick ended with a dirty worktree")
            assert_governance(
                self.config.project_root, self.config.governance.protected_paths, baseline
            )
            after_snapshot = reconcile(self.config.project_root)
            self.ledger.set_checkpoint("last_reconciliation", after_snapshot)
            repository_changed = git(self.config.project_root, "rev-parse", "HEAD") != baseline
            github_changed = canonical_json(snapshot.get("github")) != canonical_json(
                after_snapshot.get("github")
            )
            observed_mutation = repository_changed or github_changed
            if observed_mutation and not bool(structured["mutation"]):
                raise PolicyViolation("Coordinator reported no mutation but durable state changed")
            self.ledger.append_event(
                "coordinator_completed",
                run_id=run_id,
                tick_id=tick_id,
                state=str(structured["state"]),
                operation_key=f"tick:coordinator:{tick_id}",
                payload={
                    "result": structured,
                    "session_id": result.session_id,
                    "usage": result.usage,
                },
            )

            mutation = bool(structured["mutation"])
            gate_evidence: list[dict[str, Any]] = []
            review_diff = ""
            if mutation:
                review_diff = git(
                    self.config.project_root,
                    "diff",
                    "--no-ext-diff",
                    "--binary",
                    baseline,
                    timeout=120,
                )
                secret_findings = scan_added_secrets(review_diff)
                self.ledger.append_event(
                    "secret_scan",
                    run_id=run_id,
                    tick_id=tick_id,
                    state="verifying",
                    operation_key=f"secret-scan:{tick_id}",
                    payload={"passed": not secret_findings, "findings": secret_findings},
                )
                if secret_findings:
                    raise PolicyViolation("Secret scan blocked added credential-shaped content")
                for gate in self.config.quality_gates:
                    gate_result = run_gate(self.config.project_root, gate)
                    gate_evidence.append(
                        {
                            "name": gate_result.name,
                            "passed": gate_result.passed,
                            "exit_code": gate_result.exit_code,
                            "duration_seconds": gate_result.duration_seconds,
                        }
                    )
                    self.ledger.append_event(
                        "quality_gate",
                        run_id=run_id,
                        tick_id=tick_id,
                        state="verifying",
                        operation_key=f"gate:{tick_id}:{gate.name}",
                        payload=gate_result.__dict__,
                    )
                    if not gate_result.passed:
                        raise RuntimeError(
                            f"quality gate failed: {gate.name}\n{gate_result.output_tail}"
                        )

            review_passed: bool | None = None
            if mutation and self.config.review.enabled:
                if not bool(structured["requires_review"]):
                    raise PolicyViolation("A product mutation cannot opt out of independent review")
                review_passed = self._review(
                    tick_id,
                    {
                        **structured,
                        "issue_number": issue_number,
                        "quality_gates": gate_evidence,
                    },
                    review_diff,
                    implementation_session_id=result.session_id,
                )
                if not review_passed:
                    raise PolicyViolation("Independent review failed")

            final_state = str(structured["state"])
            self.ledger.append_event(
                "tick_finished",
                run_id=run_id,
                tick_id=tick_id,
                state=final_state,
                operation_key=f"tick:finish:{tick_id}",
                payload={"success": True, "review_passed": review_passed},
            )
            self.ledger.set_checkpoint("last_success_epoch", time.time())
            self.ledger.set_checkpoint("recovery_worktree", None)
            return TickOutcome(tick_id, run_id, final_state, True, "success", review_passed)
        except SafetyPause as exc:
            evidence = str(exc)
            self.ledger.fail_active_run(run_id, evidence[-4000:])
            if run_started:
                self._checkpoint_recovery_worktree()
            delay = max(
                self.config.recovery.retry_base_seconds,
                int(self.config.scheduler.loop_minutes * 60),
            )
            self.ledger.set_checkpoint("next_allowed_epoch", time.time() + delay)
            kind = "budget_exhausted" if isinstance(exc, BudgetExceeded) else "safety_pause"
            self.ledger.append_event(
                kind,
                run_id=run_id,
                tick_id=tick_id,
                state="waiting",
                operation_key=f"{kind}:{tick_id}",
                payload={"reason": evidence[-4000:], "retry_after_seconds": delay},
            )
            return TickOutcome(tick_id, run_id, "waiting", False, evidence)
        except (
            QwenUnavailable,
            PolicyViolation,
            RuntimeError,
            ValueError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            evidence = str(exc)
            self.ledger.fail_active_run(run_id, evidence[-4000:])
            if not isinstance(exc, QwenUnavailable) and _TRANSIENT_DEPENDENCY.search(evidence):
                if run_started:
                    self._checkpoint_recovery_worktree()
                delay = self.config.recovery.retry_base_seconds
                self.ledger.set_checkpoint("next_allowed_epoch", time.time() + delay)
                self.ledger.append_event(
                    "dependency_wait",
                    run_id=run_id,
                    tick_id=tick_id,
                    state="waiting",
                    operation_key=f"dependency-wait:{tick_id}",
                    payload={"reason": evidence[-4000:], "retry_after_seconds": delay},
                )
                return TickOutcome(tick_id, run_id, "waiting", False, evidence)
            if isinstance(exc, PolicyViolation):
                self.ledger.append_event(
                    "policy_violation",
                    run_id=run_id,
                    tick_id=tick_id,
                    operation_key=f"policy-violation:{tick_id}",
                    payload={"error": evidence[-4000:]},
                )
            if run_started:
                self._checkpoint_recovery_worktree()
            fp = fingerprint("coordinator_tick", None, evidence)
            failure_scope = (
                "host"
                if isinstance(exc, (QwenUnavailable, OSError, subprocess.SubprocessError))
                else "work"
            )
            count, quarantined = self.ledger.record_failure(
                fp,
                "coordinator_tick",
                evidence,
                self.config.recovery.maximum_identical_failures,
                scope=failure_scope,
                issue_number=issue_number,
            )
            state = "failed" if quarantined else "waiting"
            self.ledger.append_event(
                "tick_failed",
                run_id=run_id,
                tick_id=tick_id,
                state=state,
                operation_key=f"tick:failed:{tick_id}",
                payload={
                    "error": evidence[-4000:],
                    "fingerprint": fp,
                    "attempt": count,
                    "quarantined": quarantined,
                    "scope": failure_scope,
                    "issue_number": issue_number,
                },
            )
            delay = min(
                self.config.recovery.retry_maximum_seconds,
                self.config.recovery.retry_base_seconds * (2 ** max(0, count - 1)),
            )
            self.ledger.set_checkpoint("next_allowed_epoch", time.time() + delay)
            return TickOutcome(tick_id, run_id, state, False, evidence)
        finally:
            released = self.ledger.release_lease("coordinator", tick_id)
            self.ledger.append_event(
                "lease_released",
                run_id=run_id,
                tick_id=tick_id,
                operation_key=f"lease:release:coordinator:{tick_id}",
                payload={"resource": "coordinator", "owner": tick_id, "released": released},
            )

    def _review(
        self,
        tick_id: str,
        summary: dict[str, Any],
        diff: str,
        *,
        implementation_session_id: str | None,
    ) -> bool:
        review_run_id = str(uuid.uuid4())
        path = self.config.runtime_dir / "runs" / f"{review_run_id}.jsonl"
        self.ledger.start_run(review_run_id, tick_id, "reviewer", path)
        try:
            result = self.launcher.run_reviewer(
                review_run_id, tick_id, path, summary=summary, diff=diff
            )
        except (QwenUnavailable, RuntimeError, OSError) as exc:
            self.ledger.fail_active_run(review_run_id, str(exc)[-4000:])
            raise
        issue_value = summary.get("issue_number")
        issue_number = int(issue_value) if isinstance(issue_value, int) else None
        try:
            self._record_model_usage(
                role="reviewer",
                run_id=review_run_id,
                tick_id=tick_id,
                issue_number=issue_number,
                usage=result.usage,
            )
        except SafetyPause as exc:
            self.ledger.fail_active_run(review_run_id, str(exc)[-4000:])
            raise
        self.ledger.finish_run(
            review_run_id,
            "succeeded" if result.exit_code == 0 else "failed",
            result.exit_code,
            result.reason,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                result.stderr_tail or result.stdout_tail or "reviewer process failed"
            )
        if "[API Error:" in result.stdout_tail:
            raise RuntimeError(result.stdout_tail[-4000:])
        if not result.session_id:
            raise PolicyViolation("Independent reviewer did not report a session ID")
        if implementation_session_id and result.session_id == implementation_session_id:
            raise PolicyViolation("Reviewer reused the implementation session")
        review = self.launcher.validated_output(result, {"verdict", "summary", "findings"})
        findings = review.get("findings", [])
        critical = sum(
            1
            for item in findings
            if isinstance(item, dict) and str(item.get("severity", "")).lower() == "critical"
        )
        passed = (
            review.get("verdict") == "pass"
            and critical <= self.config.review.maximum_critical_findings
        )
        self.ledger.append_event(
            "independent_review",
            run_id=review_run_id,
            tick_id=tick_id,
            state="verifying",
            operation_key=f"review:{tick_id}",
            payload={
                "passed": passed,
                "review": review,
                "usage": result.usage,
                "implementation_session_id": implementation_session_id,
                "reviewer_session_id": result.session_id,
            },
        )
        return passed

    def loop(self, once: bool = False, max_seconds: float | None = None) -> None:
        with process_lock(self.config.runtime_dir / "scheduler.lock"):
            self._install_signals()
            stop_at = time.monotonic() + max_seconds if max_seconds is not None else None
            self.ledger.append_event("scheduler_started", payload={"pid": os.getpid()})
            while not self._stop.is_set() and (stop_at is None or time.monotonic() < stop_at):
                self.recover()
                next_allowed = self.ledger.get_checkpoint("next_allowed_epoch")
                now = time.time()
                host_quarantine = self.ledger.failures(quarantined_only=True, scope="host")
                if not host_quarantine and (
                    not isinstance(next_allowed, (int, float)) or now >= next_allowed
                ):
                    self.tick()
                if once:
                    break
                deadline = time.monotonic() + self.config.scheduler.loop_minutes * 60
                if stop_at is not None:
                    deadline = min(deadline, stop_at)
                while not self._stop.is_set() and time.monotonic() < deadline:
                    self._stop.wait(
                        min(self.config.scheduler.heartbeat_seconds, deadline - time.monotonic())
                    )
            self.ledger.append_event("scheduler_stopped", payload={"pid": os.getpid()})

    def _install_signals(self) -> None:
        def stop(_signum: int, _frame: Any) -> None:
            self._stop.set()

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with suppress(ValueError):
                    signal.signal(sig, stop)

    def status(self) -> dict[str, Any]:
        status = self.ledger.status()
        reconciliation = self.ledger.get_checkpoint("last_reconciliation")
        status["reconciliation"] = reconciliation
        created = self.ledger.get_checkpoint("supervisor_created_epoch")
        status["uptime_seconds"] = max(0, time.time() - float(created or time.time()))
        try:
            branch = git(self.config.project_root, "branch", "--show-current")
            head = git(self.config.project_root, "rev-parse", "HEAD")
            dirty = bool(git(self.config.project_root, "status", "--porcelain=v1"))
        except (PolicyViolation, OSError, subprocess.SubprocessError):
            branch, head, dirty = None, None, None
        issue_match = re.fullmatch(r"issue/(\d+)-.+", branch or "")
        issue_number = int(issue_match.group(1)) if issue_match else None
        github = reconciliation.get("github", {}) if isinstance(reconciliation, dict) else {}
        pull_requests = github.get("pull_requests", []) if isinstance(github, dict) else []
        current_pr = next(
            (
                pr
                for pr in pull_requests
                if isinstance(pr, dict) and pr.get("headRefName") == branch
            ),
            None,
        )
        usage = status["usage"]
        estimated_cost = self._cost(usage)
        budget_usage = self._budget_usage(
            status["active_run"].get("tick_id") if status.get("active_run") else None,
            issue_number,
        )
        disk_free = {
            "runtime": shutil.disk_usage(self.config.runtime_dir).free,
            "project": (
                shutil.disk_usage(self.config.project_root).free
                if self.config.project_root.exists()
                else None
            ),
        }
        counts = status["event_counts"]
        campaign_checkpoint = self.ledger.get_checkpoint(ACTIVE_CAMPAIGN_CHECKPOINT_KEY)
        active_campaign = None
        if (
            isinstance(campaign_checkpoint, dict)
            and isinstance(campaign_checkpoint.get("deadline_epoch"), (int, float))
            and campaign_checkpoint["deadline_epoch"] > time.time()
        ):
            active_campaign = {
                "campaign_id": campaign_checkpoint.get("campaign_id"),
                "started_at": campaign_checkpoint.get("started_at"),
                "deadline_epoch": campaign_checkpoint["deadline_epoch"],
                "remaining_seconds": max(
                    0.0, campaign_checkpoint["deadline_epoch"] - time.time()
                ),
            }
        running_jobs = [
            {
                "job_id": job["job_id"],
                "name": job["name"],
                "started_at": job["started_at"],
                "elapsed_seconds": max(0.0, time.time() - float(job["started_epoch"])),
                "max_duration_seconds": job["max_duration_seconds"],
            }
            for job in self.ledger.running_jobs()
        ]
        status.update(
            {
                "active_campaign": active_campaign,
                "running_jobs": running_jobs,
                "current_state": (
                    status["latest_state"].get("state") if status["latest_state"] else None
                ),
                "current_issue": issue_number,
                "git": {"branch": branch, "head": head, "dirty": dirty},
                "current_pr": current_pr,
                "estimated_cost": estimated_cost,
                "budget_usage": budget_usage,
                "budget_limits": {
                    "max_tokens_per_tick": self.config.budgets.max_tokens_per_tick,
                    "max_tokens_per_hour": self.config.budgets.max_tokens_per_hour,
                    "max_tokens_per_day": self.config.budgets.max_tokens_per_day,
                    "max_tokens_per_issue": self.config.budgets.max_tokens_per_issue,
                    "max_cost_per_tick": self.config.budgets.max_cost_per_tick,
                    "max_cost_per_hour": self.config.budgets.max_cost_per_hour,
                    "max_cost_per_day": self.config.budgets.max_cost_per_day,
                    "max_cost_per_issue": self.config.budgets.max_cost_per_issue,
                },
                "storage": {
                    "disk_free_bytes": disk_free,
                    "minimum_free_bytes": self.config.storage.minimum_free_bytes,
                    "artifact_retention_days": self.config.storage.artifact_retention_days,
                    "maximum_artifacts": self.config.storage.maximum_artifacts,
                },
                "healthy": not bool(status["quarantined_host"]),
                "delivery": {
                    "ticks_successful": int(counts.get("tick_finished", 0)),
                    "ticks_failed": int(counts.get("tick_failed", 0)),
                    "issues_completed": int(counts.get("issue_completed", 0)),
                    "pull_requests_merged": int(counts.get("pr_merged", 0)),
                    "self_discoveries": int(counts.get("self_discovery_created", 0)),
                    "recovered_runs": int(counts.get("run_recovered", 0)),
                    "chaos_kills": int(counts.get("chaos_process_killed", 0)),
                },
            }
        )
        return status

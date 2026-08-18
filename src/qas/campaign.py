from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from qas.models import ACTIVE_CAMPAIGN_CHECKPOINT_KEY, utc_now
from qas.runtime import Supervisor


@dataclass(frozen=True)
class CampaignResult:
    campaign_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    chaos_kills: int
    successful_ticks: int
    failed_ticks: int
    reconciled_operations: int
    duplicate_operations: int
    expired_leases: int
    recovered_runs: int
    policy_violations: int
    issues_completed: int
    pull_requests_merged: int
    self_discoveries: int
    total_tokens: int
    estimated_cost: float
    quarantined_work: int
    quarantined_host: int
    human_interventions: int
    passed: bool
    failures: tuple[str, ...]
    report_path: str


def kill_active_agent(supervisor: Supervisor, *, reason: str = "manual chaos") -> int | None:
    status = supervisor.ledger.status()
    active = status.get("active_run")
    if not isinstance(active, dict) or active.get("kind") not in {"coordinator", "reviewer"}:
        return None
    pid = active.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in {0, 128}:
            raise RuntimeError(result.stderr.strip() or f"failed to kill PID {pid}")
    else:
        signal_api: Any = signal
        os.kill(pid, signal_api.SIGKILL)
    supervisor.ledger.append_event(
        "chaos_process_killed",
        run_id=str(active.get("run_id")) if active.get("run_id") else None,
        tick_id=str(active.get("tick_id")) if active.get("tick_id") else None,
        operation_key=f"chaos:kill:{active.get('run_id')}:{pid}",
        payload={"pid": pid, "kind": active.get("kind"), "reason": reason},
    )
    return pid


def active_campaign(supervisor: Supervisor) -> dict[str, Any] | None:
    """Return the durable state of an unfinished campaign, if one exists and
    its original deadline hasn't already passed -- ``None`` otherwise. This
    is what makes a campaign resumable across a full process restart (host
    reboot, OOM kill, crash): the checkpoint survives in the SQLite ledger
    even though everything else about a `run_campaign()` call is local
    Python state that dies with the process.
    """
    state = supervisor.ledger.get_checkpoint(ACTIVE_CAMPAIGN_CHECKPOINT_KEY)
    if not isinstance(state, dict):
        return None
    deadline = state.get("deadline_epoch")
    if not isinstance(deadline, (int, float)) or deadline <= time.time():
        return None
    return state


def run_campaign(
    supervisor: Supervisor,
    *,
    duration_seconds: float,
    chaos_every_seconds: float | None = None,
    minimum_successful_ticks: int = 1,
) -> CampaignResult:
    if duration_seconds <= 0:
        raise ValueError("campaign duration must be positive")
    if chaos_every_seconds is not None and chaos_every_seconds <= 0:
        raise ValueError("chaos interval must be positive")

    now = time.time()
    resumed_state = active_campaign(supervisor)
    if resumed_state is not None:
        # Resume the SAME campaign a prior process (now dead) started:
        # identical campaign_id and original event-count baseline, so the
        # metrics reported at the end genuinely span the full requested
        # wall-clock duration, not just however long this particular process
        # happened to stay up.
        campaign_id = str(resumed_state["campaign_id"])
        started_at = str(resumed_state["started_at"])
        started_epoch = float(resumed_state["started_epoch"])
        before = dict(resumed_state["before"])
        chaos_every_seconds = resumed_state.get("chaos_every_seconds", chaos_every_seconds)
        minimum_successful_ticks = int(
            resumed_state.get("minimum_successful_ticks", minimum_successful_ticks)
        )
        remaining_seconds = max(0.0, float(resumed_state["deadline_epoch"]) - now)
        resumed = True
    else:
        campaign_id = str(uuid.uuid4())
        started_at = utc_now()
        started_epoch = now
        before = supervisor.ledger.status()["event_counts"]
        remaining_seconds = duration_seconds
        supervisor.ledger.set_checkpoint(
            ACTIVE_CAMPAIGN_CHECKPOINT_KEY,
            {
                "campaign_id": campaign_id,
                "started_at": started_at,
                "started_epoch": started_epoch,
                "deadline_epoch": now + duration_seconds,
                "before": before,
                "chaos_every_seconds": chaos_every_seconds,
                "minimum_successful_ticks": minimum_successful_ticks,
            },
        )
        resumed = False

    stop = threading.Event()

    def chaos_worker() -> None:
        if chaos_every_seconds is None:
            return
        while not stop.wait(chaos_every_seconds):
            kill_active_agent(supervisor, reason=f"campaign:{campaign_id}")

    chaos_thread: threading.Thread | None = None
    if chaos_every_seconds is not None and remaining_seconds > 0:
        chaos_thread = threading.Thread(target=chaos_worker, daemon=True)
        chaos_thread.start()

    def deadline_worker() -> None:
        if not stop.wait(remaining_seconds):
            kill_active_agent(supervisor, reason=f"campaign-deadline:{campaign_id}")
            supervisor.request_stop()

    deadline_thread: threading.Thread | None = None
    if remaining_seconds > 0:
        deadline_thread = threading.Thread(target=deadline_worker, daemon=True)
        deadline_thread.start()

    supervisor.ledger.append_event(
        "campaign_resumed" if resumed else "campaign_started",
        operation_key=f"campaign:{'resume' if resumed else 'start'}:{campaign_id}:{now}",
        payload={
            "campaign_id": campaign_id,
            "duration_seconds": duration_seconds,
            "remaining_seconds": remaining_seconds,
            "chaos_every_seconds": chaos_every_seconds,
        },
    )
    try:
        if remaining_seconds > 0:
            supervisor.loop(max_seconds=remaining_seconds)
    finally:
        stop.set()
        if chaos_thread:
            chaos_thread.join(timeout=5)
        if deadline_thread:
            deadline_thread.join(timeout=5)

    after_status = supervisor.status()
    after = after_status["event_counts"]

    def delta(kind: str) -> int:
        return int(after.get(kind, 0)) - int(before.get(kind, 0))

    successful = delta("tick_finished")
    failed = delta("tick_failed")
    failures: list[str] = []
    if successful < minimum_successful_ticks:
        failures.append(f"successful ticks {successful} below required {minimum_successful_ticks}")
    if after_status["quarantined_host"]:
        failures.append("host failure is quarantined")
    campaigns = supervisor.config.runtime_dir / "campaigns"
    campaigns.mkdir(exist_ok=True)
    report_path = campaigns / f"{campaign_id}.json"
    result = CampaignResult(
        campaign_id=campaign_id,
        started_at=started_at,
        finished_at=utc_now(),
        duration_seconds=time.time() - started_epoch,
        chaos_kills=delta("chaos_process_killed"),
        successful_ticks=successful,
        failed_ticks=failed,
        reconciled_operations=delta("operation_reconciled"),
        quarantined_work=len(after_status["quarantined_work"]),
        quarantined_host=len(after_status["quarantined_host"]),
        duplicate_operations=delta("operation_duplicate_detected"),
        expired_leases=delta("lease_expired"),
        recovered_runs=delta("run_recovered"),
        policy_violations=delta("policy_violation"),
        issues_completed=delta("issue_completed"),
        pull_requests_merged=delta("pr_merged"),
        self_discoveries=delta("self_discovery_created"),
        total_tokens=int(after_status["usage"]["total_tokens"]),
        estimated_cost=float(after_status["estimated_cost"]),
        human_interventions=delta("failure_unquarantined"),
        passed=not failures,
        failures=tuple(failures),
        report_path=str(report_path),
    )
    report_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    supervisor.ledger.set_checkpoint(ACTIVE_CAMPAIGN_CHECKPOINT_KEY, None)
    supervisor.ledger.append_event(
        "campaign_finished",
        operation_key=f"campaign:finish:{campaign_id}",
        payload=asdict(result),
    )
    return result

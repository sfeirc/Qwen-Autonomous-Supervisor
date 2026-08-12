from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import write_config

from qas.config import load_config
from qas.models import ProcessResult
from qas.runtime import AlreadyRunning, Supervisor, process_lock


def test_missing_qwen_is_persisted_as_failure(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project, failures=2))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    first = supervisor.tick()
    second = supervisor.tick()
    assert not first.success and first.state == "waiting"
    assert not second.success and second.state == "failed"
    assert supervisor.status()["quarantined_failures"] == 1
    assert not supervisor.status()["leases"]


def test_dirty_worktree_is_refused(tmp_path: Path, git_project: Path) -> None:
    (git_project / "dirty.txt").write_text("mine", encoding="utf-8")
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    outcome = supervisor.tick()
    assert not outcome.success
    assert "dirty" in outcome.reason


def test_recovery_reaps_expired_lease(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    assert supervisor.ledger.acquire_lease("old", "owner", 0, 1)
    result = supervisor.recover()
    assert result["expired_leases"] == 1


class FakeLauncher:
    def __init__(
        self,
        project: Path,
        mutation: bool = False,
        review_pass: bool = True,
        reviewer_session: str = "fresh",
    ) -> None:
        self.project = project
        self.mutation = mutation
        self.review_pass = review_pass
        self.reviewer_session = reviewer_session

    def run_coordinator(self, _run: str, _tick: str, _path: Path) -> ProcessResult:
        if self.mutation:
            (self.project / "feature.txt").write_text("delivered\n", encoding="utf-8")
            subprocess.run(["git", "add", "feature.txt"], cwd=self.project, check=True)
            subprocess.run(
                ["git", "commit", "-m", "feat: deliver feature"],
                cwd=self.project,
                check=True,
                capture_output=True,
            )
        structured = {
            "state": "verifying" if self.mutation else "idle",
            "action": "implement" if self.mutation else "idle",
            "summary": "bounded tick",
            "mutation": self.mutation,
            "requires_review": self.mutation,
        }
        return ProcessResult(0, "success", "a", "b", 1, "", "", "session", structured, {})

    def run_reviewer(
        self, _run: str, _tick: str, _path: Path, *, summary: dict[str, Any], diff: str
    ) -> ProcessResult:
        assert summary["mutation"]
        assert "feature.txt" in diff
        review = {
            "verdict": "pass" if self.review_pass else "fail",
            "summary": "reviewed",
            "findings": (
                []
                if self.review_pass
                else [{"severity": "critical", "title": "unsafe", "evidence": "proof"}]
            ),
        }
        return ProcessResult(0, "success", "a", "b", 1, "", "", self.reviewer_session, review, {})

    @staticmethod
    def validated_output(result: ProcessResult, _required: set[str]) -> dict[str, Any]:
        assert result.structured_output is not None
        return result.structured_output


@pytest.mark.parametrize("mutation", [False, True])
def test_successful_tick_and_independent_review(
    tmp_path: Path, git_project: Path, mutation: bool
) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.launcher = FakeLauncher(git_project, mutation)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert outcome.success
    assert outcome.review_passed is (True if mutation else None)
    kinds = {event["kind"] for event in supervisor.ledger.events(20)}
    assert "tick_finished" in kinds
    assert ("independent_review" in kinds) is mutation


def test_failed_review_blocks_tick(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.launcher = FakeLauncher(git_project, True, False)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert not outcome.success
    assert "review failed" in outcome.reason


def test_reviewer_cannot_reuse_implementation_session(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.launcher = FakeLauncher(  # type: ignore[assignment]
        git_project, True, True, reviewer_session="session"
    )
    outcome = supervisor.tick()
    assert not outcome.success
    assert "reused" in outcome.reason


class SecretLauncher(FakeLauncher):
    def run_coordinator(self, _run: str, _tick: str, _path: Path) -> ProcessResult:
        (self.project / "secrets.py").write_text('API_KEY="123456"\n', encoding="utf-8")
        subprocess.run(["git", "add", "secrets.py"], cwd=self.project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: add accidental secret"],
            cwd=self.project,
            check=True,
            capture_output=True,
        )
        structured = {
            "state": "verifying",
            "action": "implement",
            "summary": "unsafe patch",
            "mutation": True,
            "requires_review": True,
        }
        return ProcessResult(0, "success", "a", "b", 1, "", "", "session", structured, {})


def test_secret_gate_blocks_committed_credential(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.launcher = SecretLauncher(git_project, True)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert not outcome.success
    assert "Secret scan" in outcome.reason


def test_recovery_marks_dead_stale_run(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.ledger.start_run("dead", "tick", "coordinator", tmp_path / "out")
    with supervisor.ledger.connect() as connection:
        connection.execute(
            "UPDATE runs SET status='running',pid=99999999,heartbeat_at='2000-01-01T00:00:00+00:00'"
        )
    result = supervisor.recover()
    assert result["recovered_runs"] == ["dead"]


def test_scheduler_lock_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "scheduler.lock"
    with process_lock(lock), pytest.raises(AlreadyRunning), process_lock(lock):
        raise AssertionError("unreachable")


def test_exact_interrupted_dirty_state_can_resume(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    (git_project / "partial.txt").write_text("from interrupted run\n", encoding="utf-8")
    supervisor._checkpoint_recovery_worktree()
    supervisor.launcher = FakeLauncher(git_project, False)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert not outcome.success
    assert "ended with a dirty worktree" in outcome.reason


def test_changed_dirty_checkpoint_is_refused(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    (git_project / "partial.txt").write_text("agent state\n", encoding="utf-8")
    supervisor._checkpoint_recovery_worktree()
    (git_project / "partial.txt").write_text("user changed it\n", encoding="utf-8")
    outcome = supervisor.tick()
    assert not outcome.success
    assert "does not match" in outcome.reason


def test_work_quarantine_does_not_stop_next_tick(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.ledger.record_failure(
        "work-fp", "quality_gate", "same failure", 1, scope="work", issue_number=12
    )
    calls: list[bool] = []
    monkeypatch.setattr(supervisor, "tick", lambda: calls.append(True))
    supervisor.loop(once=True)
    assert calls == [True]
    assert supervisor.status()["healthy"]


def test_host_quarantine_stops_ticks(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_config(tmp_path, git_project))
    supervisor = Supervisor(config, Path(__file__).parents[1])
    supervisor.ledger.record_failure("host-fp", "qwen_launch", "binary missing", 1, scope="host")
    calls: list[bool] = []
    monkeypatch.setattr(supervisor, "tick", lambda: calls.append(True))
    supervisor.loop(once=True)
    assert calls == []
    assert not supervisor.status()["healthy"]


def test_status_exposes_git_usage_cost_and_current_pr(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "observability:\n  inputCostPerMillion: 2\n  outputCostPerMillion: 4\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "switch", "-c", "issue/42-health"], cwd=git_project, check=True)
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    supervisor.ledger.append_event(
        "coordinator_completed",
        state="verifying",
        payload={"usage": {"input_tokens": 1000, "output_tokens": 500}},
    )
    supervisor.ledger.set_checkpoint(
        "last_reconciliation",
        {"github": {"pull_requests": [{"number": 7, "headRefName": "issue/42-health"}]}},
    )
    status = supervisor.status()
    assert status["current_issue"] == 42
    assert status["current_pr"]["number"] == 7
    assert status["usage"]["total_tokens"] == 1500
    assert status["estimated_cost"] == pytest.approx(0.004)


def test_global_token_budget_blocks_launch_without_quarantine(
    tmp_path: Path, git_project: Path
) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8") + "budgets:\n  maxTokensPerHour: 1\n",
        encoding="utf-8",
    )
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    supervisor.ledger.append_event(
        "model_usage",
        tick_id="previous",
        payload={"role": "coordinator", "usage": {"total_tokens": 1}},
    )
    outcome = supervisor.tick()
    assert outcome.state == "waiting" and not outcome.success
    assert "budget exhausted" in outcome.reason
    assert not supervisor.ledger.failures()
    assert supervisor.ledger.events(1)[0]["kind"] == "lease_released"
    assert any(event["kind"] == "budget_exhausted" for event in supervisor.ledger.events(5))


def test_global_cost_budget_blocks_launch(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """observability:
  inputCostPerMillion: 1
  outputCostPerMillion: 1
budgets:
  maxCostPerHour: 0.000001
""",
        encoding="utf-8",
    )
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    supervisor.ledger.append_event(
        "model_usage",
        tick_id="previous",
        payload={"role": "coordinator", "usage": {"output_tokens": 2}},
    )
    outcome = supervisor.tick()
    assert outcome.state == "waiting"
    assert "cost budget exhausted" in outcome.reason


class ProviderOutageLauncher(FakeLauncher):
    def run_coordinator(self, _run: str, _tick: str, _path: Path) -> ProcessResult:
        return ProcessResult(
            1,
            "process_error",
            "a",
            "b",
            1,
            "",
            "HTTP 429 rate limit",
            None,
            None,
            {"total_tokens": 7},
        )


def test_provider_outage_waits_without_failure_loop(tmp_path: Path, git_project: Path) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    supervisor.launcher = ProviderOutageLauncher(git_project)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert outcome.state == "waiting" and "429" in outcome.reason
    assert not supervisor.ledger.failures()
    kinds = {event["kind"] for event in supervisor.ledger.events(10)}
    assert "dependency_wait" in kinds
    assert supervisor.ledger.usage_totals()["total_tokens"] == 7


class ProviderSoftErrorLauncher(FakeLauncher):
    def run_coordinator(self, _run: str, _tick: str, _path: Path) -> ProcessResult:
        return ProcessResult(
            0,
            "success",
            "a",
            "b",
            1,
            "[API Error: HTTP status 500 service unavailable]",
            "",
            None,
            None,
            {"total_tokens": 0},
        )


def test_provider_error_inside_successful_cli_exit_still_waits(
    tmp_path: Path, git_project: Path
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    supervisor.launcher = ProviderSoftErrorLauncher(git_project)  # type: ignore[assignment]
    outcome = supervisor.tick()
    assert outcome.state == "waiting"
    assert "500" in outcome.reason
    assert not supervisor.ledger.failures()


def test_disk_threshold_pauses_before_model_launch(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    monkeypatch.setattr("qas.runtime.shutil.disk_usage", lambda _path: SimpleNamespace(free=1))
    outcome = supervisor.tick()
    assert outcome.state == "waiting"
    assert "disk free threshold" in outcome.reason
    assert not supervisor.ledger.failures()


def test_artifact_retention_enforces_maximum(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "storage:\n  maximumArtifacts: 1\n  artifactRetentionDays: 30\n",
        encoding="utf-8",
    )
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    runs = supervisor.config.runtime_dir / "runs"
    for index in range(3):
        (runs / f"old-{index}.jsonl").write_text("event\n", encoding="utf-8")
    assert supervisor._retain_artifacts() == 2
    assert len(list(runs.glob("*.jsonl"))) == 1


def test_recovery_kills_live_process_with_stale_heartbeat(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    supervisor.ledger.start_run("hung", "tick", "coordinator", tmp_path / "out")
    with supervisor.ledger.connect() as connection:
        connection.execute(
            "UPDATE runs SET status='running',pid=1234,heartbeat_at='2000-01-01T00:00:00+00:00'"
        )
    killed: list[int] = []
    monkeypatch.setattr("qas.runtime._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "qas.runtime.terminate_pid_tree", lambda pid, _grace: killed.append(pid)
    )
    result = supervisor.recover()
    assert killed == [1234]
    assert result["hung_runs"] == ["hung"]
    assert result["recovered_runs"] == ["hung"]

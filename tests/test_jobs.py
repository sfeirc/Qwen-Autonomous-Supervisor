from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

import pytest

from qas.db import Ledger
from qas.jobs import JobError, _pid_alive, list_jobs, poll_job, start_job


def _py(code: str) -> str:
    """Build a safe `python3 -c "..."` fragment as a single shell-quoted
    string, for use inside a larger shell command line (see test_jobs.py's
    shell-operator test for why `command` itself must stay a raw string)."""
    return shlex.join([sys.executable, "-c", code])


def _wait_until_terminal(ledger: Ledger, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    record = poll_job(ledger, job_id)
    while record["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        record = poll_job(ledger, job_id)
    return record


def test_quick_succeeding_job_reports_real_exit_code(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    job_id = start_job(
        ledger,
        jobs_dir,
        name="quick-ok",
        command=_py("print('hello from job')"),
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=2,
    )
    record = _wait_until_terminal(ledger, job_id)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0
    assert "hello from job" in Path(record["log_path"]).read_text(encoding="utf-8")


def test_failing_job_reports_real_nonzero_exit_code(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    job_id = start_job(
        ledger,
        jobs_dir,
        name="quick-fail",
        command=_py("import sys; sys.exit(3)"),
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=2,
    )
    record = _wait_until_terminal(ledger, job_id)
    assert record["status"] == "failed"
    assert record["exit_code"] == 3


def test_job_exceeding_max_duration_is_expired_and_actually_killed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    job_id = start_job(
        ledger,
        jobs_dir,
        name="overrun",
        command=_py("import time; time.sleep(30)"),
        cwd=tmp_path,
        max_duration_seconds=1,
        max_concurrent=2,
    )
    time.sleep(1.2)
    record = poll_job(ledger, job_id)
    assert record["status"] == "expired"
    assert record["exit_code"] is None
    # Real kill, not just a status flip: the process must actually be dead
    # (or an unreaped zombie -- this sandbox's PID 1 doesn't reap orphans,
    # confirmed directly; kill(pid, 0) alone stays "alive" against a zombie,
    # so _pid_alive's /proc-state check is what actually proves this).
    assert not _pid_alive(record["pid"])


def test_duplicate_running_name_is_rejected(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    start_job(
        ledger,
        jobs_dir,
        name="dup",
        command=_py("import time; time.sleep(5)"),
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=5,
    )
    with pytest.raises(JobError, match="already running"):
        start_job(
            ledger,
            jobs_dir,
            name="dup",
            command=_py("print('should not start')"),
            cwd=tmp_path,
            max_duration_seconds=10,
            max_concurrent=5,
        )


def test_max_concurrent_jobs_enforced(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    for i in range(2):
        start_job(
            ledger,
            jobs_dir,
            name=f"slot-{i}",
            command=_py("import time; time.sleep(5)"),
            cwd=tmp_path,
            max_duration_seconds=10,
            max_concurrent=2,
        )
    with pytest.raises(JobError, match="max concurrent"):
        start_job(
            ledger,
            jobs_dir,
            name="slot-2",
            command=_py("print('should not start')"),
            cwd=tmp_path,
            max_duration_seconds=10,
            max_concurrent=2,
        )


def test_job_state_survives_a_simulated_process_restart(tmp_path: Path) -> None:
    """A job launched by one qas process must still be pollable to a correct
    terminal state by a completely separate, later Ledger instance pointed at
    the same DB file -- the same restart-durability guarantee proven for
    campaigns earlier: the launching process does not need to still be alive
    for its job's real outcome to be recorded correctly."""
    db_path = tmp_path / "state.db"
    jobs_dir = tmp_path / "jobs"
    first_process_ledger = Ledger(db_path)
    job_id = start_job(
        first_process_ledger,
        jobs_dir,
        name="outlives-launcher",
        command=_py("import time; time.sleep(0.3); print('done')"),
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=2,
    )
    del first_process_ledger  # simulate the launching process going away

    second_process_ledger = Ledger(db_path)
    record = _wait_until_terminal(second_process_ledger, job_id)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0


def test_command_shell_operators_are_honored_not_quoted_away(tmp_path: Path) -> None:
    """`command` must be run as a genuine shell command line, preserving
    operators like `&&`. Caught during manual verification: an earlier
    version shlex-split the command into an argv list and shlex-joined it
    back together, which silently turns `&&` into a literal quoted argument
    (`sleep 2 '&&' echo done`) instead of a real shell operator -- confirmed
    real, not hypothetical, by actually running it and seeing `sleep` fail
    with "invalid time interval '&&'"."""
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    marker = tmp_path / "second-command-ran"
    job_id = start_job(
        ledger,
        jobs_dir,
        name="operators",
        command=f"{_py('print(1)')} && touch {shlex.quote(str(marker))}",
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=2,
    )
    record = _wait_until_terminal(ledger, job_id)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0
    assert marker.is_file()


def test_list_jobs_reports_known_jobs(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.db")
    jobs_dir = tmp_path / "jobs"
    job_id = start_job(
        ledger,
        jobs_dir,
        name="listed",
        command=_py("print('ok')"),
        cwd=tmp_path,
        max_duration_seconds=10,
        max_concurrent=2,
    )
    _wait_until_terminal(ledger, job_id)
    jobs = list_jobs(ledger)
    assert any(job["job_id"] == job_id for job in jobs)

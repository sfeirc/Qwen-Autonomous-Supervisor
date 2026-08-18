"""Durable, async long-running job subsystem.

A coordinator tick is bounded (maxWallTime/maxToolCalls/maxSessionTurns) and
the coordinator's own Qwen process ends at the tick boundary -- there was
previously no way to start something that legitimately runs for hours (a
training run, a long data pipeline, anything else) without either blocking a
tick on it or losing track of it once the tick ends. This module makes such a
job durable: its identity, status, and eventual result survive independently
of any one qas process's lifetime, the same way campaign state (qas.campaign)
and tick/lease state already do.

Honest scope: this is generic long-running-job infrastructure, not GPU- or
ML-specific code. It has not been exercised against real GPU training -- this
environment has no GPU at all (confirmed: no nvidia-smi, no CUDA, no NVIDIA
PCI device) -- and it adds no ML research capability (hypothesis generation,
benchmark evaluation). It only removes the "a tick can't outlive its own
bounded wall-time" ceiling that would otherwise make any long job, training or
not, impossible to track at all.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from qas.db import Ledger
from qas.process import terminate_pid_tree

_TERMINATE_GRACE_SECONDS = 20


class JobError(RuntimeError):
    pass


def start_job(
    ledger: Ledger,
    jobs_dir: Path,
    *,
    name: str,
    command: str,
    cwd: Path,
    max_duration_seconds: int,
    max_concurrent: int,
    results_path: str | None = None,
) -> str:
    """`command` is a raw shell command line (run via `/bin/sh -c`), not an
    argv list: it is deliberately NOT split/rejoined through shlex, because
    doing so silently strips the meaning of shell operators like `&&`, `|`,
    `;` and redirects (shlex tokenizes them as plain words, and rejoining
    re-quotes them as literal arguments -- confirmed this breaks a real
    `sleep 2 && echo done` command during development). Callers building a
    command from parts that may contain spaces/special characters should use
    `shlex.join([...])` themselves to produce a safe fragment, then compose
    it with real shell operators as needed.
    """
    if not command.strip():
        raise JobError("job command must not be empty")
    if max_duration_seconds <= 0:
        raise JobError("max_duration_seconds must be positive")

    existing = ledger.get_running_job_by_name(name)
    if existing is not None:
        raise JobError(f"a job named {name!r} is already running (job_id={existing['job_id']})")
    running = ledger.count_running_jobs()
    if running >= max_concurrent:
        raise JobError(f"max concurrent jobs reached ({running}/{max_concurrent})")

    job_id = f"{name}-{int(time.time() * 1000)}"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "output.log"
    exit_code_path = job_dir / "exit_code"

    # Once this call returns, `qas job start`'s own process exits and the
    # job is reparented to PID 1 -- a later `qas job status` call cannot
    # waitpid() it to learn the real exit code, so the job must self-report
    # it to a file as its very last action.
    wrapped = f"{command}; echo $? > {shlex.quote(str(exit_code_path))}"
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(  # noqa: S602
            ["/bin/sh", "-c", wrapped],
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    ledger.start_job(
        job_id=job_id,
        name=name,
        command=command,
        cwd=str(cwd),
        pid=process.pid,
        started_epoch=time.time(),
        max_duration_seconds=max_duration_seconds,
        log_path=str(log_path),
        exit_code_path=str(exit_code_path),
        results_path=results_path,
    )
    return job_id


def _pid_alive(pid: int) -> bool:
    """True only if `pid` is genuinely running, not merely present in the
    process table as an unreaped zombie. A detached job's parent becomes
    PID 1 after this process exits; some minimal/containerized init systems
    do not reap orphaned zombies promptly, and `kill(pid, 0)` alone still
    succeeds against a zombie (the kernel keeps a process-table entry until
    it is reaped) -- checked directly against a real orphaned zombie in this
    environment during development, not assumed. Checking /proc's state
    field is the reliable way to tell "exited, awaiting reap" from "actually
    running" on Linux; falls back to the plain kill(pid, 0) check where
    /proc is unavailable.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            # Format: "pid (comm) state ...". comm can contain spaces/parens,
            # so split on the LAST ')' to find where the state field starts.
            content = stat_path.read_text(encoding="utf-8")
            state = content.rsplit(")", 1)[-1].split()[0]
            if state == "Z":
                return False
        except (OSError, IndexError):
            pass
    return True


def poll_job(ledger: Ledger, job_id_or_name: str) -> dict[str, Any]:
    record = ledger.get_job(job_id_or_name)
    if record is None:
        raise JobError(f"no such job: {job_id_or_name}")
    if record["status"] != "running":
        return record

    exit_code_path = Path(record["exit_code_path"])
    if exit_code_path.is_file():
        raw = exit_code_path.read_text(encoding="utf-8").strip()
        try:
            exit_code = int(raw)
        except ValueError:
            exit_code = None
        status = "succeeded" if exit_code == 0 else "failed"
        ledger.finish_job(record["job_id"], status=status, exit_code=exit_code)
        return ledger.get_job(record["job_id"]) or record

    elapsed = time.time() - float(record["started_epoch"])
    if elapsed > float(record["max_duration_seconds"]):
        if _pid_alive(int(record["pid"])):
            terminate_pid_tree(int(record["pid"]), _TERMINATE_GRACE_SECONDS)
        ledger.finish_job(record["job_id"], status="expired", exit_code=None)
        return ledger.get_job(record["job_id"]) or record

    if not _pid_alive(int(record["pid"])):
        # Gone without ever writing an exit code (e.g. OOM-killed) -- report
        # honestly as failed/unknown, never silently "still running".
        ledger.finish_job(record["job_id"], status="failed", exit_code=None)
        return ledger.get_job(record["job_id"]) or record

    return record


def list_jobs(ledger: Ledger, limit: int = 50) -> list[dict[str, Any]]:
    return ledger.list_jobs(limit)


__all__ = ["JobError", "start_job", "poll_job", "list_jobs"]

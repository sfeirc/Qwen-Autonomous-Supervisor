from __future__ import annotations

import os
import sys
from pathlib import Path

from qas.process import duration_seconds, run_process


def test_duration_parser() -> None:
    assert duration_seconds("1.5h") == 5400
    assert duration_seconds("2m") == 120
    assert duration_seconds("3") == 3


def test_stream_json_and_structured_output(tmp_path: Path) -> None:
    code = (
        "import json; "
        "print(json.dumps({'type':'system','session_id':'abc'}), flush=True); "
        "print(json.dumps({'type':'result','result':{'state':'idle'}}), flush=True)"
    )
    result = run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        output_path=tmp_path / "run.jsonl",
        env=dict(os.environ),
        wall_timeout_seconds=5,
        silence_timeout_seconds=5,
        shutdown_grace_seconds=1,
        heartbeat_interval_seconds=1,
    )
    assert result.exit_code == 0
    assert result.session_id == "abc"
    assert result.structured_output == {"state": "idle"}
    assert (tmp_path / "run.jsonl").is_file()


def test_silence_timeout_kills_process(tmp_path: Path) -> None:
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        output_path=tmp_path / "timeout.jsonl",
        env=dict(os.environ),
        wall_timeout_seconds=5,
        silence_timeout_seconds=1,
        shutdown_grace_seconds=1,
        heartbeat_interval_seconds=1,
    )
    assert result.reason == "silence_timeout"
    assert result.duration_seconds < 5


def test_lost_lease_kills_process(tmp_path: Path) -> None:
    result = run_process(
        [sys.executable, "-c", "import time; print('alive', flush=True); time.sleep(10)"],
        cwd=tmp_path,
        output_path=tmp_path / "lease.jsonl",
        env=dict(os.environ),
        wall_timeout_seconds=5,
        silence_timeout_seconds=5,
        shutdown_grace_seconds=1,
        heartbeat_interval_seconds=1,
        on_heartbeat=lambda _session: False,
    )
    assert result.reason == "lease_lost"


def test_large_input_uses_stdin(tmp_path: Path) -> None:
    code = "import sys; data=sys.stdin.read(); print(len(data), flush=True)"
    payload = "x" * 100_000
    result = run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        output_path=tmp_path / "stdin.jsonl",
        env=dict(os.environ),
        wall_timeout_seconds=5,
        silence_timeout_seconds=5,
        shutdown_grace_seconds=1,
        heartbeat_interval_seconds=1,
        stdin_text=payload,
    )
    assert result.exit_code == 0
    assert "100000" in result.stdout_tail


def test_jsonl_output_rotates_at_size_limit(tmp_path: Path) -> None:
    output = tmp_path / "rotating.jsonl"
    code = "for i in range(20): print('x' * 100, flush=True)"
    result = run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        output_path=output,
        env=dict(os.environ),
        wall_timeout_seconds=5,
        silence_timeout_seconds=5,
        shutdown_grace_seconds=1,
        heartbeat_interval_seconds=1,
        max_log_bytes=400,
        log_backup_count=2,
    )
    assert result.exit_code == 0
    assert output.is_file()
    assert (tmp_path / "rotating.jsonl.1").is_file()
    assert not (tmp_path / "rotating.jsonl.3").exists()

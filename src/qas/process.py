from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from qas.models import ProcessResult, canonical_json, utc_now


def duration_seconds(value: str) -> float:
    value = value.strip().lower()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if value[-1:] in units:
        return float(value[:-1]) * units[value[-1]]
    return float(value)


def _terminate_tree(process: subprocess.Popen[str], grace_seconds: int) -> None:
    os_api: Any = os
    signal_api: Any = signal
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except OSError:
            process.terminate()
    else:
        os_api.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
        )
    else:
        os_api.killpg(process.pid, signal_api.SIGKILL)
    process.wait(timeout=max(5, grace_seconds))


def terminate_pid_tree(pid: int, grace_seconds: int) -> None:
    """Terminate a process group previously created by run_process."""
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
        )
        return
    os_api: Any = os
    signal_api: Any = signal
    try:
        os_api.killpg(pid, signal_api.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    with suppress(OSError):
        os_api.killpg(pid, signal_api.SIGKILL)


def _rotate_output(path: Path, backup_count: int) -> None:
    oldest = path.with_name(f"{path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def _reader(stream: TextIO, source: str, messages: queue.Queue[tuple[str, str, float]]) -> None:
    try:
        for line in iter(stream.readline, ""):
            messages.put((source, line.rstrip("\r\n"), time.monotonic()))
    finally:
        stream.close()


def _writer(stream: TextIO, content: str) -> None:
    try:
        stream.write(content)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()


def _extract(event: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    session = event.get("session_id")
    session_id = session if isinstance(session, str) else None
    structured: dict[str, Any] | None = None
    for key in ("structured_output", "result"):
        candidate = event.get(key)
        if isinstance(candidate, dict):
            structured = candidate
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                candidate = block.get("structured_output") or block.get("input")
                if block.get("type") in {"structured_output", "tool_use"} and isinstance(
                    candidate, dict
                ):
                    structured = candidate
    usage = event.get("usage")
    return session_id, structured, usage if isinstance(usage, dict) else {}


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    output_path: Path,
    env: dict[str, str],
    wall_timeout_seconds: float,
    silence_timeout_seconds: int,
    shutdown_grace_seconds: int,
    on_started: Callable[[int], None] | None = None,
    on_heartbeat: Callable[[str | None], bool | None] | None = None,
    heartbeat_interval_seconds: int = 15,
    stdin_text: str | None = None,
    max_log_bytes: int = 50 * 1024 * 1024,
    log_backup_count: int = 3,
) -> ProcessResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_text = utc_now()
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # type: ignore[attr-defined]
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    if on_started:
        on_started(process.pid)
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("failed to capture child process streams")
    messages: queue.Queue[tuple[str, str, float]] = queue.Queue(maxsize=1000)
    threads = [
        threading.Thread(target=_reader, args=(process.stdout, "stdout", messages), daemon=True),
        threading.Thread(target=_reader, args=(process.stderr, "stderr", messages), daemon=True),
    ]
    for thread in threads:
        thread.start()
    writer: threading.Thread | None = None
    if stdin_text is not None:
        if process.stdin is None:
            raise RuntimeError("failed to open child process input")
        writer = threading.Thread(target=_writer, args=(process.stdin, stdin_text), daemon=True)
        writer.start()

    stdout_tail: deque[str] = deque(maxlen=200)
    stderr_tail: deque[str] = deque(maxlen=200)
    last_activity = started
    last_heartbeat = 0.0
    session_id: str | None = None
    structured: dict[str, Any] | None = None
    usage: dict[str, Any] = {}
    reason = "exit"

    log = output_path.open("a", encoding="utf-8", newline="\n")
    log_bytes = output_path.stat().st_size

    def write_log(line: str) -> None:
        nonlocal log, log_bytes
        encoded_size = len(line.encode("utf-8"))
        if log_bytes and log_bytes + encoded_size > max_log_bytes:
            log.close()
            _rotate_output(output_path, log_backup_count)
            log = output_path.open("a", encoding="utf-8", newline="\n")
            log_bytes = 0
        log.write(line)
        log.flush()
        log_bytes += encoded_size

    try:
        write_log(
            canonical_json(
                {"type": "supervisor_start", "timestamp": started_text, "pid": process.pid}
            )
            + "\n"
        )
        while (
            process.poll() is None
            or any(thread.is_alive() for thread in threads)
            or not messages.empty()
        ):
            now = time.monotonic()
            if process.poll() is None and now - started >= wall_timeout_seconds:
                reason = "wall_timeout"
                _terminate_tree(process, shutdown_grace_seconds)
            elif process.poll() is None and now - last_activity >= silence_timeout_seconds:
                reason = "silence_timeout"
                _terminate_tree(process, shutdown_grace_seconds)
            try:
                source, line, emitted = messages.get(timeout=0.2)
            except queue.Empty:
                source = line = ""
                emitted = now
            if line:
                last_activity = emitted
                target = stdout_tail if source == "stdout" else stderr_tail
                target.append(line[-16_000:])
                write_log(
                    canonical_json({"type": source, "timestamp": utc_now(), "line": line})
                    + "\n"
                )
                if source == "stdout":
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = None
                    if isinstance(event, dict):
                        found_session, found_structured, found_usage = _extract(event)
                        session_id = found_session or session_id
                        structured = found_structured or structured
                        if found_usage:
                            usage = found_usage
            if on_heartbeat and now - last_heartbeat >= heartbeat_interval_seconds:
                lease_ok = on_heartbeat(session_id)
                if lease_ok is False and process.poll() is None:
                    reason = "lease_lost"
                    _terminate_tree(process, shutdown_grace_seconds)
                last_heartbeat = now
    finally:
        log.close()

    exit_code = process.wait()
    for thread in threads:
        thread.join(timeout=1)
    if writer is not None:
        writer.join(timeout=1)
    finished = time.monotonic()
    if reason == "exit":
        reason = "success" if exit_code == 0 else "process_error"
    return ProcessResult(
        exit_code=exit_code,
        reason=reason,
        started_at=started_text,
        finished_at=utc_now(),
        duration_seconds=finished - started,
        stdout_tail="\n".join(stdout_tail),
        stderr_tail="\n".join(stderr_tail),
        session_id=session_id,
        structured_output=structured,
        usage=usage,
    )

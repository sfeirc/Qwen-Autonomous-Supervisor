from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qas.models import canonical_json, utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    run_id TEXT,
    tick_id TEXT,
    state TEXT,
    operation_key TEXT UNIQUE,
    payload TEXT NOT NULL CHECK(json_valid(payload))
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    tick_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    session_id TEXT,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    reason TEXT,
    output_path TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    resource TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    issue_number INTEGER,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    fingerprint TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    count INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    quarantined INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'work',
    issue_number INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK(json_valid(value)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    operation_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    external_id TEXT,
    request TEXT NOT NULL CHECK(json_valid(request)),
    evidence TEXT CHECK(evidence IS NULL OR json_valid(evidence))
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    command TEXT NOT NULL CHECK(json_valid(command)),
    cwd TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','expired')),
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    max_duration_seconds INTEGER NOT NULL,
    log_path TEXT NOT NULL,
    exit_code_path TEXT NOT NULL,
    results_path TEXT
);

CREATE INDEX IF NOT EXISTS jobs_name_idx ON jobs(name);
"""


class LedgerIntegrityError(RuntimeError):
    """Raised when SQLite reports corruption or an unreadable ledger."""


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.exists() and self.path.stat().st_size:
                self.integrity_check()
            with self.connect() as connection:
                connection.executescript(SCHEMA)
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(failures)").fetchall()
                }
                if "scope" not in columns:
                    connection.execute(
                        "ALTER TABLE failures ADD COLUMN scope TEXT NOT NULL DEFAULT 'work'"
                    )
                if "issue_number" not in columns:
                    connection.execute("ALTER TABLE failures ADD COLUMN issue_number INTEGER")
            self.integrity_check()
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError(f"SQLite integrity check failed: {exc}") from exc

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    def integrity_check(self) -> None:
        try:
            with self.connect() as connection:
                rows = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise LedgerIntegrityError(f"SQLite integrity check failed: {exc}") from exc
        messages = [str(row[0]) for row in rows]
        if messages != ["ok"]:
            raise LedgerIntegrityError(
                "SQLite integrity check failed: " + "; ".join(messages[:20])
            )

    def append_event(
        self,
        kind: str,
        *,
        run_id: str | None = None,
        tick_id: str | None = None,
        state: str | None = None,
        operation_key: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO events(timestamp,kind,run_id,tick_id,state,operation_key,payload) VALUES(?,?,?,?,?,?,?)",
                (
                    utc_now(),
                    kind,
                    run_id,
                    tick_id,
                    state,
                    operation_key,
                    canonical_json(payload or {}),
                ),
            )
            return (
                int(cursor.lastrowid) if cursor.rowcount and cursor.lastrowid is not None else None
            )

    def start_run(self, run_id: str, tick_id: str, kind: str, output_path: Path) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id,tick_id,kind,status,started_at,heartbeat_at,output_path) VALUES(?,?,?,?,?,?,?)",
                (run_id, tick_id, kind, "starting", now, now, str(output_path)),
            )

    def set_run_running(self, run_id: str, pid: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status='running', pid=?, heartbeat_at=? WHERE run_id=? AND status='starting'",
                (pid, utc_now(), run_id),
            )

    def heartbeat_run(self, run_id: str, session_id: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET heartbeat_at=?, session_id=COALESCE(?,session_id) WHERE run_id=? AND status IN ('starting','running')",
                (utc_now(), session_id, run_id),
            )

    def finish_run(self, run_id: str, status: str, exit_code: int, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?,exit_code=?,reason=?,finished_at=?,heartbeat_at=? WHERE run_id=?",
                (status, exit_code, reason, utc_now(), utc_now(), run_id),
            )

    def fail_active_run(self, run_id: str, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE runs SET status='failed',exit_code=-1,reason=?,finished_at=?,heartbeat_at=?
                WHERE run_id=? AND status IN ('starting','running')""",
                (reason, utc_now(), utc_now(), run_id),
            )

    def acquire_lease(
        self,
        resource: str,
        owner: str,
        now_epoch: float,
        ttl_seconds: int,
        issue_number: int | None = None,
    ) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM leases WHERE resource=? AND expires_epoch<=?", (resource, now_epoch)
            )
            try:
                connection.execute(
                    "INSERT INTO leases(resource,owner,issue_number,acquired_at,heartbeat_at,expires_epoch) VALUES(?,?,?,?,?,?)",
                    (resource, owner, issue_number, now, now, now_epoch + ttl_seconds),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return False
            connection.execute("COMMIT")
            return True

    def renew_lease(self, resource: str, owner: str, now_epoch: float, ttl_seconds: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE leases SET heartbeat_at=?,expires_epoch=? WHERE resource=? AND owner=? AND expires_epoch>?",
                (utc_now(), now_epoch + ttl_seconds, resource, owner, now_epoch),
            )
            return cursor.rowcount == 1

    def release_lease(self, resource: str, owner: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE resource=? AND owner=?", (resource, owner)
            )
            return cursor.rowcount == 1

    def reap_expired_leases(self, now_epoch: float) -> list[dict[str, Any]]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM leases WHERE expires_epoch<=?", (now_epoch,)
            ).fetchall()
            connection.execute("DELETE FROM leases WHERE expires_epoch<=?", (now_epoch,))
            connection.execute("COMMIT")
        return [dict(row) for row in rows]

    def record_failure(
        self,
        fp: str,
        operation: str,
        evidence: str,
        threshold: int,
        *,
        scope: str = "work",
        issue_number: int | None = None,
    ) -> tuple[int, bool]:
        if scope not in {"host", "work"}:
            raise ValueError("failure scope must be host or work")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT count FROM failures WHERE fingerprint=?", (fp,)
            ).fetchone()
            count = (int(row["count"]) if row else 0) + 1
            quarantined = count >= threshold
            connection.execute(
                """INSERT INTO failures(
                fingerprint,operation,count,first_seen,last_seen,quarantined,evidence,scope,issue_number
                ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
                count=excluded.count,last_seen=excluded.last_seen,quarantined=excluded.quarantined,
                evidence=excluded.evidence,scope=excluded.scope,issue_number=excluded.issue_number""",
                (
                    fp,
                    operation,
                    count,
                    now,
                    now,
                    int(quarantined),
                    evidence[-4000:],
                    scope,
                    issue_number,
                ),
            )
            connection.execute("COMMIT")
        return count, quarantined

    def start_job(
        self,
        *,
        job_id: str,
        name: str,
        command: str,
        cwd: str,
        pid: int,
        started_epoch: float,
        max_duration_seconds: int,
        log_path: str,
        exit_code_path: str,
        results_path: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO jobs(
                job_id,name,command,cwd,status,pid,started_at,started_epoch,
                max_duration_seconds,log_path,exit_code_path,results_path
                ) VALUES(?,?,?,?,'running',?,?,?,?,?,?,?)""",
                (
                    job_id,
                    name,
                    canonical_json(command),
                    cwd,
                    pid,
                    utc_now(),
                    started_epoch,
                    max_duration_seconds,
                    log_path,
                    exit_code_path,
                    results_path,
                ),
            )

    def get_job(self, job_id_or_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
                (job_id_or_name,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE name=? ORDER BY started_at DESC LIMIT 1",
                    (job_id_or_name,),
                ).fetchone()
        return dict(row) if row else None

    def get_running_job_by_name(self, name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE name=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (name,),
            ).fetchone()
        return dict(row) if row else None

    def count_running_jobs(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status='running'"
            ).fetchone()
        return int(row["count"]) if row else 0

    def finish_job(self, job_id: str, status: str, exit_code: int | None) -> None:
        if status not in {"succeeded", "failed", "expired"}:
            raise ValueError(f"invalid terminal job status: {status}")
        with self.connect() as connection:
            connection.execute(
                """UPDATE jobs SET status=?,exit_code=?,finished_at=?
                WHERE job_id=? AND status='running'""",
                (status, exit_code, utc_now(), job_id),
            )

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status='running' ORDER BY started_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_checkpoint(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, canonical_json(value), utc_now()),
            )

    def get_checkpoint(self, key: str) -> Any | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def begin_operation(self, key: str, kind: str, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO operations(
                operation_key,kind,status,created_at,updated_at,request
                ) VALUES(?,?,'pending',?,?,?)""",
                (key, kind, now, now, canonical_json(request)),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?", (key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("operation disappeared after insertion")
        return dict(row)

    def complete_operation(
        self, key: str, external_id: str | None, evidence: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE operations SET status='completed',updated_at=?,external_id=?,evidence=?
                WHERE operation_key=?""",
                (utc_now(), external_id, canonical_json(evidence), key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown operation: {key}")

    def fail_operation(self, key: str, evidence: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE operations SET status='failed',updated_at=?,evidence=?
                WHERE operation_key=? AND status!='completed'""",
                (utc_now(), canonical_json(evidence), key),
            )

    def operation(self, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def pending_operations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operations WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def failures(
        self, quarantined_only: bool = False, scope: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if quarantined_only:
            clauses.append("quarantined=1")
        if scope:
            clauses.append("scope=?")
            parameters.append(scope)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM failures {where} ORDER BY last_seen DESC",  # noqa: S608
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def unquarantine(self, fp: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE failures SET quarantined=0,count=0,last_seen=? WHERE fingerprint=? AND quarantined=1",
                (utc_now(), fp),
            )
            return cursor.rowcount == 1

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            latest = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
            latest_state = connection.execute(
                "SELECT state,kind,timestamp FROM events WHERE state IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest_success = connection.execute(
                "SELECT * FROM events WHERE kind='tick_finished' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest_failure = connection.execute(
                "SELECT * FROM events WHERE kind='tick_failed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            active_run = connection.execute(
                """SELECT * FROM runs WHERE status IN ('starting','running')
                ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            leases = connection.execute("SELECT * FROM leases ORDER BY resource").fetchall()
            counts = connection.execute(
                "SELECT status,COUNT(*) AS count FROM runs GROUP BY status"
            ).fetchall()
            event_counts = connection.execute(
                "SELECT kind,COUNT(*) AS count FROM events GROUP BY kind"
            ).fetchall()
            quarantined = connection.execute(
                "SELECT COUNT(*) AS count FROM failures WHERE quarantined=1"
            ).fetchone()
        return {
            "latest_event": dict(latest) if latest else None,
            "latest_state": dict(latest_state) if latest_state else None,
            "last_success": dict(latest_success) if latest_success else None,
            "last_failure": dict(latest_failure) if latest_failure else None,
            "latest_run": dict(run) if run else None,
            "active_run": dict(active_run) if active_run else None,
            "leases": [dict(item) for item in leases],
            "run_counts": {item["status"]: item["count"] for item in counts},
            "event_counts": {item["kind"]: item["count"] for item in event_counts},
            "quarantined_failures": quarantined["count"] if quarantined else 0,
            "quarantined_work": self.failures(quarantined_only=True, scope="work"),
            "quarantined_host": self.failures(quarantined_only=True, scope="host"),
            "pending_operations": self.pending_operations(),
            "usage": self.usage_totals(),
        }

    def usage_totals(
        self,
        *,
        since: str | None = None,
        tick_id: str | None = None,
        issue_number: int | None = None,
    ) -> dict[str, int]:
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}
        clauses = [
            "kind IN ('model_usage','coordinator_completed','independent_review')"
        ]
        parameters: list[Any] = []
        if since is not None:
            clauses.append("timestamp>=?")
            parameters.append(since)
        if tick_id is not None:
            clauses.append("tick_id=?")
            parameters.append(tick_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT kind,tick_id,payload FROM events WHERE "  # noqa: S608
                + " AND ".join(clauses),
                parameters,
            ).fetchall()

        model_roles: set[tuple[str | None, str]] = set()
        issue_ticks: set[str | None] = set()
        for row in rows:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            event_issue = payload.get("issue_number")
            result = payload.get("result")
            if event_issue is None and isinstance(result, dict):
                event_issue = result.get("issue_number")
            if issue_number is not None and event_issue == issue_number:
                issue_ticks.add(row["tick_id"])
            if row["kind"] == "model_usage":
                model_roles.add((row["tick_id"], str(payload.get("role", "coordinator"))))

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = key.lower().replace("-", "_")
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        if normalized in {"input_tokens", "inputtokens", "prompt_tokens"}:
                            totals["input_tokens"] += int(item)
                        elif normalized in {
                            "output_tokens",
                            "outputtokens",
                            "completion_tokens",
                        }:
                            totals["output_tokens"] += int(item)
                        elif normalized in {"total_tokens", "totaltokens"}:
                            totals["total_tokens"] += int(item)
                    elif isinstance(item, (dict, list)):
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for row in rows:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            role = (
                str(payload.get("role", "coordinator"))
                if row["kind"] == "model_usage"
                else ("reviewer" if row["kind"] == "independent_review" else "coordinator")
            )
            if row["kind"] != "model_usage" and (row["tick_id"], role) in model_roles:
                continue
            event_issue = payload.get("issue_number")
            result = payload.get("result")
            if event_issue is None and isinstance(result, dict):
                event_issue = result.get("issue_number")
            if (
                issue_number is not None
                and event_issue != issue_number
                and row["tick_id"] not in issue_ticks
            ):
                continue
            totals["model_calls"] += 1
            collect(payload.get("usage", {}))
        totals["total_tokens"] = max(
            totals["total_tokens"], totals["input_tokens"] + totals["output_tokens"]
        )
        return totals

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from qas import __version__
from qas.campaign import kill_active_agent, run_campaign
from qas.config import ConfigError, SupervisorConfig, load_config
from qas.db import LedgerIntegrityError
from qas.dogfood import run_scenario
from qas.governance_audit import audit_governance
from qas.httpd import serve
from qas.process import duration_seconds
from qas.runtime import AlreadyRunning, Supervisor

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qas", description="Qwen Autonomous Supervisor")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=os.environ.get("QAS_CONFIG", "supervisor.yml"))
    parser.add_argument(
        "--env-file",
        default=os.environ.get("QAS_ENV_FILE"),
        help="load provider variables from a local KEY=VALUE file without overriding the host",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="validate configuration and safety invariants")
    sub.add_parser("doctor", help="check external runtime dependencies")
    sub.add_parser("audit-governance", help="verify GitHub branch protection and CODEOWNERS")
    sub.add_parser("tick", help="execute one bounded coordinator tick")
    loop = sub.add_parser("loop", help="run the persistent scheduler")
    loop.add_argument("--once", action="store_true", help="recover and run at most one tick")
    loop.add_argument("--duration", help="stop after a duration such as 15m or 72h")
    sub.add_parser("recover", help="reconcile expired leases and interrupted runs")
    sub.add_parser("status", help="show durable supervisor status")
    events = sub.add_parser("events", help="show recent append-only events")
    events.add_argument("--limit", type=int, default=50)
    sub.add_parser("failures", help="show failure fingerprints and quarantine state")
    clear = sub.add_parser("unquarantine", help="allow retry after maintainer-provided evidence")
    clear.add_argument("fingerprint")
    clear.add_argument("--reason", required=True)
    sub.add_parser("serve", help="serve health, status and Prometheus metrics")
    chaos = sub.add_parser("chaos-kill-active", help="kill only the currently recorded Qwen child")
    chaos.add_argument("--reason", default="manual chaos test")
    campaign = sub.add_parser("campaign", help="run a bounded endurance/chaos campaign")
    campaign.add_argument("--duration", required=True)
    campaign.add_argument("--chaos-every")
    campaign.add_argument("--minimum-successful-ticks", type=int, default=1)
    dogfood = sub.add_parser("dogfood", help="run one deterministic dogfood scenario")
    dogfood.add_argument("scenario")
    return parser


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _load_env_file(path: str | None) -> None:
    if path is None:
        return
    env_path = Path(path).resolve()
    if not env_path.is_file():
        raise ConfigError(f"Environment file not found: {env_path}")
    for number, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid environment entry at {env_path}:{number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "A").isalnum() or not name[0].isalpha():
            raise ConfigError(f"Invalid environment name at {env_path}:{number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _doctor(config: SupervisorConfig, supervisor: Supervisor) -> int:
    tools = {}
    for name, binary in {
        "qwen": config.qwen.binary,
        "git": "git",
        "github_cli": "gh",
        "docker": "docker",
    }.items():
        resolved = str(Path(binary).resolve()) if Path(binary).is_file() else shutil.which(binary)
        tools[name] = {"available": bool(resolved), "path": resolved}
    git_repository = False
    if tools["git"]["available"] and config.project_root.is_dir():
        result = subprocess.run(
            ["git", "-C", str(config.project_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        git_repository = result.returncode == 0 and result.stdout.strip() == "true"
    report = {
        "configuration": "valid",
        "project_root": str(config.project_root),
        "project_exists": config.project_root.is_dir(),
        "git_repository": git_repository,
        "runtime_dir": str(config.runtime_dir),
        "qwen_ready": supervisor.launcher.available(),
        "tools": tools,
    }
    _json(report)
    return 0 if report["project_exists"] and git_repository and report["qwen_ready"] else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _load_env_file(args.env_file)
        config = load_config(args.config)
        supervisor = Supervisor(config, PACKAGE_ROOT)
        if args.command == "validate":
            _json({"valid": True, "config": str(config.config_path)})
            return 0
        if args.command == "doctor":
            return _doctor(config, supervisor)
        if args.command == "audit-governance":
            audit = audit_governance(config.project_root)
            _json(audit)
            return 0 if audit["safe"] else 1
        if args.command == "tick":
            outcome = supervisor.tick()
            _json(outcome.__dict__)
            return 0 if outcome.success else 1
        if args.command == "loop":
            maximum = duration_seconds(args.duration) if args.duration else None
            supervisor.loop(once=args.once, max_seconds=maximum)
            return 0
        if args.command == "recover":
            _json(supervisor.recover())
            return 0
        if args.command == "status":
            _json(supervisor.status())
            return 0
        if args.command == "events":
            _json(supervisor.ledger.events(args.limit))
            return 0
        if args.command == "failures":
            _json(supervisor.ledger.failures())
            return 0
        if args.command == "unquarantine":
            changed = supervisor.ledger.unquarantine(args.fingerprint)
            if changed:
                supervisor.ledger.append_event(
                    "failure_unquarantined",
                    operation_key=f"unquarantine:{args.fingerprint}:{args.reason}",
                    payload={"fingerprint": args.fingerprint, "reason": args.reason},
                )
            _json({"unquarantined": changed, "fingerprint": args.fingerprint})
            return 0 if changed else 1
        if args.command == "serve":
            serve(supervisor)
            return 0
        if args.command == "chaos-kill-active":
            pid = kill_active_agent(supervisor, reason=args.reason)
            _json({"killed": pid is not None, "pid": pid})
            return 0 if pid is not None else 1
        if args.command == "campaign":
            campaign_result = run_campaign(
                supervisor,
                duration_seconds=duration_seconds(args.duration),
                chaos_every_seconds=(
                    duration_seconds(args.chaos_every) if args.chaos_every else None
                ),
                minimum_successful_ticks=args.minimum_successful_ticks,
            )
            _json(campaign_result.__dict__)
            return 0 if campaign_result.passed else 1
        if args.command == "dogfood":
            scenario = Path(args.scenario).resolve()
            scenario_result = run_scenario(scenario, config.project_root)
            _json(scenario_result.__dict__)
            return 0 if scenario_result.passed else 1
    except (ConfigError, LedgerIntegrityError, AlreadyRunning, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

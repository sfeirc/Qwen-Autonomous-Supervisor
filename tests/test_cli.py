from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import write_config

from qas.cli import main
from qas.config import load_config
from qas.db import Ledger


def test_validate_status_events_and_recover(tmp_path: Path, git_project: Path) -> None:
    config = write_config(tmp_path, git_project)
    assert main(["--config", str(config), "validate"]) == 0
    assert main(["--config", str(config), "status"]) == 0
    assert main(["--config", str(config), "events", "--limit", "2"]) == 0
    assert main(["--config", str(config), "recover"]) == 0


def test_doctor_reports_missing_qwen(tmp_path: Path, git_project: Path) -> None:
    config = write_config(tmp_path, git_project)
    assert main(["--config", str(config), "doctor"]) == 2


def test_failure_listing_and_unquarantine(tmp_path: Path, git_project: Path) -> None:
    config = write_config(tmp_path, git_project, failures=1)
    assert main(["--config", str(config), "tick"]) == 1
    assert main(["--config", str(config), "failures"]) == 0
    loaded = load_config(config)
    fp = Ledger(loaded.runtime_dir / "state.db").failures(True)[0]["fingerprint"]
    assert (
        main(
            [
                "--config",
                str(config),
                "unquarantine",
                fp,
                "--reason",
                "dependency repaired",
            ]
        )
        == 0
    )


def test_cli_dogfood(tmp_path: Path, git_project: Path) -> None:
    config = write_config(tmp_path, git_project)
    scenario = tmp_path / "scenario.yml"
    scenario.write_text("name: git\nsteps:\n  - command: [git, status]\n", encoding="utf-8")
    assert main(["--config", str(config), "dogfood", str(scenario)]) == 0


def test_bad_config_is_user_facing(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "missing.yml"
    assert main(["--config", str(missing), "validate"]) == 2
    assert "not found" in capsys.readouterr().err

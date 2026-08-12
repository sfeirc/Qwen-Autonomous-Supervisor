from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from qas.campaign import kill_active_agent, run_campaign
from qas.config import load_config
from qas.runtime import Supervisor


def test_campaign_writes_durable_report(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )

    def fake_loop(*, max_seconds: float | None = None, once: bool = False) -> None:
        assert max_seconds == 0.1
        for index in range(2):
            supervisor.ledger.append_event(
                "tick_finished",
                tick_id=str(index),
                state="idle",
                operation_key=f"fake-tick:{index}",
            )

    monkeypatch.setattr(supervisor, "loop", fake_loop)
    result = run_campaign(supervisor, duration_seconds=0.1, minimum_successful_ticks=2)
    assert result.passed
    assert result.successful_ticks == 2
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert report["campaign_id"] == result.campaign_id


def test_campaign_reports_failed_acceptance(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    monkeypatch.setattr(supervisor, "loop", lambda **_kwargs: None)
    result = run_campaign(supervisor, duration_seconds=0.01, minimum_successful_ticks=1)
    assert not result.passed
    assert "below required" in result.failures[0]
    assert kill_active_agent(supervisor) is None

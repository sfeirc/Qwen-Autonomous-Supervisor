from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import write_config

from qas.campaign import active_campaign, kill_active_agent, run_campaign
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


def test_campaign_resumes_after_simulated_process_crash(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real SIGKILL/OOM/host-reboot never lets Python's cleanup code run,
    so the durable checkpoint set at campaign start is the only thing that
    survives. Simulate that here by making the mocked loop() raise -- the
    exception propagates out of run_campaign() before it ever reaches the
    finalization/checkpoint-clearing code, exactly like a hard crash would.
    A second, independent Supervisor reusing the same ledger must then
    resume the SAME campaign (same id, same original event-count baseline,
    remaining time only) rather than starting a fresh one.
    """
    config = load_config(write_config(tmp_path, git_project))
    first_supervisor = Supervisor(config, Path(__file__).parents[1])

    def crashing_loop(*, max_seconds: float | None = None, once: bool = False) -> None:
        assert max_seconds == pytest.approx(3600, abs=1)
        first_supervisor.ledger.append_event(
            "tick_finished", tick_id="0", state="idle", operation_key="fake-tick:0"
        )
        raise RuntimeError("simulated hard process crash mid-campaign")

    monkeypatch.setattr(first_supervisor, "loop", crashing_loop)
    with pytest.raises(RuntimeError, match="simulated hard process crash"):
        run_campaign(first_supervisor, duration_seconds=3600, minimum_successful_ticks=1)

    checkpoint = active_campaign(first_supervisor)
    assert checkpoint is not None
    original_campaign_id = checkpoint["campaign_id"]

    second_supervisor = Supervisor(config, Path(__file__).parents[1])

    def second_loop(*, max_seconds: float | None = None, once: bool = False) -> None:
        assert max_seconds is not None
        assert max_seconds < 3600  # remaining time to the ORIGINAL deadline, not a fresh 3600s
        second_supervisor.ledger.append_event(
            "tick_finished", tick_id="1", state="idle", operation_key="fake-tick:1"
        )

    monkeypatch.setattr(second_supervisor, "loop", second_loop)
    # duration_seconds=999 here must be ignored: we're resuming, not starting fresh.
    result = run_campaign(second_supervisor, duration_seconds=999, minimum_successful_ticks=2)

    assert result.campaign_id == original_campaign_id
    assert result.successful_ticks == 2  # 1 from the crashed process + 1 from the resumed one
    assert result.passed
    assert active_campaign(second_supervisor) is None  # cleared on genuine completion


def test_expired_campaign_checkpoint_is_not_resumed(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )
    supervisor.ledger.set_checkpoint(
        "active_campaign",
        {
            "campaign_id": "stale-campaign-from-a-week-ago",
            "started_at": "2020-01-01T00:00:00.000Z",
            "started_epoch": 0.0,
            "deadline_epoch": time.time() - 3600,  # already expired
            "before": {},
            "chaos_every_seconds": None,
            "minimum_successful_ticks": 1,
        },
    )
    assert active_campaign(supervisor) is None

    monkeypatch.setattr(supervisor, "loop", lambda **_kwargs: None)
    result = run_campaign(supervisor, duration_seconds=60, minimum_successful_ticks=0)
    assert result.campaign_id != "stale-campaign-from-a-week-ago"


def test_status_reports_active_campaign(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        load_config(write_config(tmp_path, git_project)), Path(__file__).parents[1]
    )

    def long_loop(*, max_seconds: float | None = None, once: bool = False) -> None:
        status = supervisor.status()
        assert status["active_campaign"] is not None
        assert status["active_campaign"]["remaining_seconds"] > 0

    monkeypatch.setattr(supervisor, "loop", long_loop)
    run_campaign(supervisor, duration_seconds=3600, minimum_successful_ticks=0)
    assert supervisor.status()["active_campaign"] is None

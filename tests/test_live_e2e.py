from __future__ import annotations

import os
from pathlib import Path

import pytest

from qas.config import load_config
from qas.runtime import Supervisor


@pytest.mark.live_e2e
def test_real_qwen_github_tick() -> None:
    config_path = os.environ.get("QAS_LIVE_E2E_CONFIG")
    if not config_path:
        pytest.skip("set QAS_LIVE_E2E_CONFIG to an authenticated disposable repository")
    config = load_config(config_path)
    supervisor = Supervisor(config, Path(__file__).parents[1])
    assert supervisor.launcher.available(), "Qwen Code is not installed"
    outcome = supervisor.tick()
    assert outcome.success, outcome.reason

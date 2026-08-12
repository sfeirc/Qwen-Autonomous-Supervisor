from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_disposable_fixture_starts_healthy_for_existing_behavior() -> None:
    fixture = Path(__file__).parents[1] / "e2e" / "fixture"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

from __future__ import annotations

import sys
from pathlib import Path

from qas.dogfood import run_scenario


def test_dogfood_assertions(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.yml"
    scenario.write_text(
        f"""name: hello
steps:
  - command: [{sys.executable!r}, -c, \"print('hello')\"]
    stdoutContains: hello
    outputRegex: h.llo
""",
        encoding="utf-8",
    )
    result = run_scenario(scenario, tmp_path)
    assert result.passed
    assert result.steps[0]["exit_code"] == 0


def test_dogfood_stops_after_failure(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.yml"
    scenario.write_text(
        f"""name: fail
steps:
  - command: [{sys.executable!r}, -c, \"raise SystemExit(7)\"]
  - command: [{sys.executable!r}, -c, \"print('not run')\"]
""",
        encoding="utf-8",
    )
    result = run_scenario(scenario, tmp_path)
    assert not result.passed
    assert len(result.steps) == 1

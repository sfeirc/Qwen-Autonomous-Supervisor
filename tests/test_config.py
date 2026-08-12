from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from qas.config import ConfigError, load_config


def test_load_config_resolves_paths(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project))
    assert config.project_root == git_project.resolve()
    assert config.runtime_dir == (tmp_path / "runtime").resolve()
    assert config.scheduler.heartbeat_seconds < config.scheduler.lease_seconds


def test_runtime_cannot_be_inside_target(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            (tmp_path / "runtime").as_posix(), (git_project / "runtime").as_posix()
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="outside projectRoot"):
        load_config(path)


def test_runtime_cannot_contain_target(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            (tmp_path / "runtime").as_posix(), tmp_path.as_posix()
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="outside projectRoot"):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("sandbox: true", "sandbox: 'false'", "true or false"),
        ("maxWallTime: 2s", "maxWallTime: forever", "duration"),
    ],
)
def test_safety_types_are_strict(
    tmp_path: Path, git_project: Path, old: str, new: str, message: str
) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize("schema", ["wrong", "", "qas/v2"])
def test_schema_is_strict(tmp_path: Path, git_project: Path, schema: str) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(path.read_text().replace("qas/v1", schema), encoding="utf-8")
    with pytest.raises(ConfigError, match="schema"):
        load_config(path)


def test_cost_budgets_require_pricing(tmp_path: Path, git_project: Path) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8") + "budgets:\n  maxCostPerDay: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="pricing"):
        load_config(path)


def test_budget_storage_and_output_token_limits_parse(
    tmp_path: Path, git_project: Path
) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  maxSessionTurns: 5",
            "  maxSessionTurns: 5\n  maxOutputTokens: 4096",
        )
        + """observability:
  inputCostPerMillion: 3
  outputCostPerMillion: 15
budgets:
  maxTokensPerDay: 12345
  maxCostPerDay: 2
storage:
  minimumFreeBytes: 1024
  maximumArtifacts: 7
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.qwen.max_output_tokens == 4096
    assert config.budgets.max_tokens_per_day == 12345
    assert config.budgets.max_cost_per_day == 2
    assert config.storage.minimum_free_bytes == 1024
    assert config.storage.maximum_artifacts == 7

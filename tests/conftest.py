from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    (project / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial"], cwd=project, check=True, capture_output=True
    )
    return project


def write_config(tmp_path: Path, project: Path, **overrides: object) -> Path:
    runtime = tmp_path / "runtime"
    binary = overrides.get("binary", "definitely-missing-qwen")
    failures = overrides.get("failures", 3)
    text = f"""schema: qas/v1
projectRoot: {project.as_posix()}
runtimeDir: {runtime.as_posix()}
qwen:
  binary: {binary}
  model: test
  sandbox: true
  maxWallTime: 2s
  maxToolCalls: 5
  maxSessionTurns: 5
scheduler:
  loopMinutes: 0.01
  leaseSeconds: 2
  heartbeatSeconds: 1
  silenceTimeoutSeconds: 2
  shutdownGraceSeconds: 1
recovery:
  maximumIdenticalFailures: {failures}
  retryBaseSeconds: 1
  retryMaximumSeconds: 2
review:
  enabled: true
  maxToolCalls: 1
qualityGates: []
"""
    path = tmp_path / "supervisor.yml"
    path.write_text(text, encoding="utf-8")
    return path

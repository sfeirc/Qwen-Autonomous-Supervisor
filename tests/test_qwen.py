from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import write_config

from qas.config import load_config
from qas.db import Ledger
from qas.models import ProcessResult
from qas.qwen import QwenLauncher


def result(structured: dict[str, Any] | None = None, stdout: str = "") -> ProcessResult:
    return ProcessResult(0, "success", "a", "b", 1, stdout, "", "session", structured, {})


def test_coordinator_builds_bounded_command(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_config(tmp_path, git_project, binary=sys.executable))
    ledger = Ledger(config.runtime_dir / "state.db")
    launcher = QwenLauncher(config, ledger, Path(__file__).parents[1])
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> ProcessResult:
        captured["argv"] = argv
        captured.update(kwargs)
        return result({"state": "idle"})

    monkeypatch.setattr("qas.qwen.run_process", fake_run)
    output = launcher.run_coordinator("run", "tick", tmp_path / "out.jsonl")
    assert output.structured_output == {"state": "idle"}
    argv = captured["argv"]
    assert "--output-format" in argv and "stream-json" in argv
    assert "--max-wall-time" in argv and "--json-schema" in argv
    assert "--system-prompt" not in argv and "--append-system-prompt" not in argv
    assert "\n" not in argv[argv.index("--prompt") + 1]
    assert captured["cwd"] == git_project
    assert "UNTRUSTED RUNTIME SNAPSHOT" in captured["stdin_text"]


def test_reviewer_is_fresh_and_read_only(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_config(tmp_path, git_project, binary=sys.executable))
    launcher = QwenLauncher(
        config, Ledger(config.runtime_dir / "state.db"), Path(__file__).parents[1]
    )
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> ProcessResult:
        captured["argv"] = argv
        captured.update(kwargs)
        return result({"verdict": "pass", "summary": "ok", "findings": []})

    monkeypatch.setattr("qas.qwen.run_process", fake_run)
    launcher.run_reviewer(
        "review", "tick", tmp_path / "review.jsonl", summary={"x": 1}, diff="diff"
    )
    assert "--resume" not in captured["argv"] and "--continue" not in captured["argv"]
    assert "--safe-mode" in captured["argv"]
    assert "--sandbox" not in captured["argv"]
    assert "--system-prompt" not in captured["argv"]
    assert "\n" not in captured["argv"][captured["argv"].index("--prompt") + 1]
    assert captured["argv"][captured["argv"].index("--max-tool-calls") + 1] == "1"
    assert "--exclude-tools" not in captured["argv"]
    assert "UNTRUSTED DIFF" in captured["stdin_text"]


def test_validated_output_supports_last_json_line() -> None:
    payload = {"state": "idle", "action": "wait"}
    parsed = QwenLauncher.validated_output(
        result(None, "noise\n" + json.dumps(payload)), {"state", "action"}
    )
    assert parsed == payload
    with pytest.raises(ValueError, match="structured"):
        QwenLauncher.validated_output(result(), {"state"})


def test_validated_output_supports_json_in_qwen_result_event() -> None:
    payload = {"verdict": "pass", "summary": "ok", "findings": []}
    event = json.dumps({"type": "result", "result": json.dumps(payload)})
    parsed = QwenLauncher.validated_output(
        result(None, "noise\n" + event), {"verdict", "summary", "findings"}
    )
    assert parsed == payload


def test_packaged_resource_fallback(tmp_path: Path, git_project: Path) -> None:
    config = load_config(write_config(tmp_path, git_project, binary=sys.executable))
    launcher = QwenLauncher(config, Ledger(config.runtime_dir / "state.db"), tmp_path / "absent")
    assert launcher._resource("agents/coordinator.md").is_file()


def test_child_environment_is_allowlisted(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNRELATED_DATABASE_PASSWORD", "must-not-leak")
    monkeypatch.setenv("QWEN_API_KEY", "allowed-provider-secret")
    config = load_config(write_config(tmp_path, git_project, binary=sys.executable))
    launcher = QwenLauncher(config, Ledger(config.runtime_dir / "state.db"), tmp_path)
    child = launcher._environment()
    assert "QWEN_API_KEY" in child
    assert "UNRELATED_DATABASE_PASSWORD" not in child
    assert child["QWEN_CODE_MAX_OUTPUT_TOKENS"] == "8192"
    assert child["QWEN_CODE_SKIP_UPDATE_CHECK_ONCE"] == "true"
    assert "QWEN_CODE_UNATTENDED_RETRY" not in child


def test_system_ca_is_enabled_without_disabling_tls(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path, git_project, binary=sys.executable)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  maxSessionTurns: 5",
            "  maxSessionTurns: 5\n  useSystemCertificateStore: true",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    launcher = QwenLauncher(
        load_config(path), Ledger(tmp_path / "runtime" / "state.db"), tmp_path
    )
    child = launcher._environment()
    assert "--use-system-ca" in child["NODE_OPTIONS"]
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in child

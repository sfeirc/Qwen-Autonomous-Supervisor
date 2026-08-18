from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when supervisor configuration is unsafe or invalid."""


@dataclass(frozen=True)
class QwenConfig:
    binary: str = "qwen"
    model: str = "qwen3.8-max"
    approval_mode: str = "auto"
    sandbox: bool = True
    persistent_retry: bool = False
    max_wall_time: str = "90m"
    max_tool_calls: int = 300
    max_session_turns: int = 120
    max_output_tokens: int = 8192
    use_system_certificate_store: bool = False
    reasoning_effort: str | None = None
    exclude_tools: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "TERM",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "QWEN_*",
        "DASHSCOPE_*",
        "OPENAI_*",
        "ANTHROPIC_*",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
    )


@dataclass(frozen=True)
class SchedulerConfig:
    loop_minutes: float = 30
    lease_seconds: int = 600
    heartbeat_seconds: int = 15
    silence_timeout_seconds: int = 600
    shutdown_grace_seconds: int = 20


@dataclass(frozen=True)
class RecoveryConfig:
    maximum_identical_failures: int = 3
    retry_base_seconds: int = 30
    retry_maximum_seconds: int = 1800


@dataclass(frozen=True)
class ReviewConfig:
    enabled: bool = True
    model: str = "qwen3.8-max"
    max_wall_time: str = "20m"
    max_tool_calls: int = 1
    maximum_critical_findings: int = 0


@dataclass(frozen=True)
class GateConfig:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 900


@dataclass(frozen=True)
class GovernanceConfig:
    protected_paths: tuple[str, ...] = (
        "AUTONOMY.md",
        ".autonomy/**",
        ".github/workflows/**",
        ".github/CODEOWNERS",
    )


@dataclass(frozen=True)
class ObservabilityConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


@dataclass(frozen=True)
class BudgetConfig:
    max_tokens_per_tick: int = 200_000
    max_tokens_per_hour: int = 500_000
    max_tokens_per_day: int = 2_000_000
    max_tokens_per_issue: int = 500_000
    max_cost_per_tick: float | None = None
    max_cost_per_hour: float | None = None
    max_cost_per_day: float | None = None
    max_cost_per_issue: float | None = None


@dataclass(frozen=True)
class StorageConfig:
    minimum_free_bytes: int = 1_073_741_824
    max_log_bytes: int = 52_428_800
    log_backup_count: int = 3
    artifact_retention_days: int = 30
    maximum_artifacts: int = 1000


@dataclass(frozen=True)
class SupervisorConfig:
    config_path: Path
    project_root: Path
    runtime_dir: Path
    qwen: QwenConfig = field(default_factory=QwenConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    quality_gates: tuple[GateConfig, ...] = ()
    dogfood_scenarios_directory: str = ".autonomy/dogfood"
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)[smh]?$", re.IGNORECASE)

# The real, currently-supported tiers for Qwen Code's `model.reasoningEffort`
# settings.json field, confirmed against the installed @qwen-code/qwen-code
# CLI's own compiled REASONING_EFFORT_TIERS constant -- not guessed.
REASONING_EFFORT_TIERS = ("low", "medium", "high", "xhigh", "max")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name} is required")

        return _ENV.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _positive(value: Any, name: str, *, allow_float: bool = False) -> float | int:
    expected = (int, float) if allow_float else (int,)
    if isinstance(value, bool) or not isinstance(value, expected) or value <= 0:
        raise ConfigError(f"{name} must be positive")
    return float(value) if allow_float else int(value)


def _optional_positive(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return float(_positive(value, name, allow_float=True))


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return int(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _duration(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DURATION.fullmatch(value.strip()):
        raise ConfigError(f"{name} must be a positive duration such as 90s, 20m, or 1.5h")
    number = float(value[:-1] if value[-1:].lower() in {"s", "m", "h"} else value)
    if number <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(value)


def _resolve(base: Path, raw: Any, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{name} must be a non-empty path")
    path = Path(raw)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(path: str | Path) -> SupervisorConfig:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML: {exc}") from exc
    data = _expand_env(_mapping(raw, "configuration"))
    if data.get("schema") != "qas/v1":
        raise ConfigError("schema must be qas/v1")

    base = config_path.parent
    project_root = _resolve(base, data.get("projectRoot"), "projectRoot")
    runtime_dir = _resolve(base, data.get("runtimeDir", "./runtime"), "runtimeDir")
    if (
        runtime_dir == project_root
        or project_root in runtime_dir.parents
        or runtime_dir in project_root.parents
        or runtime_dir == Path(runtime_dir.anchor)
    ):
        raise ConfigError(
            "runtimeDir must be outside projectRoot so runtime state is never committed"
        )

    q = _mapping(data.get("qwen"), "qwen")
    approval = str(q.get("approvalMode", "auto"))
    if approval not in {"plan", "default", "auto-edit", "auto", "yolo"}:
        raise ConfigError("qwen.approvalMode is invalid")
    binary = q.get("binary", "qwen")
    if not isinstance(binary, str) or not binary.strip():
        raise ConfigError("qwen.binary must be non-empty text")
    reasoning_effort_raw = q.get("reasoningEffort")
    reasoning_effort: str | None = None
    if reasoning_effort_raw is not None:
        if (
            not isinstance(reasoning_effort_raw, str)
            or reasoning_effort_raw not in REASONING_EFFORT_TIERS
        ):
            raise ConfigError(
                "qwen.reasoningEffort must be one of: "
                + ", ".join(REASONING_EFFORT_TIERS)
            )
        reasoning_effort = reasoning_effort_raw
    qwen = QwenConfig(
        binary=binary,
        model=str(q.get("model", "qwen3.8-max")),
        approval_mode=approval,
        sandbox=_boolean(q.get("sandbox", True), "qwen.sandbox"),
        persistent_retry=_boolean(q.get("persistentRetry", False), "qwen.persistentRetry"),
        max_wall_time=_duration(q.get("maxWallTime", "90m"), "qwen.maxWallTime"),
        max_tool_calls=int(_positive(q.get("maxToolCalls", 300), "qwen.maxToolCalls")),
        max_session_turns=int(_positive(q.get("maxSessionTurns", 120), "qwen.maxSessionTurns")),
        max_output_tokens=int(
            _positive(q.get("maxOutputTokens", 8192), "qwen.maxOutputTokens")
        ),
        use_system_certificate_store=_boolean(
            q.get("useSystemCertificateStore", False), "qwen.useSystemCertificateStore"
        ),
        reasoning_effort=reasoning_effort,
        exclude_tools=_string_list(q.get("excludeTools", []), "qwen.excludeTools"),
        environment_allowlist=_string_list(
            q.get("environmentAllowlist", list(QwenConfig().environment_allowlist)),
            "qwen.environmentAllowlist",
        ),
    )
    if qwen.approval_mode == "yolo" and not qwen.sandbox:
        raise ConfigError("qwen approvalMode yolo requires sandbox=true")

    s = _mapping(data.get("scheduler"), "scheduler")
    scheduler = SchedulerConfig(
        loop_minutes=float(
            _positive(s.get("loopMinutes", 30), "scheduler.loopMinutes", allow_float=True)
        ),
        lease_seconds=int(_positive(s.get("leaseSeconds", 600), "scheduler.leaseSeconds")),
        heartbeat_seconds=int(
            _positive(s.get("heartbeatSeconds", 15), "scheduler.heartbeatSeconds")
        ),
        silence_timeout_seconds=int(
            _positive(s.get("silenceTimeoutSeconds", 600), "scheduler.silenceTimeoutSeconds")
        ),
        shutdown_grace_seconds=int(
            _positive(s.get("shutdownGraceSeconds", 20), "scheduler.shutdownGraceSeconds")
        ),
    )
    if scheduler.heartbeat_seconds >= scheduler.lease_seconds:
        raise ConfigError("heartbeatSeconds must be lower than leaseSeconds")

    r = _mapping(data.get("recovery"), "recovery")
    recovery = RecoveryConfig(
        maximum_identical_failures=int(
            _positive(r.get("maximumIdenticalFailures", 3), "recovery.maximumIdenticalFailures")
        ),
        retry_base_seconds=int(
            _positive(r.get("retryBaseSeconds", 30), "recovery.retryBaseSeconds")
        ),
        retry_maximum_seconds=int(
            _positive(r.get("retryMaximumSeconds", 1800), "recovery.retryMaximumSeconds")
        ),
    )

    rv = _mapping(data.get("review"), "review")
    review = ReviewConfig(
        enabled=_boolean(rv.get("enabled", True), "review.enabled"),
        model=str(rv.get("model", qwen.model)),
        max_wall_time=_duration(rv.get("maxWallTime", "20m"), "review.maxWallTime"),
        max_tool_calls=_non_negative_integer(
            rv.get("maxToolCalls", 1), "review.maxToolCalls"
        ),
        maximum_critical_findings=int(rv.get("maximumCriticalFindings", 0)),
    )
    if review.max_tool_calls != 1:
        raise ConfigError("review.maxToolCalls must be exactly 1 for the read-only reviewer")
    if review.maximum_critical_findings < 0:
        raise ConfigError("review.maximumCriticalFindings cannot be negative")

    gov = _mapping(data.get("governance"), "governance")
    protected = gov.get("protectedPaths", list(GovernanceConfig().protected_paths))
    if not isinstance(protected, list) or not all(isinstance(item, str) for item in protected):
        raise ConfigError("governance.protectedPaths must be a list of paths")

    gates_raw = data.get("qualityGates", [])
    if not isinstance(gates_raw, list):
        raise ConfigError("qualityGates must be a list")
    gates: list[GateConfig] = []
    for index, item in enumerate(gates_raw):
        gate = _mapping(item, f"qualityGates[{index}]")
        command = gate.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) for x in command)
        ):
            raise ConfigError(f"qualityGates[{index}].command must be a non-empty argv list")
        gates.append(
            GateConfig(
                name=str(gate.get("name", f"gate-{index + 1}")),
                command=tuple(command),
                timeout_seconds=int(
                    _positive(
                        gate.get("timeoutSeconds", 900), f"qualityGates[{index}].timeoutSeconds"
                    )
                ),
            )
        )

    dogfood = _mapping(data.get("dogfood"), "dogfood")
    obs = _mapping(data.get("observability"), "observability")
    observability = ObservabilityConfig(
        host=str(obs.get("host", "127.0.0.1")),
        port=int(obs.get("port", 8787)),
        input_cost_per_million=float(obs.get("inputCostPerMillion", 0.0)),
        output_cost_per_million=float(obs.get("outputCostPerMillion", 0.0)),
    )
    if not 1 <= observability.port <= 65535:
        raise ConfigError("observability.port must be between 1 and 65535")
    if observability.input_cost_per_million < 0 or observability.output_cost_per_million < 0:
        raise ConfigError("observability token costs cannot be negative")

    b = _mapping(data.get("budgets"), "budgets")
    budgets = BudgetConfig(
        max_tokens_per_tick=int(
            _positive(b.get("maxTokensPerTick", 200_000), "budgets.maxTokensPerTick")
        ),
        max_tokens_per_hour=int(
            _positive(b.get("maxTokensPerHour", 500_000), "budgets.maxTokensPerHour")
        ),
        max_tokens_per_day=int(
            _positive(b.get("maxTokensPerDay", 2_000_000), "budgets.maxTokensPerDay")
        ),
        max_tokens_per_issue=int(
            _positive(b.get("maxTokensPerIssue", 500_000), "budgets.maxTokensPerIssue")
        ),
        max_cost_per_tick=_optional_positive(
            b.get("maxCostPerTick"), "budgets.maxCostPerTick"
        ),
        max_cost_per_hour=_optional_positive(
            b.get("maxCostPerHour"), "budgets.maxCostPerHour"
        ),
        max_cost_per_day=_optional_positive(b.get("maxCostPerDay"), "budgets.maxCostPerDay"),
        max_cost_per_issue=_optional_positive(
            b.get("maxCostPerIssue"), "budgets.maxCostPerIssue"
        ),
    )
    if any(
        value is not None
        for value in (
            budgets.max_cost_per_tick,
            budgets.max_cost_per_hour,
            budgets.max_cost_per_day,
            budgets.max_cost_per_issue,
        )
    ) and not (
        observability.input_cost_per_million > 0
        or observability.output_cost_per_million > 0
    ):
        raise ConfigError("cost budgets require non-zero observability token pricing")

    st = _mapping(data.get("storage"), "storage")
    storage = StorageConfig(
        minimum_free_bytes=int(
            _positive(st.get("minimumFreeBytes", 1_073_741_824), "storage.minimumFreeBytes")
        ),
        max_log_bytes=int(
            _positive(st.get("maxLogBytes", 52_428_800), "storage.maxLogBytes")
        ),
        log_backup_count=int(
            _positive(st.get("logBackupCount", 3), "storage.logBackupCount")
        ),
        artifact_retention_days=int(
            _positive(
                st.get("artifactRetentionDays", 30), "storage.artifactRetentionDays"
            )
        ),
        maximum_artifacts=int(
            _positive(st.get("maximumArtifacts", 1000), "storage.maximumArtifacts")
        ),
    )

    return SupervisorConfig(
        config_path=config_path,
        project_root=project_root,
        runtime_dir=runtime_dir,
        qwen=qwen,
        scheduler=scheduler,
        recovery=recovery,
        review=review,
        governance=GovernanceConfig(tuple(protected)),
        quality_gates=tuple(gates),
        dogfood_scenarios_directory=str(dogfood.get("scenariosDirectory", ".autonomy/dogfood")),
        observability=observability,
        budgets=budgets,
        storage=storage,
    )

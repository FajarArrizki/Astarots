"""Strict configuration model for reproducible mainnet-fork campaigns."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devil.core.types import ChainId, RelayMode, frozen_mapping


@dataclass(frozen=True)
class ChainConfig:
    alias: str
    chain_id: int
    rpc_env: str
    fork_block: int
    expected_block_hash: str = ""
    expected_state_root: str = ""

    def rpc_url(self, environ: Mapping[str, str] | None = None) -> str:
        value = (environ or os.environ).get(self.rpc_env, "")
        if not value:
            raise ValueError(f"RPC environment variable {self.rpc_env!r} is not set")
        return value


@dataclass(frozen=True)
class TargetConfig:
    context: str
    chain: str
    address: str
    artifact: str
    source: str = ""
    role: str = "source"
    expected_code_hash: str = ""
    proxy_kind: str = "none"
    implementation_address: str = ""
    expected_implementation_code_hash: str = ""


@dataclass(frozen=True)
class CorrelationConfig:
    id: str
    source_context: str
    source_event: str
    source_fields: tuple[str, ...]
    destination_context: str
    destination_event: str
    destination_fields: tuple[str, ...]
    normalize: str


@dataclass(frozen=True)
class ConfigDeadline:
    value: int
    unit: str
    chain_id: str = ""


@dataclass(frozen=True)
class RelayConfig:
    dataset: str
    mode: RelayMode
    protocol_adapter: str
    delay_model: str
    dataset_hash: str
    adapter_config: str
    adapter_config_hash: str
    ordering: str
    duplicate_delivery: str
    reorg_assumption: str
    delivery_deadline: ConfigDeadline | None = None
    finality_blocks: Mapping[str, int] = field(default_factory=dict)
    min_delay_seconds: Mapping[str, int] = field(default_factory=dict)
    max_delay_seconds: Mapping[str, int] = field(default_factory=dict)
    protocol_epoch_rules: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "finality_blocks", frozen_mapping(self.finality_blocks))
        object.__setattr__(self, "min_delay_seconds", frozen_mapping(self.min_delay_seconds))
        object.__setattr__(self, "max_delay_seconds", frozen_mapping(self.max_delay_seconds))
        object.__setattr__(self, "protocol_epoch_rules", frozen_mapping(self.protocol_epoch_rules))


@dataclass(frozen=True)
class SnapshotConfig:
    max_timestamp_delta: int = 300
    require_finalized: bool = True
    anchor_timestamp: int | None = None
    finality_policy: str = "probabilistic"
    protocol_epochs: Mapping[str, str] = field(default_factory=dict)
    message_cutoffs: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_epochs", frozen_mapping(self.protocol_epochs))
        object.__setattr__(self, "message_cutoffs", frozen_mapping(self.message_cutoffs))


@dataclass(frozen=True)
class ActorsConfig:
    policy: str
    policy_hash: str


@dataclass(frozen=True)
class ToolConfig:
    name: str
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", frozen_mapping(self.options))


@dataclass(frozen=True)
class CampaignConfig:
    invariants: str = "test/invariants/"
    output: str = ".astarots/output"
    tools: tuple[str, ...] = ("echidna", "halmos", "slither")
    max_depth: int = 8
    branching_caps: tuple[int, ...] = (4, 4, 3, 3, 2, 2, 1, 1)
    max_consecutive_expansions_per_chain: int = 4
    max_states: int = 200
    timeout: int = 600
    chains: Mapping[str, ChainConfig] = field(default_factory=dict)
    targets: Mapping[str, TargetConfig] = field(default_factory=dict)
    correlations: Mapping[str, CorrelationConfig] = field(default_factory=dict)
    relay: RelayConfig | None = None
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    actors: ActorsConfig | None = None
    tool_config: Mapping[str, ToolConfig] = field(default_factory=dict)
    source_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chains", frozen_mapping(self.chains))
        object.__setattr__(self, "targets", frozen_mapping(self.targets))
        object.__setattr__(self, "correlations", frozen_mapping(self.correlations))
        object.__setattr__(self, "tool_config", frozen_mapping(self.tool_config))

    def validate(self, *, check_paths: bool = False) -> list[str]:
        errors: list[str] = []
        if len(self.chains) < 2:
            errors.append("at least two chains must be configured")
        for alias, chain in self.chains.items():
            if alias != chain.alias:
                errors.append(f"chain alias mismatch: {alias!r}")
            if chain.chain_id <= 0:
                errors.append(f"{alias}: chain_id must be positive")
            if chain.fork_block < 0:
                errors.append(f"{alias}: fork_block must be non-negative")
            if not chain.rpc_env:
                errors.append(f"{alias}: rpc_env is required")
            if bool(chain.expected_block_hash) != bool(chain.expected_state_root):
                errors.append(f"{alias}: block hash and state root must be declared together")
        for context, target in self.targets.items():
            if context != target.context:
                errors.append(f"target context mismatch: {context!r}")
            if target.chain not in self.chains:
                errors.append(f"{context}: unknown chain {target.chain!r}")
            if not _is_address(target.address):
                errors.append(f"{context}: invalid deployed address")
            if not target.artifact:
                errors.append(f"{context}: artifact is required")
            if target.proxy_kind not in {"none", "transparent", "uups", "beacon"}:
                errors.append(f"{context}: unsupported proxy_kind {target.proxy_kind!r}")
            if target.proxy_kind != "none" and not target.implementation_address:
                errors.append(f"{context}: proxy implementation_address is required")
            if check_paths:
                if not self.resolve_path(target.artifact).is_file():
                    errors.append(f"{context}: artifact does not exist: {target.artifact}")
                if target.source and not self.resolve_path(target.source).exists():
                    errors.append(f"{context}: source does not exist: {target.source}")
        for name, correlation in self.correlations.items():
            if name != correlation.id:
                errors.append(f"correlation ID mismatch: {name!r}")
            if correlation.source_context not in self.targets:
                errors.append(f"{name}: unknown source context")
            if correlation.destination_context not in self.targets:
                errors.append(f"{name}: unknown destination context")
            if not correlation.source_fields or len(correlation.source_fields) != len(
                correlation.destination_fields
            ):
                errors.append(f"{name}: correlation fields must have equal non-zero arity")
        if self.relay is None:
            errors.append("relay configuration is required")
        else:
            errors.extend(_validate_relay(self.relay, self.chains))
            if check_paths and not self.resolve_path(self.relay.dataset).is_file():
                errors.append(f"relay dataset does not exist: {self.relay.dataset}")
        if self.actors is None:
            errors.append("actors configuration is required")
        elif check_paths and not self.resolve_path(self.actors.policy).is_file():
            errors.append(f"actor policy does not exist: {self.actors.policy}")
        if self.snapshot.max_timestamp_delta < 0:
            errors.append("snapshot.max_timestamp_delta must be non-negative")
        if self.max_depth < 0 or self.max_states <= 0 or self.timeout <= 0:
            errors.append("max_depth must be non-negative, max_states and timeout positive")
        if self.max_consecutive_expansions_per_chain <= 0:
            errors.append("max_consecutive_expansions_per_chain must be positive")
        if not self.branching_caps or any(cap < 0 for cap in self.branching_caps):
            errors.append("branching_caps must contain non-negative integers")
        unknown_tools = set(self.tools) - {"echidna", "halmos", "slither"}
        if unknown_tools:
            errors.append("unsupported tools: " + ", ".join(sorted(unknown_tools)))
        return errors

    def resolve_path(self, value: str) -> Path:
        base = self.source_path.parent if self.source_path else Path.cwd()
        path = Path(value)
        return path if path.is_absolute() else base / path


class ChainRegistry:
    def __init__(self, chains: Mapping[str, ChainConfig] | None = None) -> None:
        self._chains = dict(chains or {})

    def add(self, config: ChainConfig) -> None:
        if not config.alias.strip() or config.chain_id <= 0 or config.fork_block < 0:
            raise ValueError("chain alias, chain_id, and fork_block are invalid")
        self._chains[config.alias] = config

    def remove(self, alias: str) -> ChainConfig:
        try:
            return self._chains.pop(alias)
        except KeyError as exc:
            raise KeyError(f"unknown chain {alias!r}") from exc

    def get(self, alias: str) -> ChainConfig:
        try:
            return self._chains[alias]
        except KeyError as exc:
            raise KeyError(f"unknown chain {alias!r}") from exc

    def list(self) -> tuple[ChainConfig, ...]:
        return tuple(self._chains[key] for key in sorted(self._chains))

    def as_dict(self) -> dict[str, ChainConfig]:
        return dict(self._chains)


def load_campaign(path: str | Path) -> CampaignConfig:
    """Load every documented ``astarots.toml`` field and reject unknown keys."""
    source_path = Path(path).resolve()
    raw = tomllib.loads(source_path.read_text(encoding="utf-8"))
    _reject_unknown(
        raw,
        {"default", "chains", "targets", "correlations", "relay", "snapshot", "actors", "tools"},
        "root",
    )
    default = _mapping(raw.get("default", {}), "default")
    _reject_unknown(
        default,
        {
            "invariants",
            "tools",
            "output",
            "max_depth",
            "branching_caps",
            "max_consecutive_expansions_per_chain",
            "max_states",
            "timeout",
        },
        "default",
    )
    chains = {
        alias: _chain_from_toml(alias, _mapping(value, f"chains.{alias}"))
        for alias, value in _mapping(raw.get("chains", {}), "chains").items()
    }
    targets = {
        context: _target_from_toml(context, _mapping(value, f"targets.{context}"))
        for context, value in _mapping(raw.get("targets", {}), "targets").items()
    }
    correlations = {
        name: _correlation_from_toml(name, _mapping(value, f"correlations.{name}"))
        for name, value in _mapping(raw.get("correlations", {}), "correlations").items()
    }
    relay_raw = raw.get("relay")
    relay = _relay_from_toml(_mapping(relay_raw, "relay")) if relay_raw is not None else None
    snapshot = _snapshot_from_toml(_mapping(raw.get("snapshot", {}), "snapshot"))
    actors_raw = raw.get("actors")
    actors = _actors_from_toml(_mapping(actors_raw, "actors")) if actors_raw is not None else None
    tool_configs: dict[str, ToolConfig] = {}
    allowed_tool_options = {
        "echidna": {"timeout", "test_limit", "corpus_dir"},
        "halmos": {"timeout", "solver_timeout", "loop", "loop_bound", "width"},
        "slither": {"timeout", "exclude", "exclude_detectors"},
    }
    for name, value in _mapping(raw.get("tools", {}), "tools").items():
        options = _mapping(value, f"tools.{name}")
        _reject_unknown(options, allowed_tool_options.get(name, set()), f"tools.{name}")
        tool_configs[name] = ToolConfig(name, options)
    config = CampaignConfig(
        output=str(default.get("output", ".astarots/output")),
        invariants=str(default.get("invariants", "test/invariants/")),
        tools=tuple(str(tool) for tool in default.get("tools", ("echidna", "halmos", "slither"))),
        max_depth=int(default.get("max_depth", 8)),
        branching_caps=tuple(
            int(cap) for cap in default.get("branching_caps", (4, 4, 3, 3, 2, 2, 1, 1))
        ),
        max_consecutive_expansions_per_chain=int(
            default.get("max_consecutive_expansions_per_chain", 4)
        ),
        max_states=int(default.get("max_states", 200)),
        timeout=int(default.get("timeout", 600)),
        chains=chains,
        targets=targets,
        correlations=correlations,
        relay=relay,
        snapshot=snapshot,
        actors=actors,
        tool_config=tool_configs,
        source_path=source_path,
    )
    errors = config.validate()
    if errors:
        raise ValueError("invalid campaign configuration: " + "; ".join(errors))
    return config


def _chain_from_toml(alias: str, value: Mapping[str, Any]) -> ChainConfig:
    _reject_unknown(
        value,
        {"chain_id", "rpc_env", "fork_block", "expected_block_hash", "expected_state_root"},
        f"chains.{alias}",
    )
    return ChainConfig(
        alias=alias,
        chain_id=int(value["chain_id"]),
        rpc_env=str(value["rpc_env"]),
        fork_block=int(value["fork_block"]),
        expected_block_hash=str(value.get("expected_block_hash", "")),
        expected_state_root=str(value.get("expected_state_root", "")),
    )


def _target_from_toml(context: str, value: Mapping[str, Any]) -> TargetConfig:
    _reject_unknown(
        value,
        {
            "address",
            "artifact",
            "source",
            "role",
            "expected_code_hash",
            "proxy_kind",
            "implementation_address",
            "expected_implementation_code_hash",
        },
        f"targets.{context}",
    )
    chain, separator, _ = context.partition(".")
    if not separator:
        raise ValueError(f"target context {context!r} must be chain.contract")
    return TargetConfig(
        context=context,
        chain=chain,
        address=str(value["address"]),
        artifact=str(value["artifact"]),
        source=str(value.get("source", "")),
        role=str(value.get("role", "source")),
        expected_code_hash=str(value.get("expected_code_hash", "")),
        proxy_kind=str(value.get("proxy_kind", "none")).lower(),
        implementation_address=str(value.get("implementation_address", "")),
        expected_implementation_code_hash=str(value.get("expected_implementation_code_hash", "")),
    )


def _correlation_from_toml(name: str, value: Mapping[str, Any]) -> CorrelationConfig:
    _reject_unknown(
        value,
        {
            "source_context",
            "source_event",
            "source_fields",
            "destination_context",
            "destination_event",
            "destination_fields",
            "normalize",
        },
        f"correlations.{name}",
    )
    return CorrelationConfig(
        id=name,
        source_context=str(value["source_context"]),
        source_event=str(value["source_event"]),
        source_fields=tuple(str(item) for item in value["source_fields"]),
        destination_context=str(value["destination_context"]),
        destination_event=str(value["destination_event"]),
        destination_fields=tuple(str(item) for item in value["destination_fields"]),
        normalize=str(value["normalize"]),
    )


def _relay_from_toml(value: Mapping[str, Any]) -> RelayConfig:
    allowed = {
        "dataset",
        "mode",
        "protocol_adapter",
        "delay_model",
        "dataset_hash",
        "adapter_config",
        "adapter_config_hash",
        "ordering",
        "duplicate_delivery",
        "reorg_assumption",
        "delivery_deadline",
        "finality_blocks",
        "min_delay_seconds",
        "max_delay_seconds",
        "protocol_epoch_rules",
    }
    _reject_unknown(value, allowed, "relay")
    deadline_raw = value.get("delivery_deadline")
    deadline = None
    if deadline_raw is not None:
        deadline_map = _mapping(deadline_raw, "relay.delivery_deadline")
        _reject_unknown(deadline_map, {"value", "unit", "chain_id"}, "relay.delivery_deadline")
        deadline = ConfigDeadline(
            int(deadline_map["value"]),
            str(deadline_map["unit"]),
            str(deadline_map.get("chain_id", "")),
        )
    return RelayConfig(
        dataset=str(value["dataset"]),
        mode=RelayMode(str(value["mode"])),
        protocol_adapter=str(value["protocol_adapter"]),
        delay_model=str(value["delay_model"]).lower(),
        dataset_hash=str(value["dataset_hash"]),
        adapter_config=str(value["adapter_config"]),
        adapter_config_hash=str(value["adapter_config_hash"]),
        ordering=str(value["ordering"]).lower(),
        duplicate_delivery=str(value["duplicate_delivery"]).lower(),
        reorg_assumption=str(value["reorg_assumption"]).lower(),
        delivery_deadline=deadline,
        finality_blocks={
            str(k): int(v)
            for k, v in _mapping(value.get("finality_blocks", {}), "relay.finality_blocks").items()
        },
        min_delay_seconds={
            str(k): int(v)
            for k, v in _mapping(
                value.get("min_delay_seconds", {}), "relay.min_delay_seconds"
            ).items()
        },
        max_delay_seconds={
            str(k): int(v)
            for k, v in _mapping(
                value.get("max_delay_seconds", {}), "relay.max_delay_seconds"
            ).items()
        },
        protocol_epoch_rules=_mapping(
            value.get("protocol_epoch_rules", {}), "relay.protocol_epoch_rules"
        ),
    )


def _snapshot_from_toml(value: Mapping[str, Any]) -> SnapshotConfig:
    _reject_unknown(
        value,
        {
            "max_timestamp_delta",
            "require_finalized",
            "anchor_timestamp",
            "finality_policy",
            "protocol_epochs",
            "message_cutoffs",
        },
        "snapshot",
    )
    return SnapshotConfig(
        max_timestamp_delta=int(value.get("max_timestamp_delta", 300)),
        require_finalized=bool(value.get("require_finalized", True)),
        anchor_timestamp=int(value["anchor_timestamp"]) if "anchor_timestamp" in value else None,
        finality_policy=str(value.get("finality_policy", "probabilistic")),
        protocol_epochs={
            str(k): str(v)
            for k, v in _mapping(
                value.get("protocol_epochs", {}), "snapshot.protocol_epochs"
            ).items()
        },
        message_cutoffs={
            str(k): int(v)
            for k, v in _mapping(
                value.get("message_cutoffs", {}), "snapshot.message_cutoffs"
            ).items()
        },
    )


def _actors_from_toml(value: Mapping[str, Any]) -> ActorsConfig:
    _reject_unknown(value, {"policy", "policy_hash"}, "actors")
    return ActorsConfig(str(value["policy"]), str(value["policy_hash"]))


def _validate_relay(relay: RelayConfig, chains: Mapping[str, ChainConfig]) -> list[str]:
    errors: list[str] = []
    if relay.delay_model not in {"none", "fixed", "bounded", "dataset"}:
        errors.append("relay.delay_model is invalid")
    if relay.ordering not in {"fifo_per_emitter", "unordered", "protocol_defined"}:
        errors.append("relay.ordering is invalid")
    if relay.duplicate_delivery not in {"reject", "allow_for_test"}:
        errors.append("relay.duplicate_delivery is invalid")
    if relay.reorg_assumption != "no_reorg_after_finality":
        errors.append("only no_reorg_after_finality is supported")
    for mapping_name, mapping in (
        ("finality_blocks", relay.finality_blocks),
        ("min_delay_seconds", relay.min_delay_seconds),
        ("max_delay_seconds", relay.max_delay_seconds),
    ):
        unknown = set(mapping) - set(chains)
        if unknown:
            errors.append(f"relay.{mapping_name} has unknown chains: {sorted(unknown)}")
        if any(value < 0 for value in mapping.values()):
            errors.append(f"relay.{mapping_name} values must be non-negative")
    for chain in chains:
        minimum = relay.min_delay_seconds.get(chain, 0)
        maximum = relay.max_delay_seconds.get(chain, minimum)
        if maximum < minimum:
            errors.append(f"relay delay range is inverted for {chain}")
    return errors


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a table")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} keys: {', '.join(sorted(unknown))}")


def _is_address(value: str) -> bool:
    return (
        len(value) == 42
        and value.startswith("0x")
        and all(character in "0123456789abcdefABCDEF" for character in value[2:])
    )


def chain_id_for(alias: str) -> ChainId:
    return ChainId(alias)

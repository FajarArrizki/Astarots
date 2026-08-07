"""Configuration and chain registry for mainnet-fork campaigns."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devil.core.types import ChainId


@dataclass(frozen=True)
class ChainConfig:
    """A reproducible fork target; RPC secrets are referenced by env name."""

    alias: str
    chain_id: int
    rpc_env: str
    fork_block: int
    expected_block_hash: str = ""
    expected_state_root: str = ""

    def rpc_url(self, environ: dict[str, str] | None = None) -> str:
        """Resolve the RPC URL only at backend-spawn time."""
        value = (environ or os.environ).get(self.rpc_env, "")
        if not value:
            raise ValueError(f"RPC environment variable {self.rpc_env!r} is not set")
        return value


@dataclass(frozen=True)
class TargetConfig:
    """Explicit chain/context binding for an already deployed contract."""

    context: str
    chain: str
    address: str
    artifact: str
    source: str = ""
    role: str = "source"
    expected_code_hash: str = ""
    proxy_kind: str = ""
    implementation_address: str = ""
    expected_implementation_code_hash: str = ""


@dataclass(frozen=True)
class CampaignConfig:
    """Effective campaign configuration after precedence resolution."""

    invariants: str = "test/invariants/"
    tools: tuple[str, ...] = ("echidna", "halmos", "slither")
    max_depth: int = 8
    branching_caps: tuple[int, ...] = (4, 4, 3, 3, 2, 2, 1, 1)
    max_states: int = 200
    timeout: int = 600
    chains: dict[str, ChainConfig] = field(default_factory=dict)
    targets: dict[str, TargetConfig] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.chains:
            errors.append("at least one chain must be configured")
        for alias, chain in self.chains.items():
            if alias != chain.alias:
                errors.append(f"chain alias mismatch: {alias!r}")
            if chain.chain_id <= 0:
                errors.append(f"{alias}: chain_id must be positive")
            if chain.fork_block < 0:
                errors.append(f"{alias}: fork_block must be non-negative")
            if not chain.rpc_env:
                errors.append(f"{alias}: rpc_env is required")
        for context, target in self.targets.items():
            if context != target.context:
                errors.append(f"target context mismatch: {context!r}")
            if target.chain not in self.chains:
                errors.append(f"{context}: unknown chain {target.chain!r}")
            if not _is_address(target.address):
                errors.append(f"{context}: invalid deployed address")
            if not target.artifact:
                errors.append(f"{context}: artifact is required")
        if self.max_depth < 0 or self.max_states <= 0 or self.timeout <= 0:
            errors.append("max_depth must be non-negative, max_states and timeout positive")
        if not self.branching_caps or any(cap < 0 for cap in self.branching_caps):
            errors.append("branching_caps must contain non-negative integers")
        return errors


class ChainRegistry:
    """Mutable registry used by CLI commands; campaign objects stay immutable."""

    def __init__(self, chains: dict[str, ChainConfig] | None = None) -> None:
        self._chains = dict(chains or {})

    def add(self, config: ChainConfig) -> None:
        if not config.alias.strip():
            raise ValueError("chain alias cannot be empty")
        if config.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if config.fork_block < 0:
            raise ValueError("fork_block must be non-negative")
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
    """Load ``astarots.toml`` without resolving RPC secrets."""
    raw = tomllib.loads(Path(path).read_text())
    default = raw.get("default", {})
    chains = {
        alias: _chain_from_toml(alias, value) for alias, value in raw.get("chains", {}).items()
    }
    targets = {
        context: _target_from_toml(context, value)
        for context, value in raw.get("targets", {}).items()
    }
    config = CampaignConfig(
        invariants=str(default.get("invariants", "test/invariants/")),
        tools=tuple(str(tool) for tool in default.get("tools", ("echidna", "halmos", "slither"))),
        max_depth=int(default.get("max_depth", 8)),
        branching_caps=tuple(
            int(cap) for cap in default.get("branching_caps", (4, 4, 3, 3, 2, 2, 1, 1))
        ),
        max_states=int(default.get("max_states", 200)),
        timeout=int(default.get("timeout", 600)),
        chains=chains,
        targets=targets,
    )
    errors = config.validate()
    if errors:
        raise ValueError("invalid campaign configuration: " + "; ".join(errors))
    return config


def _chain_from_toml(alias: str, value: dict[str, Any]) -> ChainConfig:
    return ChainConfig(
        alias=alias,
        chain_id=int(value["chain_id"]),
        rpc_env=str(value["rpc_env"]),
        fork_block=int(value["fork_block"]),
        expected_block_hash=str(value.get("expected_block_hash", "")),
        expected_state_root=str(value.get("expected_state_root", "")),
    )


def _target_from_toml(context: str, value: dict[str, Any]) -> TargetConfig:
    chain, _, name = context.partition(".")
    return TargetConfig(
        context=context,
        chain=chain,
        address=str(value["address"]),
        artifact=str(value["artifact"]),
        source=str(value.get("source", "")),
        role=str(value.get("role", "source")),
        expected_code_hash=str(value.get("expected_code_hash", "")),
        proxy_kind=str(value.get("proxy_kind", "")),
        implementation_address=str(value.get("implementation_address", "")),
        expected_implementation_code_hash=str(value.get("expected_implementation_code_hash", "")),
    )


def _is_address(value: str) -> bool:
    return (
        len(value) == 42
        and value.startswith("0x")
        and all(char in "0123456789abcdefABCDEF" for char in value[2:])
    )


def chain_id_for(alias: str) -> ChainId:
    """Map built-in aliases to the typed chain enum."""
    try:
        return ChainId(alias)
    except ValueError as exc:
        raise ValueError(f"unsupported built-in chain alias {alias!r}") from exc

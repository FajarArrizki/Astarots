"""End-to-end campaign construction from validated configuration."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from collections.abc import Mapping
from importlib.metadata import version
from typing import Any

from devil.adapter import AdapterRegistry, CandidateWorkers, project_chain
from devil.adapter.echidna import EchidnaAdapter, EchidnaForkConfig
from devil.adapter.halmos import HalmosAdapter
from devil.adapter.slither import SlitherAdapter
from devil.core.config import CampaignConfig
from devil.core.loaders import (
    correlation_extractors,
    load_abi_signatures,
    load_actor_policy,
    load_relay_dataset,
    load_storage_layouts,
)
from devil.core.relay import MessageCoordinator, relay_policy_hash
from devil.core.snapshot import SnapshotSet, verify_campaign_snapshots
from devil.core.types import ChainId, SearchState
from devil.evidence.report import EvidenceReport
from devil.harness.anvil import AnvilFleet
from devil.harness.executor import CanonicalForkExecutor, JsonRpcForkBackend
from devil.harness.observer import EvmBindingObserver
from devil.harness.scheduler import CampaignResult, CampaignScheduler
from devil.harness.search import SearchConfig
from devil.invariant.ir import CrossChainInvariant, load_invariant


class CampaignRuntime:
    """Validate once, then execute every invariant on fresh local pinned forks."""

    def __init__(self, config: CampaignConfig) -> None:
        self.config = config
        if config.relay is None or config.actors is None:
            raise ValueError("campaign relay and actor configuration are required")
        self.relay_dataset = load_relay_dataset(
            config.resolve_path(config.relay.dataset), config.relay.dataset_hash
        )
        self.actor_policy = load_actor_policy(
            config.resolve_path(config.actors.policy), config.actors.policy_hash
        )
        self.correlations = correlation_extractors(config)
        self.abi_signatures = load_abi_signatures(config)
        self.storage_layouts = load_storage_layouts(config)
        self.invariants = self._load_invariants()

    def validate(self) -> SnapshotSet:
        epochs = _observed_epochs(self.relay_dataset.messages)
        cutoffs = _observed_cutoffs(self.relay_dataset.messages)
        return verify_campaign_snapshots(
            self.config,
            observed_protocol_epochs=epochs,
            observed_message_cutoffs=cutoffs,
        )

    def run(self) -> tuple[CampaignResult, ...]:
        snapshot_set = self.validate()
        results: list[CampaignResult] = []
        with AnvilFleet(self.config.chains) as fleet:
            backend = JsonRpcForkBackend(fleet.clients)
            for invariant in self.invariants:
                coordinator = MessageCoordinator(self.relay_dataset, self.config.relay)
                observer = EvmBindingObserver(
                    invariant,
                    snapshot_set,
                    fleet.clients,
                    storage_layouts=self.storage_layouts,
                )
                executor = CanonicalForkExecutor(
                    snapshot_set,
                    backend,
                    relay_dataset=self.relay_dataset,
                    actor_policy=self.actor_policy,
                    coordinator=coordinator,
                    observe=observer,
                )
                workers = self._workers(invariant)

                def propose(
                    state: SearchState, *, _workers: Mapping[ChainId, CandidateWorkers] = workers
                ):
                    chain = state.chain_context
                    projection = project_chain(
                        state.global_state,
                        snapshot_set.base_fingerprints[chain],
                        chain,
                    )
                    runtime_options = {
                        "echidna": {
                            "fork_config": EchidnaForkConfig(
                                self.config.chains[chain.value].rpc_env,
                                state.global_state.chain_snapshots[chain].block_number,
                                int(
                                    _tool_options(self.config, "echidna").get("test_limit", 50_000)
                                ),
                                str(_tool_options(self.config, "echidna").get("corpus_dir", "")),
                                fleet.endpoints[chain],
                            )
                        }
                    }
                    targets = {
                        context: str(self.config.resolve_path(target.source or target.artifact))
                        for context, target in self.config.targets.items()
                        if target.chain == chain.value
                    }
                    return _workers[chain].propose(
                        targets=targets,
                        invariant_id=invariant.id,
                        constraints=state.constraints,
                        projection=projection,
                        chain=chain,
                        runtime_options=runtime_options,
                    )

                scheduler = CampaignScheduler(
                    SearchConfig(
                        self.config.max_depth,
                        self.config.max_states,
                        self.config.branching_caps,
                        self.config.max_consecutive_expansions_per_chain,
                    )
                )
                results.append(scheduler.run(invariant, executor, propose=propose))
        return tuple(results)

    def report(self, result: CampaignResult, snapshot_set: SnapshotSet) -> EvidenceReport:
        metadata = evidence_metadata(
            self.config,
            result.search,
            snapshot_set,
            self.relay_dataset.dataset_hash,
            self.actor_policy.policy_hash,
            invariant_hash=_digest(_invariant_payload(self._invariant(result.invariant_id))),
        )
        return EvidenceReport(result.invariant_id, result.search, metadata)

    def _load_invariants(self) -> tuple[CrossChainInvariant, ...]:
        root = self.config.resolve_path(self.config.invariants)
        paths = (root,) if root.is_file() else tuple(sorted(root.rglob("*.t.sol")))
        if not paths:
            raise ValueError(f"no invariant files found under {root}")
        return tuple(
            load_invariant(
                path,
                correlations=self.correlations,
                default_tools=self.config.tools,
                default_timeout=self.config.timeout,
            )
            for path in paths
        )

    def _workers(self, invariant: CrossChainInvariant) -> dict[ChainId, CandidateWorkers]:
        enabled = invariant.tool_allowlist or self.config.tools
        result: dict[ChainId, CandidateWorkers] = {}
        for chain in {context.chain_id for context in invariant.contexts.values()}:
            registry = AdapterRegistry()
            registry.register(EchidnaAdapter(timeout=_tool_timeout(self.config, "echidna", 300)))
            registry.register(HalmosAdapter(timeout=_tool_timeout(self.config, "halmos", 600)))
            registry.register(SlitherAdapter(timeout=_tool_timeout(self.config, "slither", 300)))
            echidna_options = dict(_tool_options(self.config, "echidna"))
            echidna_options["actor"] = self.actor_policy.actors[0]
            options = {
                "echidna": echidna_options,
                "halmos": dict(_tool_options(self.config, "halmos")),
                "slither": dict(_tool_options(self.config, "slither")),
            }
            result[chain] = CandidateWorkers(
                registry,
                enabled,
                options=options,
                abi_signatures=self.abi_signatures,
            )
        return result

    def _invariant(self, invariant_id: str) -> CrossChainInvariant:
        return next(item for item in self.invariants if item.id == invariant_id)


def evidence_metadata(
    config: CampaignConfig,
    search: Any,
    snapshot_set: SnapshotSet,
    relay_dataset_hash: str,
    actor_policy_hash: str,
    *,
    invariant_hash: str,
) -> dict[str, Any]:
    relay = config.relay
    if relay is None:
        raise ValueError("relay configuration is required")
    tools = {name: _tool_version(name) for name in config.tools}
    tools["canonical_executor"] = "anvil-json-rpc-v1"
    snapshot_chains = {
        chain.value: {
            "chain_id": fingerprint.chain_id,
            "fork_block": fingerprint.block_number,
            "block_hash": fingerprint.block_hash,
            "state_root": fingerprint.state_root,
            "fork_cache_hash": fingerprint.fork_cache_hash,
            "targets": {
                context: {
                    "address": target.address,
                    "code_hash": target.runtime_code_hash,
                    "proxy_kind": target.proxy_kind,
                    "implementation": target.implementation_address,
                    "implementation_code_hash": target.implementation_code_hash,
                    "artifact_hash": target.artifact_hash,
                }
                for context, target in fingerprint.targets.items()
            },
        }
        for chain, fingerprint in snapshot_set.base_fingerprints.items()
    }
    return {
        "run_id": "run_"
        + _digest([snapshot_set.id, invariant_hash, config.source_path, search.budget_used])[7:23],
        "project": {
            "revision": _project_revision(),
            "effective_config_hash": _digest(_config_payload(config)),
            "invariant_ir_hash": invariant_hash,
        },
        "tools": tools,
        "runtime": {
            "astarots": version("astarots"),
            "python": platform.python_version(),
            "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        },
        "snapshot_set": {
            "id": snapshot_set.id,
            "coherence_checks_hash": snapshot_set.coherence_checks_hash,
            "chains": snapshot_chains,
        },
        "relay": {
            "mode": relay.mode.value,
            "dataset_hash": relay_dataset_hash,
            "policy_hash": relay_policy_hash(relay),
            "adapter_config_hash": relay.adapter_config_hash,
            "message_ids": [message.identity for message in self_or_empty(search)],
        },
        "actor_policy_hash": actor_policy_hash,
        "execution": {
            "action_trace_hash": _digest(
                search.deepest_edge.witness.call_sequence
                if search.deepest_edge and search.deepest_edge.witness
                else ()
            ),
            "environment_hash": _digest(snapshot_set.id),
        },
        "search": {
            "global_depth": search.deepest_edge.depth if search.deepest_edge else 0,
            "budget_used": search.budget_used,
            "budget_total": search.budget_total,
            "incomplete_outcomes": [item.value for item in search.incomplete_outcomes],
            "unsupported_paths": sum(
                item.value == "unsupported" for item in search.incomplete_outcomes
            ),
        },
    }


def self_or_empty(search: Any) -> tuple[Any, ...]:
    if not search.witnesses:
        return ()
    return tuple(
        message.envelope for message in search.witnesses[-1].snapshot.pending_messages.values()
    )


def _observed_epochs(messages: tuple[Any, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        for key, value in message.protocol_metadata.items():
            if key.endswith("epoch"):
                result[key] = str(value)
    return result


def _observed_cutoffs(messages: tuple[Any, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for message in messages:
        result[message.emitter] = max(result.get(message.emitter, 0), message.sequence)
    return result


def _tool_options(config: CampaignConfig, name: str) -> Mapping[str, Any]:
    entry = config.tool_config.get(name)
    return entry.options if entry else {}


def _tool_timeout(config: CampaignConfig, name: str, default: int) -> int:
    return int(_tool_options(config, name).get("timeout", default))


def _tool_version(name: str) -> str:
    binary = {"echidna": "echidna", "halmos": "halmos", "slither": "slither"}[name]
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0] if output else "unknown"


def _project_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _config_payload(config: CampaignConfig) -> dict[str, Any]:
    return {
        "chains": {name: vars(value) for name, value in config.chains.items()},
        "targets": {name: vars(value) for name, value in config.targets.items()},
        "correlations": {name: vars(value) for name, value in config.correlations.items()},
        "relay": vars(config.relay) if config.relay else None,
        "snapshot": vars(config.snapshot),
        "actors": vars(config.actors) if config.actors else None,
        "tools": config.tools,
        "tool_config": {name: dict(value.options) for name, value in config.tool_config.items()},
        "bounds": {
            "max_depth": config.max_depth,
            "branching_caps": config.branching_caps,
            "max_states": config.max_states,
        },
    }


def _invariant_payload(invariant: CrossChainInvariant) -> dict[str, Any]:
    return {
        "id": invariant.id,
        "contexts": list(invariant.contexts),
        "entry": invariant.entry_context,
        "correlation": invariant.correlation_extractor_id,
        "bindings": [binding.id for binding in invariant.bindings],
        "property": invariant.property.predicate.predicate.canonical(),
        "observation": invariant.observation_policy.kind.value,
    }

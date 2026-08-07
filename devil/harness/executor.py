"""Canonical, atomic execution of calls and lifecycle steps on branch-local forks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from devil.core.snapshot import JsonRpcClient, SnapshotSet
from devil.core.types import (
    ActorPolicy,
    Call,
    Candidate,
    CanonicalExecutionResult,
    ChainId,
    CodeChange,
    Constraint,
    CrossChainStep,
    EnvironmentTransition,
    Event,
    Evidence,
    ExecutionStatus,
    ForkSnapshot,
    GlobalState,
    Impact,
    MessageState,
    Outcome,
    RelayDataset,
    RelayTransition,
    RevertKind,
    SlotChange,
)


@dataclass(frozen=True)
class BackendCallResult:
    status: ExecutionStatus
    transaction_hash: str = ""
    revert_data: str = ""
    revert_kind: RevertKind | None = None
    state_diff: tuple[SlotChange, ...] = ()
    code_diff: tuple[CodeChange, ...] = ()
    events: tuple[Event, ...] = ()
    block_number: int = 0
    timestamp: int = 0
    evidence_raw: str = ""


class ForkBackend(Protocol):
    def checkpoint(self, chain: ChainId) -> str: ...

    def revert(self, chain: ChainId, checkpoint: str) -> None: ...

    def apply_call(
        self, snapshot: ForkSnapshot, call: Call, target_address: str
    ) -> BackendCallResult: ...

    def advance(
        self, snapshot: ForkSnapshot, transition: EnvironmentTransition
    ) -> ForkSnapshot: ...
    def restore(
        self,
        state: GlobalState,
        targets: Mapping[ChainId, Mapping[str, str]],
    ) -> None: ...


class JsonRpcForkBackend:
    """Canonical backend for local Anvil-compatible fork RPC endpoints."""

    def __init__(self, clients: Mapping[ChainId, JsonRpcClient]) -> None:
        self._clients = dict(clients)

    def checkpoint(self, chain: ChainId) -> str:
        return str(self._client(chain).call("evm_snapshot", []))

    def revert(self, chain: ChainId, checkpoint: str) -> None:
        if not self._client(chain).call("evm_revert", [checkpoint]):
            raise RuntimeError(f"failed to revert {chain.value} checkpoint")

    def apply_call(
        self, snapshot: ForkSnapshot, call: Call, target_address: str
    ) -> BackendCallResult:
        client = self._client(snapshot.chain_id)
        actor = call.actor
        sender = actor.address if actor else "0x0000000000000000000000000000000000000001"
        if actor and actor.impersonation_allowed:
            client.call("anvil_impersonateAccount", [sender])
            if actor.funding_method == "deal":
                client.call("anvil_setBalance", [sender, hex(max(call.value, 10**20))])
        transaction: dict[str, str] = {
            "from": sender,
            "to": target_address,
            "data": call.calldata,
            "value": hex(call.value),
        }
        if call.gas_limit:
            transaction["gas"] = hex(call.gas_limit)
        try:
            transaction_hash = str(client.call("eth_sendTransaction", [transaction]))
            receipt = client.call("eth_getTransactionReceipt", [transaction_hash])
        except Exception as exc:
            return BackendCallResult(
                ExecutionStatus.REVERTED,
                revert_data=str(exc),
                revert_kind=RevertKind.EVM_REVERT,
            )
        if not isinstance(receipt, Mapping):
            raise RuntimeError("fork backend returned no transaction receipt")
        if int(str(receipt.get("status", "0x0")), 16) == 0:
            return BackendCallResult(
                ExecutionStatus.REVERTED,
                transaction_hash=transaction_hash,
                revert_kind=RevertKind.EVM_REVERT,
                evidence_raw=json.dumps(receipt, sort_keys=True, default=str),
            )
        block_number = int(str(receipt["blockNumber"]), 16)
        block = client.call("eth_getBlockByNumber", [hex(block_number), False])
        timestamp = int(str(block["timestamp"]), 16)
        events = tuple(
            Event(
                context_id=call.context_id,
                signature=str(log.get("topics", [""])[0]),
                fields={"topics": tuple(log.get("topics", [])), "data": log.get("data", "0x")},
                transaction_hash=transaction_hash,
                log_index=int(str(log.get("logIndex", "0x0")), 16),
            )
            for log in receipt.get("logs", [])
        )
        state_diff, code_diff = self._trace_diff(client, transaction_hash)
        return BackendCallResult(
            ExecutionStatus.APPLIED,
            transaction_hash=transaction_hash,
            state_diff=state_diff,
            code_diff=code_diff,
            events=events,
            block_number=block_number,
            timestamp=timestamp,
            evidence_raw=json.dumps(receipt, sort_keys=True, default=str),
        )

    def advance(self, snapshot: ForkSnapshot, transition: EnvironmentTransition) -> ForkSnapshot:
        client = self._client(snapshot.chain_id)
        if transition.target_block < snapshot.block_number:
            raise ValueError("environment transition cannot move block backwards")
        if transition.target_timestamp < snapshot.timestamp:
            raise ValueError("environment transition cannot move time backwards")
        client.call("evm_setNextBlockTimestamp", [transition.target_timestamp])
        for _ in range(max(1, transition.target_block - snapshot.block_number)):
            client.call("evm_mine", [])
        return replace(
            snapshot,
            block_number_delta=transition.target_block - snapshot.base_block,
            timestamp_delta=transition.target_timestamp - snapshot.base_timestamp,
            overlay_id=snapshot.overlay_id + 1,
        )

    def restore(
        self,
        state: GlobalState,
        targets: Mapping[ChainId, Mapping[str, str]],
    ) -> None:
        """Reset local forks and deterministically replay the selected branch."""
        for chain, expected in state.chain_snapshots.items():
            client = self._client(chain)
            client.call("anvil_reset", [])
            current = replace(
                expected,
                state_diff=(),
                code_diff=(),
                emitted_logs=(),
                block_number_delta=0,
                timestamp_delta=0,
                overlay_id=0,
            )
            for step in state.trace:
                if step.chain is not chain or isinstance(step, RelayTransition):
                    continue
                if isinstance(step, EnvironmentTransition):
                    current = self.advance(current, step)
                    continue
                address = targets.get(chain, {}).get(step.context_id)
                if not address:
                    raise RuntimeError(f"cannot restore unverified context {step.context_id!r}")
                result = self.apply_call(current, step, address)
                if result.status is not ExecutionStatus.APPLIED:
                    raise RuntimeError("canonical branch replay reverted")
                current = _apply_backend_result(current, result)
            if _snapshot_execution_identity(current) != _snapshot_execution_identity(expected):
                raise RuntimeError(f"canonical branch replay diverged on {chain.value}")

    def _client(self, chain: ChainId) -> JsonRpcClient:
        try:
            return self._clients[chain]
        except KeyError as exc:
            raise RuntimeError(f"no fork backend for {chain.value}") from exc

    @staticmethod
    def _trace_diff(
        client: JsonRpcClient, transaction_hash: str
    ) -> tuple[tuple[SlotChange, ...], tuple[CodeChange, ...]]:
        try:
            trace = client.call(
                "debug_traceTransaction",
                [
                    transaction_hash,
                    {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}},
                ],
            )
        except Exception:
            return (), ()
        if not isinstance(trace, Mapping):
            return (), ()
        pre = trace.get("pre", {}) if isinstance(trace.get("pre", {}), Mapping) else {}
        post = trace.get("post", {}) if isinstance(trace.get("post", {}), Mapping) else {}
        slots: list[SlotChange] = []
        codes: list[CodeChange] = []
        for address in sorted(set(pre) | set(post)):
            before = pre.get(address, {}) if isinstance(pre.get(address, {}), Mapping) else {}
            after = post.get(address, {}) if isinstance(post.get(address, {}), Mapping) else {}
            before_storage = (
                before.get("storage", {}) if isinstance(before.get("storage", {}), Mapping) else {}
            )
            after_storage = (
                after.get("storage", {}) if isinstance(after.get("storage", {}), Mapping) else {}
            )
            for slot in sorted(set(before_storage) | set(after_storage)):
                old = str(before_storage.get(slot, "0x0"))
                new = str(after_storage.get(slot, "0x0"))
                if old != new:
                    slots.append(SlotChange(address, str(slot), old, new))
            old_code = str(before.get("code", "0x"))
            new_code = str(after.get("code", "0x"))
            if old_code != new_code:
                codes.append(
                    CodeChange(address, _portable_hash(old_code), _portable_hash(new_code))
                )
        return tuple(slots), tuple(codes)


class InMemoryForkBackend:
    """Deterministic backend for integration tests and protocol adapters."""

    def __init__(
        self,
        handler: Callable[[ForkSnapshot, Call, str], BackendCallResult],
        snapshots: Mapping[ChainId, ForkSnapshot],
    ) -> None:
        self._handler = handler
        self._snapshots = dict(snapshots)
        self._checkpoints: dict[str, dict[ChainId, ForkSnapshot]] = {}
        self._counter = 0

    def checkpoint(self, chain: ChainId) -> str:
        self._counter += 1
        identity = f"memory:{self._counter}"
        self._checkpoints[identity] = dict(self._snapshots)
        return identity

    def revert(self, chain: ChainId, checkpoint: str) -> None:
        try:
            self._snapshots = self._checkpoints.pop(checkpoint)
        except KeyError as exc:
            raise RuntimeError(f"unknown in-memory checkpoint {checkpoint}") from exc

    def apply_call(
        self, snapshot: ForkSnapshot, call: Call, target_address: str
    ) -> BackendCallResult:
        result = self._handler(snapshot, call, target_address)
        if result.status is ExecutionStatus.APPLIED:
            self._snapshots[snapshot.chain_id] = replace(
                snapshot,
                state_diff=snapshot.state_diff + result.state_diff,
                code_diff=snapshot.code_diff + result.code_diff,
                emitted_logs=snapshot.emitted_logs + result.events,
                block_number_delta=max(0, result.block_number - snapshot.base_block),
                timestamp_delta=max(0, result.timestamp - snapshot.base_timestamp),
                overlay_id=snapshot.overlay_id + 1,
            )
        return result

    def advance(self, snapshot: ForkSnapshot, transition: EnvironmentTransition) -> ForkSnapshot:
        updated = replace(
            snapshot,
            block_number_delta=transition.target_block - snapshot.base_block,
            timestamp_delta=transition.target_timestamp - snapshot.base_timestamp,
            overlay_id=snapshot.overlay_id + 1,
        )
        self._snapshots[snapshot.chain_id] = updated
        return updated

    def restore(
        self,
        state: GlobalState,
        targets: Mapping[ChainId, Mapping[str, str]],
    ) -> None:
        self._snapshots = {
            chain: replace(
                snapshot,
                state_diff=(),
                code_diff=(),
                emitted_logs=(),
                block_number_delta=0,
                timestamp_delta=0,
                overlay_id=0,
            )
            for chain, snapshot in state.chain_snapshots.items()
        }
        for step in state.trace:
            if isinstance(step, RelayTransition):
                continue
            snapshot = self._snapshots[step.chain]
            if isinstance(step, EnvironmentTransition):
                self.advance(snapshot, step)
                continue
            address = targets.get(step.chain, {}).get(step.context_id)
            if not address:
                raise RuntimeError(f"cannot restore unverified context {step.context_id!r}")
            result = self.apply_call(snapshot, step, address)
            if result.status is not ExecutionStatus.APPLIED:
                raise RuntimeError("in-memory branch replay reverted")


class PrefixOutcome(StrEnum):
    COMPLETED = "completed"
    REVERTED = "reverted"
    DEPTH_BOUND = "depth_bound"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"
    PARTIAL = "partial"


@dataclass(frozen=True)
class AppliedPrefix:
    prefix: tuple[CrossChainStep, ...]
    executed_step: CrossChainStep
    before_state: GlobalState
    after_state: GlobalState
    chain: ChainId
    branch_id: str
    parent_branch_id: str
    events: tuple[Event, ...]
    constraints: tuple[Constraint, ...]
    evidence: tuple[Evidence, ...]
    impact: Impact | None


@dataclass(frozen=True)
class CandidatePrefixResult:
    applied_prefixes: tuple[AppliedPrefix, ...]
    steps_attempted: int
    terminal_outcome: PrefixOutcome


class Coordinator(Protocol):
    policy_hash: str
    policy: Any

    def initial_states(self) -> dict[str, MessageState]: ...

    def propose_transitions(
        self, state: GlobalState, chain: ChainId | None = None
    ) -> tuple[Candidate, ...]: ...

    def next_chain(self, current: ChainId, state: GlobalState) -> ChainId: ...

    def validate_environment(
        self, state: GlobalState, transition: EnvironmentTransition
    ) -> None: ...

    def apply_transition(
        self, state: GlobalState, transition: RelayTransition
    ) -> tuple[GlobalState, tuple[Evidence, ...]]: ...


class CanonicalForkExecutor:
    """Sole authority for reachability, state diffs, events, and action traces."""

    def __init__(
        self,
        snapshot_set: SnapshotSet,
        backend: ForkBackend,
        *,
        relay_dataset: RelayDataset,
        actor_policy: ActorPolicy,
        coordinator: Coordinator,
        observe: Callable[[GlobalState], Mapping[str, Any]] | None = None,
    ) -> None:
        self.snapshot_set = snapshot_set
        self.backend = backend
        self.relay_dataset = relay_dataset
        self.actor_policy = actor_policy
        self.coordinator = coordinator
        self.observe = observe
        self._targets = {
            chain: {context: target.address for context, target in fingerprint.targets.items()}
            for chain, fingerprint in snapshot_set.base_fingerprints.items()
        }

    def initial_state(self) -> GlobalState:
        state = GlobalState(
            chain_snapshots=self.snapshot_set.snapshots,
            snapshot_set_id=self.snapshot_set.id,
            pending_messages=self.coordinator.initial_states(),
            relay_dataset_hash=self.relay_dataset.dataset_hash,
            relay_policy_hash=self.coordinator.policy_hash,
            relay_mode=self.coordinator.policy.mode,
            actor_policy=self.actor_policy,
        )
        return state.with_observed_values(self.observe(state)) if self.observe else state

    def restore(self, state: GlobalState) -> None:
        restore = getattr(self.backend, "restore", None)
        if restore is not None:
            restore(state, self._targets)

    def apply_step(self, state: GlobalState, step: CrossChainStep) -> CanonicalExecutionResult:
        if isinstance(step, RelayTransition):
            try:
                updated, evidence = self.coordinator.apply_transition(state, step)
            except ValueError as exc:
                return CanonicalExecutionResult(
                    Outcome.SUCCESS,
                    ExecutionStatus.REVERTED,
                    reason=str(exc),
                )
            updated = replace(
                updated, trace=state.trace + (step,), budget_used=state.budget_used + 1
            )
            if self.observe:
                updated = updated.with_observed_values(self.observe(updated))
            return CanonicalExecutionResult(
                Outcome.SUCCESS,
                ExecutionStatus.APPLIED,
                global_state=updated,
                evidence=evidence,
            )
        chain = step.chain
        snapshot = state.chain_snapshots[chain]
        checkpoint = self.backend.checkpoint(chain)
        try:
            if isinstance(step, EnvironmentTransition):
                self.coordinator.validate_environment(state, step)
                updated_snapshot = self.backend.advance(snapshot, step)
                result = BackendCallResult(
                    ExecutionStatus.APPLIED,
                    block_number=updated_snapshot.block_number,
                    timestamp=updated_snapshot.timestamp,
                )
            else:
                if step.actor and not self.actor_policy.permits(step.actor):
                    raise ValueError("actor is not allowed by the campaign actor policy")
                target = self.snapshot_set.base_fingerprints[chain].targets.get(step.context_id)
                if target is None:
                    raise ValueError(f"call context {step.context_id!r} is not a verified target")
                if not step.calldata.startswith("0x"):
                    raise ValueError("canonical call requires encoded calldata")
                result = self.backend.apply_call(snapshot, step, target.address)
            if result.status is ExecutionStatus.REVERTED:
                self.backend.revert(chain, checkpoint)
                return CanonicalExecutionResult(
                    Outcome.SUCCESS,
                    ExecutionStatus.REVERTED,
                    revert_data=result.revert_data,
                    revert_kind=result.revert_kind,
                    reason="canonical call reverted",
                )
            updated_snapshot = replace(
                snapshot,
                state_diff=snapshot.state_diff + result.state_diff,
                code_diff=snapshot.code_diff + result.code_diff,
                emitted_logs=snapshot.emitted_logs + result.events,
                block_number_delta=max(0, result.block_number - snapshot.base_block),
                timestamp_delta=max(0, result.timestamp - snapshot.base_timestamp),
                overlay_id=snapshot.overlay_id + 1,
            )
            updated = replace(
                state.with_snapshot(chain, updated_snapshot),
                trace=state.trace + (step,),
                budget_used=state.budget_used + 1,
            )
            if self.observe:
                updated = updated.with_observed_values(self.observe(updated))
            evidence = (
                Evidence(
                    "canonical_executor",
                    Outcome.SUCCESS,
                    result.evidence_raw,
                    _portable_hash(result.evidence_raw),
                ),
            )
            return CanonicalExecutionResult(
                Outcome.SUCCESS,
                ExecutionStatus.APPLIED,
                global_state=updated,
                events=result.events,
                evidence=evidence,
            )
        except (ValueError, RuntimeError) as exc:
            self.backend.revert(chain, checkpoint)
            return CanonicalExecutionResult(Outcome.TOOL_ERROR, reason=str(exc))

    def execute_candidate_prefixes(
        self,
        base_state: GlobalState,
        candidate: Candidate,
        *,
        max_steps: int,
        budget: int,
        parent_branch_id: str,
    ) -> CandidatePrefixResult:
        self.restore(base_state)
        prefixes: list[AppliedPrefix] = []
        current = base_state
        attempted = 0
        terminal = PrefixOutcome.COMPLETED
        for index, step in enumerate(candidate.call_sequence):
            if index >= max_steps:
                terminal = PrefixOutcome.DEPTH_BOUND
                break
            if attempted >= budget:
                terminal = PrefixOutcome.BUDGET_EXHAUSTED
                break
            attempted += 1
            before = current
            result = self.apply_step(before, step)
            if result.outcome is not Outcome.SUCCESS:
                terminal = PrefixOutcome(result.outcome.value)
                break
            if (
                result.execution_status is not ExecutionStatus.APPLIED
                or result.global_state is None
            ):
                terminal = PrefixOutcome.REVERTED
                break
            current = result.global_state
            branch_id = _branch_id(parent_branch_id, current, index)
            chain = step.chain
            prefixes.append(
                AppliedPrefix(
                    prefix=candidate.call_sequence[: index + 1],
                    executed_step=step,
                    before_state=before,
                    after_state=current,
                    chain=chain,
                    branch_id=branch_id,
                    parent_branch_id=parent_branch_id,
                    events=result.events,
                    constraints=result.constraints,
                    evidence=result.evidence,
                    impact=result.impact,
                )
            )
        return CandidatePrefixResult(tuple(prefixes), attempted, terminal)


def _branch_id(parent: str, state: GlobalState, index: int) -> str:
    return hashlib.sha256(f"{parent}|{index}|{canonical_state_hash(state)}".encode()).hexdigest()[
        :16
    ]


def _apply_backend_result(snapshot: ForkSnapshot, result: BackendCallResult) -> ForkSnapshot:
    return replace(
        snapshot,
        state_diff=snapshot.state_diff + result.state_diff,
        code_diff=snapshot.code_diff + result.code_diff,
        emitted_logs=snapshot.emitted_logs + result.events,
        block_number_delta=max(0, result.block_number - snapshot.base_block),
        timestamp_delta=max(0, result.timestamp - snapshot.base_timestamp),
        overlay_id=snapshot.overlay_id + 1,
    )


def _snapshot_execution_identity(snapshot: ForkSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.block_number,
        snapshot.timestamp,
        snapshot.state_diff,
        snapshot.code_diff,
    )


def canonical_state_hash(state: GlobalState) -> str:
    """Hash future-relevant state; deliberately exclude trace and opaque handles."""
    payload = {
        "snapshot_set": state.snapshot_set_id,
        "snapshots": {
            chain.value: {
                "base": snapshot.base_block_hash,
                "state_root": snapshot.state_root,
                "state_diff": [
                    (change.contract, change.slot, change.old_value, change.new_value)
                    for change in snapshot.state_diff
                ],
                "code_diff": [
                    (change.context_id, change.old_code_hash, change.new_code_hash)
                    for change in snapshot.code_diff
                ],
                "events": [
                    (event.context_id, event.signature, dict(event.fields))
                    for event in snapshot.emitted_logs
                ],
                "block": snapshot.block_number,
                "timestamp": snapshot.timestamp,
            }
            for chain, snapshot in sorted(
                state.chain_snapshots.items(), key=lambda item: item[0].value
            )
        },
        "messages": {
            identity: {
                "status": message.status.value,
                "history": [record.action.value for record in message.transition_history],
            }
            for identity, message in sorted(state.pending_messages.items())
        },
        "relay_dataset": state.relay_dataset_hash,
        "observation_set": state.observation_set_hash,
        "relay_policy": state.relay_policy_hash,
        "relay_mode": state.relay_mode.value,
        "actor_policy": state.actor_policy.id if state.actor_policy else "",
        "assumptions": [str(item) for item in state.assumptions],
        "liveness": {
            identity: obligation.status.value
            for identity, obligation in sorted(state.liveness_obligations.items())
        },
        "observed_values": dict(sorted(state.observed_values.items())),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _portable_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

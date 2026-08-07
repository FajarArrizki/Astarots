"""Policy-guarded causal cross-chain message coordinator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from devil.core.config import RelayConfig
from devil.core.types import (
    Candidate,
    ChainId,
    EnvironmentReason,
    EnvironmentTransition,
    Evidence,
    GlobalState,
    MessageState,
    MessageStatus,
    Outcome,
    RelayAction,
    RelayDataset,
    RelayMessage,
    RelayMode,
    RelayTransition,
    RelayTransitionRecord,
    frozen_mapping,
)


class RelayError(ValueError):
    """Raised when a lifecycle transition violates relay policy."""


@dataclass(frozen=True)
class RelayDelivery:
    message: RelayMessage
    applied: bool
    reason: str = ""


@dataclass(frozen=True)
class RelayLedger:
    """Immutable lifecycle state for every message in a content-addressed dataset."""

    dataset: RelayDataset
    policy: RelayConfig
    states: Mapping[str, MessageState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", frozen_mapping(self.states))

    @classmethod
    def from_dataset(cls, dataset: RelayDataset, policy: RelayConfig) -> RelayLedger:
        _validate_dataset_hash(dataset, policy)
        states = {
            message.identity: MessageState(
                message,
                MessageStatus.EXPIRED
                if message.destination_status == "expired"
                else MessageStatus.DELIVERED
                if message.destination_status == "delivered"
                else MessageStatus.EMITTED,
            )
            for message in dataset.messages
        }
        return cls(dataset, policy, states)

    @property
    def mode(self) -> RelayMode:
        return self.policy.mode

    @property
    def delivered(self) -> frozenset[str]:
        return frozenset(
            identity
            for identity, state in self.states.items()
            if state.status in {MessageStatus.DELIVERED, MessageStatus.CONSUMED}
        )

    @property
    def rejected(self) -> frozenset[str]:
        return frozenset(
            identity
            for identity, state in self.states.items()
            if state.status is MessageStatus.REJECTED
        )

    @property
    def pending(self) -> frozenset[str]:
        terminal = {MessageStatus.CONSUMED, MessageStatus.REJECTED, MessageStatus.EXPIRED}
        return frozenset(
            identity for identity, state in self.states.items() if state.status not in terminal
        )

    def message(self, identity: str) -> RelayMessage:
        try:
            return self.states[identity].envelope
        except KeyError as exc:
            raise RelayError(f"message {identity!r} is not in the relay dataset") from exc

    def deliver(self, identity: str) -> tuple[RelayLedger, RelayDelivery]:
        state = self.states.get(identity)
        if state is None:
            raise RelayError(f"message {identity!r} is not in the relay dataset")
        if state.status is MessageStatus.DELIVERED:
            if self.policy.duplicate_delivery == "reject":
                raise RelayError(f"message {identity!r} was already delivered")
            return self, RelayDelivery(state.envelope, False, "duplicate delivery allowed for test")
        if state.status is not MessageStatus.RELAY_ELIGIBLE:
            raise RelayError(f"message {identity!r} is not relay eligible")
        self._validate_authenticity(state.envelope)
        updated = replace(state, status=MessageStatus.DELIVERED)
        states = dict(self.states)
        states[identity] = updated
        return replace(self, states=states), RelayDelivery(state.envelope, True)

    def reject(self, identity: str, reason: str) -> RelayLedger:
        state = self.states.get(identity)
        if state is None:
            raise RelayError(f"message {identity!r} is not in the relay dataset")
        states = dict(self.states)
        states[identity] = replace(state, status=MessageStatus.REJECTED)
        return replace(self, states=states)

    def _validate_authenticity(self, message: RelayMessage) -> None:
        _validate_authenticity(self.dataset, self.policy, message)

    def quiescent(self) -> bool:
        return not self.pending

    def pending_messages(self) -> tuple[RelayMessage, ...]:
        return tuple(self.states[identity].envelope for identity in sorted(self.pending))


class MessageCoordinator:
    """Advance message lifecycle only when policy, clocks, and evidence permit it."""

    def __init__(self, dataset: RelayDataset, policy: RelayConfig) -> None:
        _validate_dataset_hash(dataset, policy)
        self.dataset = dataset
        self.policy = policy
        self._dataset_index = {message.identity: message for message in dataset.messages}
        self.policy_hash = relay_policy_hash(policy)

    def initial_states(self) -> dict[str, MessageState]:
        return dict(RelayLedger.from_dataset(self.dataset, self.policy).states)

    def register_emitted(self, state: GlobalState, message: RelayMessage) -> GlobalState:
        messages = dict(state.pending_messages)
        if message.identity in messages:
            raise RelayError(f"message {message.identity!r} already exists in this branch")
        messages[message.identity] = MessageState(message)
        return replace(state, pending_messages=messages)

    def propose_transitions(
        self, state: GlobalState, chain: ChainId | None = None
    ) -> tuple[Candidate, ...]:
        candidates: list[Candidate] = []
        for identity, message_state in sorted(state.pending_messages.items()):
            envelope = message_state.envelope
            if chain is not None and chain not in {
                envelope.source_chain,
                envelope.destination_chain,
            }:
                continue
            action = self._next_action(state, message_state)
            if isinstance(action, (RelayTransition, EnvironmentTransition)):
                candidates.append(
                    Candidate(
                        target_function=f"relay:{action.action.value}"
                        if isinstance(action, RelayTransition)
                        else f"environment:{action.reason.value}",
                        call_sequence=(action,),
                        suspicion=1.0,
                        chain=action.destination_chain
                        if isinstance(action, RelayTransition)
                        else action.chain,
                    )
                )
        return tuple(candidates)

    def validate_environment(self, state: GlobalState, transition: EnvironmentTransition) -> None:
        if transition.policy_ref != self.policy_hash:
            raise RelayError("environment transition policy identity mismatch")
        proposed = self.propose_transitions(state, transition.chain)
        if not any(candidate.call_sequence == (transition,) for candidate in proposed):
            raise RelayError("environment transition does not reach a declared policy boundary")

    def apply_transition(
        self, state: GlobalState, transition: RelayTransition
    ) -> tuple[GlobalState, tuple[Evidence, ...]]:
        try:
            message_state = state.pending_messages[transition.message_id]
        except KeyError as exc:
            raise RelayError(f"unknown message {transition.message_id!r}") from exc
        envelope = message_state.envelope
        if transition.source_chain is not envelope.source_chain:
            raise RelayError("relay transition source chain mismatch")
        if transition.destination_chain is not envelope.destination_chain:
            raise RelayError("relay transition destination chain mismatch")
        if (
            transition.relay_mode is not self.policy.mode
            or transition.policy_ref != self.policy_hash
        ):
            raise RelayError("relay transition policy identity mismatch")
        if message_state.status is not transition.from_status:
            raise RelayError(
                "message status mismatch "
                f"({message_state.status.value} != {transition.from_status.value})"
            )
        expected = _target_status(transition.action)
        if transition.to_status is not expected:
            raise RelayError("relay transition target status mismatch")
        self._guard(state, message_state, transition.action)
        destination_snapshot = state.chain_snapshots[envelope.destination_chain]
        record = RelayTransitionRecord(
            transition.action,
            transition.from_status,
            transition.to_status,
            destination_snapshot.block_number,
            destination_snapshot.timestamp,
            self.policy_hash,
        )
        updated_message = replace(
            message_state,
            status=transition.to_status,
            transition_history=message_state.transition_history + (record,),
        )
        messages = dict(state.pending_messages)
        messages[transition.message_id] = updated_message
        evidence_payload = json.dumps(
            {
                "message_id": transition.message_id,
                "action": transition.action.value,
                "from": transition.from_status.value,
                "to": transition.to_status.value,
                "policy": self.policy_hash,
                "mode": self.policy.mode.value,
            },
            sort_keys=True,
        )
        evidence = Evidence(
            "message_coordinator",
            Outcome.SUCCESS,
            evidence_payload,
            "sha256:" + hashlib.sha256(evidence_payload.encode()).hexdigest(),
        )
        return replace(state, pending_messages=messages), (evidence,)

    def quiescent(self, state: GlobalState, rule: Any) -> bool:
        statuses = [message.status for message in state.pending_messages.values()]
        if rule.exclude_expired:
            statuses = [status for status in statuses if status is not MessageStatus.EXPIRED]
        if rule.exclude_rejected:
            statuses = [status for status in statuses if status is not MessageStatus.REJECTED]
        if rule.kind.value == "no_eligible_messages":
            return MessageStatus.RELAY_ELIGIBLE not in statuses
        if rule.kind.value == "no_pending_messages":
            terminal = {MessageStatus.CONSUMED, MessageStatus.REJECTED, MessageStatus.EXPIRED}
            return all(status in terminal for status in statuses)
        if rule.max_pending_age is None:
            raise RelayError("bounded quiescence requires max_pending_age")
        return all(
            self._age(state, item.envelope) >= rule.max_pending_age.value
            for item in state.pending_messages.values()
            if item.status
            not in {MessageStatus.CONSUMED, MessageStatus.REJECTED, MessageStatus.EXPIRED}
        )

    def next_chain(self, current: ChainId, state: GlobalState) -> ChainId:
        eligible_destinations = sorted(
            {
                item.envelope.destination_chain
                for item in state.pending_messages.values()
                if item.status in {MessageStatus.RELAY_ELIGIBLE, MessageStatus.DELIVERED}
            },
            key=lambda chain: chain.value,
        )
        if eligible_destinations:
            return eligible_destinations[0]
        chains = sorted(state.chain_snapshots, key=lambda chain: chain.value)
        index = chains.index(current)
        return chains[(index + 1) % len(chains)]

    def _next_action(
        self, state: GlobalState, message_state: MessageState
    ) -> RelayTransition | EnvironmentTransition | None:
        envelope = message_state.envelope
        source = state.chain_snapshots[envelope.source_chain]
        destination = state.chain_snapshots[envelope.destination_chain]
        finality = self.policy.finality_blocks.get(envelope.source_chain.value, 0)
        if message_state.status is MessageStatus.EMITTED:
            target_block = envelope.source_block_number + finality
            if source.block_number < target_block:
                return EnvironmentTransition(
                    envelope.source_chain,
                    target_block,
                    source.timestamp,
                    EnvironmentReason.FINALITY,
                    self.policy_hash,
                )
            return self._transition(message_state, RelayAction.FINALIZE)
        if message_state.status is MessageStatus.SOURCE_FINALIZED:
            minimum = self.policy.min_delay_seconds.get(envelope.destination_chain.value, 0)
            target_timestamp = envelope.emitted_timestamp + minimum
            if destination.timestamp < target_timestamp:
                return EnvironmentTransition(
                    envelope.destination_chain,
                    destination.block_number,
                    target_timestamp,
                    EnvironmentReason.RELAY_DELAY,
                    self.policy_hash,
                )
            return self._transition(message_state, RelayAction.MAKE_ELIGIBLE)
        if message_state.status is MessageStatus.RELAY_ELIGIBLE:
            if self._expired(state, envelope):
                return self._transition(message_state, RelayAction.EXPIRE)
            return self._transition(message_state, RelayAction.DELIVER)
        return None

    def _transition(self, state: MessageState, action: RelayAction) -> RelayTransition:
        return RelayTransition(
            state.envelope.identity,
            action,
            state.status,
            _target_status(action),
            state.envelope.source_chain,
            state.envelope.destination_chain,
            self.policy.mode,
            self.policy_hash,
        )

    def _guard(self, state: GlobalState, message: MessageState, action: RelayAction) -> None:
        envelope = message.envelope
        source = state.chain_snapshots[envelope.source_chain]
        destination = state.chain_snapshots[envelope.destination_chain]
        allowed_from = {
            RelayAction.FINALIZE: MessageStatus.EMITTED,
            RelayAction.MAKE_ELIGIBLE: MessageStatus.SOURCE_FINALIZED,
            RelayAction.DELIVER: MessageStatus.RELAY_ELIGIBLE,
            RelayAction.CONSUME: MessageStatus.DELIVERED,
            RelayAction.REJECT: MessageStatus.DELIVERED,
            RelayAction.EXPIRE: MessageStatus.RELAY_ELIGIBLE,
        }[action]
        if message.status is not allowed_from:
            raise RelayError(f"{action.value} is invalid from {message.status.value}")
        if action is RelayAction.FINALIZE:
            boundary = envelope.source_block_number + self.policy.finality_blocks.get(
                envelope.source_chain.value, 0
            )
            if source.block_number < boundary:
                raise RelayError("source message has not reached finality")
        elif action is RelayAction.MAKE_ELIGIBLE:
            minimum = self.policy.min_delay_seconds.get(envelope.destination_chain.value, 0)
            if destination.timestamp < envelope.emitted_timestamp + minimum:
                raise RelayError("minimum relay delay has not elapsed")
            _validate_authenticity(self.dataset, self.policy, envelope)
        elif action is RelayAction.DELIVER:
            if self._expired(state, envelope):
                raise RelayError("message delivery deadline has expired")
            self._validate_ordering(state, envelope)
        elif action is RelayAction.EXPIRE and not self._expired(state, envelope):
            raise RelayError("message has not reached its expiry boundary")

    def _validate_ordering(self, state: GlobalState, envelope: RelayMessage) -> None:
        if self.policy.ordering != "fifo_per_emitter":
            return
        earlier = [
            item
            for item in state.pending_messages.values()
            if item.envelope.emitter.lower() == envelope.emitter.lower()
            and item.envelope.sequence < envelope.sequence
            and item.status
            not in {
                MessageStatus.DELIVERED,
                MessageStatus.CONSUMED,
                MessageStatus.REJECTED,
                MessageStatus.EXPIRED,
            }
        ]
        if earlier:
            raise RelayError("FIFO ordering blocks delivery before earlier emitter sequence")

    def _expired(self, state: GlobalState, envelope: RelayMessage) -> bool:
        if self.policy.delivery_deadline is None:
            maximum = self.policy.max_delay_seconds.get(envelope.destination_chain.value)
            if maximum is None:
                return False
            return self._age(state, envelope) > maximum
        deadline = self.policy.delivery_deadline
        chain = ChainId(deadline.chain_id) if deadline.chain_id else envelope.destination_chain
        snapshot = state.chain_snapshots[chain]
        if deadline.unit.startswith("block"):
            return snapshot.block_number > envelope.source_block_number + deadline.value
        return snapshot.timestamp > envelope.emitted_timestamp + deadline.value

    @staticmethod
    def _age(state: GlobalState, envelope: RelayMessage) -> int:
        return max(
            0,
            state.chain_snapshots[envelope.destination_chain].timestamp
            - envelope.emitted_timestamp,
        )


def relay_policy_hash(policy: RelayConfig) -> str:
    payload: dict[str, Any] = {
        "mode": policy.mode.value,
        "adapter": policy.protocol_adapter,
        "adapter_config_hash": policy.adapter_config_hash,
        "finality": dict(policy.finality_blocks),
        "delay_model": policy.delay_model,
        "min_delay": dict(policy.min_delay_seconds),
        "max_delay": dict(policy.max_delay_seconds),
        "ordering": policy.ordering,
        "duplicate": policy.duplicate_delivery,
        "reorg": policy.reorg_assumption,
        "deadline": vars(policy.delivery_deadline) if policy.delivery_deadline else None,
        "epoch_rules": dict(policy.protocol_epoch_rules),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _target_status(action: RelayAction) -> MessageStatus:
    return {
        RelayAction.FINALIZE: MessageStatus.SOURCE_FINALIZED,
        RelayAction.MAKE_ELIGIBLE: MessageStatus.RELAY_ELIGIBLE,
        RelayAction.DELIVER: MessageStatus.DELIVERED,
        RelayAction.CONSUME: MessageStatus.CONSUMED,
        RelayAction.REJECT: MessageStatus.REJECTED,
        RelayAction.EXPIRE: MessageStatus.EXPIRED,
    }[action]


def _validate_dataset_hash(dataset: RelayDataset, policy: RelayConfig) -> None:
    if dataset.dataset_hash != policy.dataset_hash:
        raise RelayError(
            f"relay dataset hash mismatch ({dataset.dataset_hash} != {policy.dataset_hash})"
        )


def _validate_authenticity(
    dataset: RelayDataset, policy: RelayConfig, message: RelayMessage
) -> None:
    if policy.mode is RelayMode.HISTORICAL_AUTHENTIC:
        indexed = {item.identity: item for item in dataset.messages}.get(message.identity)
        if indexed is None:
            raise RelayError("historical delivery requires an exact dataset envelope")
        fields = (
            "payload_hash",
            "source_event_hash",
            "attestation_hash",
            "source_block_hash",
        )
        missing = any(not getattr(message, field) for field in fields)
        if missing or message.source_log_index < 0:
            raise RelayError("historical envelope is missing authenticity evidence")
        if indexed != message:
            raise RelayError("historical envelope does not match the pinned dataset record")
    elif policy.mode is RelayMode.PROTOCOL_VALID_SYNTHETIC:
        if not policy.adapter_config_hash or not message.attestation_hash:
            raise RelayError("synthetic delivery requires a declared proof fixture and attestation")

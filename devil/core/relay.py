"""Deterministic relay lifecycle for cross-chain campaign branches."""

from __future__ import annotations

from dataclasses import dataclass, field

from devil.core.types import RelayDataset, RelayMessage, RelayMode


class RelayError(ValueError):
    """Raised when a relay transition violates its declared policy."""


@dataclass(frozen=True)
class RelayDelivery:
    """Result of one attempted relay transition."""

    message: RelayMessage
    applied: bool
    reason: str = ""


@dataclass(frozen=True)
class RelayLedger:
    """Branch-local message state; deliveries are append-only and idempotence-safe."""

    dataset: RelayDataset
    mode: RelayMode
    delivered: frozenset[str] = frozenset()
    rejected: frozenset[str] = frozenset()
    pending: frozenset[str] = frozenset()
    _messages: dict[str, RelayMessage] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dataset(cls, dataset: RelayDataset, mode: RelayMode) -> RelayLedger:
        messages = {message.identity: message for message in dataset.messages}
        if len(messages) != len(dataset.messages):
            raise RelayError("relay dataset contains duplicate message identities")
        return cls(dataset=dataset, mode=mode, pending=frozenset(messages), _messages=messages)

    def message(self, identity: str) -> RelayMessage:
        try:
            return self._messages[identity]
        except KeyError as exc:
            raise RelayError(f"message {identity!r} is not in the relay dataset") from exc

    def deliver(self, identity: str) -> tuple[RelayLedger, RelayDelivery]:
        message = self.message(identity)
        if identity in self.delivered:
            return self, RelayDelivery(message=message, applied=False, reason="already_delivered")
        if identity in self.rejected:
            return self, RelayDelivery(message=message, applied=False, reason="already_rejected")
        self._validate_authenticity(message)
        next_pending = self.pending - {identity}
        next_delivered = self.delivered | {identity}
        next_messages = dict(self._messages)
        next_messages[identity] = RelayMessage(
            emitter=message.emitter,
            sequence=message.sequence,
            payload=message.payload,
            vaa_bytes=message.vaa_bytes,
            vaa_hash=message.vaa_hash,
            guardian_set_index=message.guardian_set_index,
            destination_status="delivered",
            message_id=message.message_id,
            source_event_hash=message.source_event_hash,
            attestation_hash=message.attestation_hash,
        )
        return (
            RelayLedger(
                dataset=self.dataset,
                mode=self.mode,
                delivered=frozenset(next_delivered),
                rejected=self.rejected,
                pending=frozenset(next_pending),
                _messages=next_messages,
            ),
            RelayDelivery(message=next_messages[identity], applied=True),
        )

    def reject(self, identity: str, reason: str) -> RelayLedger:
        self.message(identity)
        if identity in self.delivered:
            raise RelayError(f"cannot reject delivered message {identity!r}")
        return RelayLedger(
            dataset=self.dataset,
            mode=self.mode,
            delivered=self.delivered,
            rejected=self.rejected | {identity},
            pending=self.pending - {identity},
            _messages=self._messages,
        )

    def _validate_authenticity(self, message: RelayMessage) -> None:
        if self.mode is RelayMode.HISTORICAL_AUTHENTIC:
            missing = [
                name
                for name, value in (
                    ("source_event_hash", message.source_event_hash),
                    ("attestation_hash", message.attestation_hash),
                    ("vaa_hash", message.vaa_hash),
                )
                if not value
            ]
            if missing:
                raise RelayError("historical-authentic message missing " + ", ".join(missing))
        elif self.mode is RelayMode.PROTOCOL_VALID_SYNTHETIC and not message.attestation_hash:
            raise RelayError("synthetic delivery requires a declared attestation fixture")

    def quiescent(self) -> bool:
        return not self.pending

    def pending_messages(self) -> tuple[RelayMessage, ...]:
        return tuple(self._messages[identity] for identity in sorted(self.pending))

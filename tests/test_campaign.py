from __future__ import annotations

from pathlib import Path

import pytest

from devil.core.types import ChainId
from devil.invariant.ir import (
    ObservationKind,
    PropertyKind,
    QuiescenceKind,
    load_invariant,
)


def _correlations():
    from devil.invariant.ir import CorrelationExtractor, EventSelector, TransformRef

    return {
        "bridge_message": CorrelationExtractor(
            EventSelector("ethereum.bridge", "Locked(bytes32,uint256)"),
            EventSelector("polygon.bridge", "Minted(bytes32,uint256)"),
            ("messageHash",),
            ("messageHash",),
            TransformRef("identity", "1"),
        )
    }


def test_loader_builds_authoritative_ir_without_silent_defaults(tmp_path: Path) -> None:
    source = tmp_path / "BridgeInvariant.t.sol"
    source.write_text(
        """
contract BridgeInvariant {
    /// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
    /// @transition ethereum.bridge:locked increase=["lock(bytes32,uint256)"]
    /// @transition polygon.bridge:minted increase=["mint(bytes32,uint256)"]
    /// @observation AFTER_ALL_DELIVERED """
        "quiescence=NO_ELIGIBLE_MESSAGES max_pending_age=ethereum:7200s "
        "exclude=expired,rejected"
        """
    /// @correlation bridge_message
    /// @bind locked=ethereum.bridge.locked(bytes32)[messageHash] """
        "minted=polygon.bridge.minted(bytes32)[messageHash]"
        """
    /// @quantify FORALL messageHash: locked == minted
    /// @observe touched,relay,historical max=256
    /// @assume signer_honesty: at_most_6_malicious
    /// @tools echidna,halmos
    /// @severity CRITICAL
    /// @timeout 900
    function invariant_locked_equals_minted() public view {
        assert(locked(messageHash) == minted(messageHash));
    }
}
"""
    )
    invariant = load_invariant(source, correlations=_correlations())
    assert invariant.entry_context == "ethereum.bridge"
    assert invariant.contexts["polygon.bridge"].chain_id is ChainId.POLYGON
    assert [binding.id for binding in invariant.bindings] == ["locked", "minted"]
    assert invariant.property.kind is PropertyKind.SAFETY
    assert invariant.property.predicate.bound_variables == ("messageHash",)
    assert invariant.observation_policy.kind is ObservationKind.AFTER_ALL_DELIVERED
    assert invariant.observation_policy.quiescence.kind is QuiescenceKind.NO_ELIGIBLE_MESSAGES
    assert invariant.observation_policy.quiescence.max_pending_age.value == 7200
    assert invariant.tool_allowlist == ("echidna", "halmos")
    assert invariant.severity == "CRITICAL"
    assert invariant.timeout_seconds == 900


def test_eventually_property_keeps_trigger_deadline_and_bound_key(tmp_path: Path) -> None:
    source = tmp_path / "Liveness.t.sol"
    source.write_text(
        """
contract Liveness {
    /// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
    /// @transition ethereum.bridge:status effect=set functions=["lock(bytes32)"]
    /// @observation BLOCK_BOUNDED deadline=polygon:128blocks
    /// @correlation bridge_message
    /// @bind status=polygon.bridge.status(bytes32)[messageHash]
    /// @eventually trigger="status[messageHash] == SourceFinalized" """
        'deadline=polygon:128blocks predicate="status[messageHash] == Consumed"'
        """
    /// @observe touched,relay max=128
    function invariant_consumed() public view {
        assert(status(messageHash) == Consumed);
    }
}
"""
    )
    invariant = load_invariant(source, correlations=_correlations())
    assert invariant.property.kind is PropertyKind.EVENTUALLY
    assert invariant.property.deadline.value == 128
    assert invariant.property.predicate.bound_variables == ("messageHash",)


def test_parser_fails_closed_on_assert_mismatch_and_unknown_correlation(tmp_path: Path) -> None:
    source = tmp_path / "Bad.t.sol"
    source.write_text(
        """
contract Bad {
    /// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
    /// @transition ethereum.bridge:locked increase=["lock()"]
    /// @transition polygon.bridge:minted increase=["mint()"]
    /// @observation PER_TRANSACTION
    /// @correlation missing
    /// @bind locked=ethereum.bridge.locked() minted=polygon.bridge.minted()
    /// @quantify FORALL key: locked == minted
    /// @observe touched max=1
    function invariant_bad() public view { assert(locked() >= minted()); }
}
"""
    )
    with pytest.raises(ValueError, match="unknown correlation extractor"):
        load_invariant(source, correlations=_correlations())

    text = source.read_text().replace("@correlation missing", "@correlation bridge_message")
    source.write_text(text)
    with pytest.raises(ValueError, match="Solidity assert does not match"):
        load_invariant(source, correlations=_correlations())

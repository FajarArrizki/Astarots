from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from devil.adapter import (
    ChainProjection,
    MaterializedSlot,
    ProjectionManifest,
    StateManifest,
    ToolRunResult,
    WitnessProjection,
)
from devil.adapter.echidna import EchidnaAdapter, EchidnaForkConfig
from devil.adapter.halmos import HalmosAdapter
from devil.adapter.slither import SlitherAdapter
from devil.core.types import Call, ChainId, Outcome


def _tool(path: Path, body: str) -> str:
    script = path / "tool.sh"
    script.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


def _projection(snapshot_set, chain: ChainId) -> ChainProjection:
    return ChainProjection(
        chain,
        snapshot_set.base_fingerprints[chain],
        StateManifest(),
    )


def test_echidna_returns_typed_candidates_and_uses_pinned_rpc_config(
    tmp_path: Path, monkeypatch, snapshot_set, actor
) -> None:
    capture = tmp_path / "captured.yml"
    payload = json.dumps(
        {
            "candidates": [
                {
                    "suspicion": 0.9,
                    "calls": [
                        {
                            "function": "submit(bytes)",
                            "context_id": "ethereum.bridge",
                            "calldata": "0x1234",
                            "args": [],
                        }
                    ],
                }
            ]
        },
        separators=(",", ":"),
    )
    body = f'''cp "$3" "{capture}"
printf '%s' '{payload}'
'''
    binary = _tool(tmp_path, body)
    target = tmp_path / "Target.sol"
    target.write_text("contract Target {}")
    monkeypatch.setenv("ETH_RPC_URL", "https://user:secret@example.invalid")
    result = EchidnaAdapter(binary=binary).probe(
        str(target),
        "invariant",
        (),
        _projection(snapshot_set, ChainId.ETHEREUM),
        ChainId.ETHEREUM,
        fork_config=EchidnaForkConfig("ETH_RPC_URL", 100, test_limit=123),
        context_id="ethereum.bridge",
        actor=actor,
    )
    assert isinstance(result, ToolRunResult)
    assert result.outcome is Outcome.COUNTEREXAMPLE
    assert result.value and result.value[0].call_sequence[0].actor == actor
    rendered = capture.read_text()
    assert 'rpcUrl: "https://user:secret@example.invalid"' in rendered
    assert "rpcBlock: 100" in rendered
    assert "testLimit: 123" in rendered
    assert "secret" not in result.evidence[0].raw


def test_echidna_uses_live_projected_branch_endpoint(tmp_path: Path, snapshot_set, actor) -> None:
    capture = tmp_path / "projected.yml"
    binary = _tool(
        tmp_path,
        f'''cp "$3" "{capture}"
printf '%s' '{{}}'
''',
    )
    target = tmp_path / "Target.sol"
    target.write_text("contract Target {}")
    projection = replace(
        _projection(snapshot_set, ChainId.ETHEREUM),
        block_number_delta=3,
    )
    result = EchidnaAdapter(binary=binary).probe(
        str(target),
        "invariant",
        (),
        projection,
        ChainId.ETHEREUM,
        fork_config=EchidnaForkConfig(
            "UNUSED_UPSTREAM_RPC",
            103,
            rpc_url="http://127.0.0.1:8545",
        ),
        context_id="ethereum.bridge",
        actor=actor,
    )
    assert result.outcome is Outcome.SUCCESS
    rendered = capture.read_text()
    assert 'rpcUrl: "http://127.0.0.1:8545"' in rendered
    assert "rpcBlock: 103" in rendered


def test_echidna_fails_closed_without_projection_actor_or_rpc(snapshot_set, tmp_path) -> None:
    target = tmp_path / "Target.sol"
    target.write_text("contract Target {}")
    result = EchidnaAdapter(binary="unused").probe(
        str(target),
        "invariant",
        (),
        _projection(snapshot_set, ChainId.ETHEREUM),
        ChainId.ETHEREUM,
        fork_config=EchidnaForkConfig("MISSING_RPC", 100),
    )
    assert result.outcome is Outcome.TOOL_ERROR
    assert result.diagnostics


def test_halmos_executes_an_explicit_materialized_projection(
    tmp_path: Path, snapshot_set, actor
) -> None:
    binary = _tool(tmp_path, "printf 'SAT\\n'")
    chain_projection = _projection(snapshot_set, ChainId.ETHEREUM)
    manifest = ProjectionManifest(
        ChainId.ETHEREUM,
        "ethereum.bridge",
        "0x0000000000000000000000000000000000000011",
        StateManifest(
            slots=(
                MaterializedSlot(
                    "ethereum.bridge",
                    "0x0000000000000000000000000000000000000011",
                    "0x0",
                    "0x1",
                ),
            )
        ),
        100,
        1_000,
    )
    witness = WitnessProjection(
        chain_projection,
        (
            Call(
                "submit()",
                chain=ChainId.ETHEREUM,
                context_id="ethereum.bridge",
                calldata="0x1234",
                actor=actor,
            ),
        ),
        "locked == minted",
        manifest,
    )
    result = HalmosAdapter(binary=binary).confirm(
        "unused",
        witness,
        ChainId.ETHEREUM,
        harness_body="assert(false);",
        loop=3,
    )
    assert result.outcome is Outcome.COUNTEREXAMPLE
    assert result.value and result.value.reproduced
    assert result.value.omitted_state == ()
    assert result.artifacts[0].kind == "projection_manifest"
    assert result.bounds["loop"] == 3


def test_slither_validates_proxy_identity_before_using_static_hints(
    tmp_path: Path, snapshot_set
) -> None:
    target = tmp_path / "Target.sol"
    target.write_text("contract Target {}")
    binary = _tool(
        tmp_path,
        "printf '%s' "
        '\'{"results":{"detectors":[{"check":"reentrancy-eth",'
        '"impact":"High","elements":[{"name":"submit(bytes)"}]}]}}\'',
    )
    projection = _projection(snapshot_set, ChainId.ETHEREUM)
    mismatch = SlitherAdapter(binary=binary).probe(
        str(target),
        "invariant",
        (),
        projection,
        ChainId.ETHEREUM,
        context_id="ethereum.bridge",
        proxy_kind="uups",
    )
    assert mismatch.outcome is Outcome.UNSUPPORTED
    assert "proxy kind" in mismatch.diagnostics[0].message

    result = SlitherAdapter(binary=binary).probe(
        str(target),
        "invariant",
        (),
        projection,
        ChainId.ETHEREUM,
        context_id="ethereum.bridge",
    )
    assert result.outcome is Outcome.SUCCESS
    assert result.value and result.value[0].selector == "submit(bytes)"
    evidence = result.evidence[0]
    assert evidence.raw_hash == "sha256:" + hashlib.sha256(evidence.raw.encode()).hexdigest()


def test_all_adapter_operations_return_structured_results(snapshot_set) -> None:
    projection = _projection(snapshot_set, ChainId.POLYGON)
    result = HalmosAdapter(binary="unused").probe(
        "unused",
        "invariant",
        (),
        projection,
        ChainId.POLYGON,
    )
    assert isinstance(result, ToolRunResult)
    assert result.outcome is Outcome.UNSUPPORTED
    assert result.diagnostics

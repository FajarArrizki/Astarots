"""Deterministic adapter tests using fake tool executables."""

from pathlib import Path

from devil.adapter.echidna import EchidnaAdapter
from devil.adapter.halmos import HalmosAdapter, ProjectionManifest
from devil.adapter.slither import SlitherAdapter
from devil.core import ChainId, Outcome


def _tool(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tool"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_echidna_normalizes_candidate_trace(tmp_path: Path) -> None:
    body = (
        "printf '%s' "
        '\'{"candidates":[{"function":"submit(bytes)","calls":["submit(bytes)"]}]} \' '
    )
    binary = _tool(tmp_path, body)
    target = tmp_path / "target.sol"
    target.write_text("contract Target {}")
    adapter = EchidnaAdapter(binary=binary)
    assert adapter.probe(str(target), "inv", chain=ChainId.ETHEREUM) is Outcome.COUNTEREXAMPLE
    assert adapter.last_result is not None
    assert adapter.last_result.value[0].target_function == "submit(bytes)"
    assert adapter.last_result.artifacts[0].kind == "echidna-output"


def test_echidna_reports_missing_target(tmp_path: Path) -> None:
    adapter = EchidnaAdapter(binary="does-not-run")
    assert adapter.probe(str(tmp_path / "missing.sol"), "inv") is Outcome.TOOL_ERROR
    assert adapter.last_result is not None
    assert "target not found" in adapter.last_result.diagnostics[0].message


def test_halmos_requires_projection_and_normalizes_sat(tmp_path: Path) -> None:
    binary = _tool(tmp_path, "printf 'SAT\\n'")
    adapter = HalmosAdapter(binary=binary)
    assert adapter.probe("Test.t.sol", "inv") is Outcome.UNSUPPORTED
    projection = ProjectionManifest(ChainId.ETHEREUM, "0xTarget", ("0x01",), block_number=10)
    assert adapter.probe("Test.t.sol", "inv", projection=projection) is Outcome.COUNTEREXAMPLE
    assert adapter.last_result is not None
    assert adapter.last_result.value["projection"]["block_number"] == 10


def test_halmos_materializes_explicit_projection(tmp_path: Path) -> None:
    adapter = HalmosAdapter(binary="unused")
    projection = ProjectionManifest(
        ChainId.POLYGON,
        "0xTarget",
        ("0x01", "0x02"),
        omitted_state=("pending_messages",),
    )
    artifact = adapter.materialize_projection(projection, tmp_path)
    path = Path(artifact.path)
    assert path.exists()
    assert '"pending_messages"' in path.read_text()
    assert artifact.digest.startswith("sha256:")


def test_slither_normalizes_cross_chain_hint(tmp_path: Path) -> None:
    body = (
        "printf '%s' "
        '\'{"results":{"detectors":[{"check":"reentrancy-eth",'
        '"impact":"High","elements":[{"name":"receiveMessage(bytes)"}]}]}}\''
    )
    binary = _tool(tmp_path, body)
    target = tmp_path / "target.sol"
    target.write_text("contract Target {}")
    adapter = SlitherAdapter(slither_binary=binary)
    assert adapter.probe(str(target), "inv", chain=ChainId.POLYGON) is Outcome.SUCCESS
    assert adapter.last_result is not None
    assert adapter.last_result.value[0].selector == "receiveMessage(bytes)"
    assert adapter.last_result.value[0].kind == "EXTERNAL_CALL"
    assert adapter.last_result.artifacts[0].kind == "static_hint"


def test_slither_does_not_claim_execution(tmp_path: Path) -> None:
    target = tmp_path / "target.sol"
    target.write_text("contract Target {}")
    adapter = SlitherAdapter(slither_binary="unused")
    result = adapter.execute(str(target))
    assert result.outcome is Outcome.UNSUPPORTED
    assert not result.reachable

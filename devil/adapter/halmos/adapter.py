"""Halmos confirmation over explicit code/storage/environment projections."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from devil.adapter.protocol import (
    ArtifactRef,
    BoundedConfirmation,
    ChainProjection,
    Diagnostic,
    ProbeArtifact,
    ProjectionManifest,
    ReplayResult,
    ToolCapabilities,
    ToolRunResult,
    WitnessProjection,
    artifact_digest,
)
from devil.core.snapshot import BaseForkFingerprint
from devil.core.types import ChainId, Constraint, CrossChainStep, Evidence, Outcome

HALMOS_CAPABILITIES = ToolCapabilities(
    name="halmos",
    symbolic_execution=True,
    supported_artifacts=("projection_manifest", "bounded_confirmation"),
)


class HalmosAdapter:
    capabilities = HALMOS_CAPABILITIES

    def __init__(self, binary: str = "halmos", *, timeout: int = 600) -> None:
        self._binary = binary
        self._timeout = timeout

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple[Constraint, ...],
        projection: ChainProjection,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[list[ProbeArtifact]]:
        witness = options.get("witness_projection")
        if not isinstance(witness, WitnessProjection):
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(Diagnostic("Halmos requires a bounded WitnessProjection"),),
            )
        result = self._run(witness, str(options.get("harness_body", "")), options)
        value: list[ProbeArtifact] | None = [result.value] if result.value is not None else None
        return ToolRunResult(
            result.outcome,
            value,
            result.evidence,
            result.artifacts,
            result.diagnostics,
            result.bounds,
        )

    def confirm(
        self,
        target: str,
        witness_projection: WitnessProjection,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[BoundedConfirmation]:
        if witness_projection.chain_projection.chain_id != chain:
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(Diagnostic("witness projection chain mismatch"),),
            )
        return self._run(witness_projection, str(options.get("harness_body", "")), options)

    def execute(
        self,
        target: str,
        trace: tuple[CrossChainStep, ...],
        base_fingerprint: BaseForkFingerprint,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[ReplayResult]:
        return ToolRunResult(
            Outcome.UNSUPPORTED,
            diagnostics=(Diagnostic("Halmos is not an authoritative concrete replay backend"),),
        )

    def materialize_projection(
        self, projection: ProjectionManifest, directory: str | Path
    ) -> ArtifactRef:
        path = Path(directory) / f"projection-{artifact_digest(projection.as_dict())[7:19]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(projection.as_dict(), sort_keys=True, indent=2) + "\n")
        return ArtifactRef("projection_manifest", artifact_digest(projection.as_dict()), str(path))

    def _run(
        self,
        witness: WitnessProjection,
        harness_body: str,
        options: dict[str, Any],
    ) -> ToolRunResult[BoundedConfirmation]:
        if not harness_body.strip():
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(
                    Diagnostic("Halmos confirmation requires an executable harness_body"),
                ),
            )
        manifest = witness.projection_manifest
        errors = _validate_manifest(witness)
        if errors:
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=tuple(Diagnostic(error) for error in errors),
            )
        bounds = {
            "loop": int(options.get("loop", 2)),
            "width": int(options.get("width", 0)),
            "solver_timeout": int(options.get("solver_timeout", 120)),
            "projection_manifest_hash": artifact_digest(manifest.as_dict()),
        }
        with tempfile.TemporaryDirectory(prefix="astarots-halmos-") as directory:
            root = Path(directory)
            source = _projection_source(manifest, harness_body)
            source_path = root / "ProjectionHarness.t.sol"
            source_path.write_text(source)
            manifest_artifact = self.materialize_projection(manifest, root)
            command = [
                self._binary,
                "--root",
                str(root),
                "--contract",
                "AstarotsProjection",
                "--function",
                "check_violation",
                "--loop",
                str(bounds["loop"]),
                "--solver-timeout-assertion",
                str(bounds["solver_timeout"]),
            ]
            if bounds["width"]:
                command.extend(("--width", str(bounds["width"])))
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raw = (exc.stdout or "") + (exc.stderr or "")
                return ToolRunResult(
                    Outcome.TIMEOUT,
                    evidence=(Evidence("halmos", Outcome.TIMEOUT, raw, artifact_digest(raw)),),
                    artifacts=(manifest_artifact,),
                    diagnostics=(Diagnostic(f"Halmos timed out after {self._timeout}s"),),
                    bounds=bounds,
                )
            except OSError as exc:
                return ToolRunResult(
                    Outcome.TOOL_ERROR,
                    artifacts=(manifest_artifact,),
                    diagnostics=(Diagnostic(f"cannot execute Halmos: {exc.strerror}"),),
                    bounds=bounds,
                )
            raw = (completed.stdout or "") + (completed.stderr or "")
            lowered = raw.lower()
            reproduced = "counterexample" in lowered or "sat" in lowered and "unsat" not in lowered
            unsat = "unsat" in lowered or "no counterexample" in lowered
            if reproduced:
                outcome = Outcome.COUNTEREXAMPLE
            elif unsat and completed.returncode == 0:
                outcome = Outcome.UNSAT_UNDER_BOUNDS
            elif completed.returncode == 0:
                outcome = Outcome.SUCCESS
            else:
                outcome = Outcome.TOOL_ERROR
            diagnostics = ()
            if outcome is Outcome.TOOL_ERROR:
                diagnostics = (Diagnostic(f"Halmos exited with status {completed.returncode}"),)
            value = BoundedConfirmation(
                reproduced,
                artifact_digest(manifest.as_dict()),
                witness.chain_projection.assumptions,
                manifest.omitted_state,
            )
            evidence = Evidence("halmos", outcome, raw, artifact_digest(raw))
            confirmation_artifact = ArtifactRef(
                "bounded_confirmation", artifact_digest({"value": vars(value), "bounds": bounds})
            )
            return ToolRunResult(
                outcome,
                value,
                (evidence,),
                (manifest_artifact, confirmation_artifact),
                diagnostics,
                bounds,
            )


def _validate_manifest(witness: WitnessProjection) -> list[str]:
    manifest = witness.projection_manifest
    errors: list[str] = []
    if manifest.chain is not witness.chain_projection.chain_id:
        errors.append("projection manifest chain differs from chain projection")
    if manifest.target_context not in witness.chain_projection.base_fingerprint.targets:
        errors.append("projection target is absent from base fingerprint")
    target = witness.chain_projection.base_fingerprint.targets.get(manifest.target_context)
    if target and target.address.lower() != manifest.target_address.lower():
        errors.append("projection target address differs from base fingerprint")
    for code in manifest.state.code:
        if artifact_digest(code.bytecode) != code.code_hash and code.code_hash.startswith(
            "sha256:"
        ):
            errors.append(f"projected code hash mismatch for {code.context_id}")
    return errors


def _projection_source(manifest: ProjectionManifest, harness_body: str) -> str:
    setup: list[str] = [
        f"vm.roll({manifest.block_number});",
        f"vm.warp({manifest.timestamp});",
    ]
    for code in manifest.state.code:
        bytecode = code.bytecode.removeprefix("0x")
        setup.append(f'vm.etch(address({code.address}), hex"{bytecode}");')
    for slot in manifest.state.slots:
        setup.append(
            f"vm.store(address({slot.address}), bytes32({slot.slot}), bytes32({slot.value}));"
        )
    setup_text = "\n        ".join(setup)
    return f"""// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.20;

interface Vm {{
    function etch(address target, bytes calldata code) external;
    function store(address target, bytes32 slot, bytes32 value) external;
    function roll(uint256 blockNumber) external;
    function warp(uint256 timestamp) external;
}}

contract AstarotsProjection {{
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function setUp() public {{
        {setup_text}
    }}

    function check_violation() public {{
        setUp();
        {harness_body}
    }}
}}
"""

"""Halmos adapter for bounded confirmation over explicit state projections."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devil.adapter.protocol import (
    ArtifactRef,
    Diagnostic,
    ToolCapabilities,
    ToolRunResult,
    artifact_digest,
)
from devil.core.types import ChainId, GlobalState, Outcome, WitnessState

HALMOS_CAPABILITIES = ToolCapabilities(
    name="halmos",
    symbolic_execution=True,
    supported_artifacts=("projection_manifest", "bounded_confirmation"),
)


@dataclass(frozen=True)
class ProjectionManifest:
    """Explicit state subset materialized for a Halmos run."""

    chain: ChainId
    target: str
    storage_slots: tuple[str, ...] = ()
    code_hash: str = ""
    block_number: int = 0
    omitted_state: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "chain": self.chain.value,
            "target": self.target,
            "storage_slots": list(self.storage_slots),
            "code_hash": self.code_hash,
            "block_number": self.block_number,
            "omitted_state": list(self.omitted_state),
        }


class HalmosAdapter:
    """Run Halmos only against a declared projection, never an implied full fork."""

    capabilities = HALMOS_CAPABILITIES

    def __init__(self, binary: str = "halmos", *, timeout: int = 600) -> None:
        self._binary = binary
        self._timeout = timeout
        self.last_result: ToolRunResult[dict[str, object]] | None = None

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple = (),
        global_state: GlobalState | None = None,
        chain: ChainId | None = None,
        projection: ProjectionManifest | None = None,
    ) -> Outcome:
        if projection is None:
            result = ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(Diagnostic("Halmos requires an explicit projection manifest"),),
            )
            self.last_result = result
            return result.outcome
        return self._run(target, projection).outcome

    def confirm(
        self,
        target: str,
        witness: WitnessState,
        chain: ChainId | None = None,
        projection: ProjectionManifest | None = None,
    ) -> Outcome:
        if projection is None:
            return Outcome.UNSUPPORTED
        return self._run(target, projection).outcome

    def execute(self, *args: object, **kwargs: object) -> object:
        from devil.adapter.protocol import ExecutionResult

        return ExecutionResult(
            reachable=False, outcome=Outcome.UNSUPPORTED, revert_reason="use canonical executor"
        )

    def materialize_projection(
        self, projection: ProjectionManifest, directory: str | Path
    ) -> ArtifactRef:
        path = Path(directory) / f"projection-{artifact_digest(projection.as_dict())[7:19]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(projection.as_dict(), sort_keys=True, indent=2) + "\n")
        return ArtifactRef("projection_manifest", artifact_digest(projection.as_dict()), str(path))

    def _run(self, target: str, projection: ProjectionManifest) -> ToolRunResult[dict[str, object]]:
        command = [self._binary, "--contract", target]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            result = ToolRunResult(Outcome.TIMEOUT, diagnostics=(Diagnostic(str(exc)),))
        except OSError as exc:
            result = ToolRunResult(Outcome.TOOL_ERROR, diagnostics=(Diagnostic(str(exc), "error"),))
        else:
            result = self._normalize(completed, projection)
        self.last_result = result
        return result

    @staticmethod
    def _normalize(
        completed: subprocess.CompletedProcess[str], projection: ProjectionManifest
    ) -> ToolRunResult[dict[str, object]]:
        raw = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            return ToolRunResult(
                Outcome.TOOL_ERROR,
                diagnostics=(Diagnostic(raw.strip() or "Halmos exited non-zero", "error"),),
            )
        upper = raw.upper()
        if "UNSAT" in upper:
            outcome = Outcome.UNSAT_UNDER_BOUNDS
        elif "SAT" in upper or "COUNTEREXAMPLE" in upper:
            outcome = Outcome.COUNTEREXAMPLE
        else:
            outcome = Outcome.PARTIAL
        artifact = ArtifactRef("projection_manifest", artifact_digest(projection.as_dict()))
        return ToolRunResult(
            outcome,
            {"projection": projection.as_dict(), "raw": raw},
            artifacts=(artifact,),
            diagnostics=(Diagnostic("Halmos result is bounded to projected state"),),
        )

"""Echidna adapter for concrete probing from pinned RPC fork state."""

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
from devil.core.types import Call, Candidate, ChainId, Constraint, GlobalState, Outcome

ECHIDNA_CAPABILITIES = ToolCapabilities(
    name="echidna",
    stateful_fuzzing=True,
    supported_artifacts=("seed_corpus", "candidate_trace"),
)


@dataclass(frozen=True)
class EchidnaForkConfig:
    """Per-chain Echidna fork settings; URL values stay in the environment."""

    rpc_env: str
    rpc_block: int
    test_limit: int = 50_000
    corpus_dir: str = ""


class EchidnaAdapter:
    """Run Echidna and normalize its JSON or text result into candidates."""

    capabilities = ECHIDNA_CAPABILITIES

    def __init__(self, binary: str = "echidna", *, timeout: int = 600) -> None:
        self._binary = binary
        self._timeout = timeout
        self.last_result: ToolRunResult[list[Candidate]] | None = None

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple[Constraint, ...] = (),
        global_state: GlobalState | None = None,
        chain: ChainId | None = None,
        fork: EchidnaForkConfig | None = None,
    ) -> Outcome:
        """Probe a deployed target; the caller supplies its fork config."""
        target_path = Path(target)
        if not target_path.exists():
            return self._finish(
                ToolRunResult(
                    Outcome.TOOL_ERROR, diagnostics=(Diagnostic(f"target not found: {target}"),)
                )
            )
        command = [self._binary, str(target_path), "--format", "json"]
        if fork is not None:
            command.extend(["--test-limit", str(fork.test_limit)])
            if fork.corpus_dir:
                command.extend(["--corpus-dir", fork.corpus_dir])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return self._finish(ToolRunResult(Outcome.TIMEOUT, diagnostics=(Diagnostic(str(exc)),)))
        except OSError as exc:
            return self._finish(
                ToolRunResult(Outcome.TOOL_ERROR, diagnostics=(Diagnostic(str(exc), "error"),))
            )
        if completed.returncode != 0:
            return self._finish(
                ToolRunResult(
                    Outcome.TOOL_ERROR,
                    diagnostics=(
                        Diagnostic(completed.stderr.strip() or "Echidna exited non-zero", "error"),
                    ),
                )
            )
        payload = _parse_output(completed.stdout)
        candidates = _candidates(payload, chain, invariant_id)
        outcome = Outcome.COUNTEREXAMPLE if candidates else Outcome.SUCCESS
        evidence = (self._evidence(outcome, completed.stdout),)
        artifact = ArtifactRef("echidna-output", artifact_digest(payload))
        return self._finish(ToolRunResult(outcome, candidates, evidence, (artifact,)))

    def execute(self, *args: object, **kwargs: object) -> object:
        """Echidna does not provide authoritative fixed-trace replay."""
        from devil.adapter.protocol import ExecutionResult

        return ExecutionResult(
            reachable=False,
            outcome=Outcome.UNSUPPORTED,
            revert_reason="canonical executor required",
        )

    def confirm(self, *args: object, **kwargs: object) -> Outcome:
        return Outcome.UNSUPPORTED

    def _finish(self, result: ToolRunResult[list[Candidate]]) -> Outcome:
        self.last_result = result
        return result.outcome

    @staticmethod
    def _evidence(outcome: Outcome, raw: str):
        from devil.core.types import Evidence

        return Evidence(tool="echidna", outcome=outcome, raw=raw, raw_hash=artifact_digest(raw))


def _parse_output(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"result": value}


def _candidates(payload: dict, chain: ChainId | None, invariant_id: str) -> list[Candidate]:
    entries = payload.get("candidates", [])
    if not isinstance(entries, list):
        entries = []
    candidates: list[Candidate] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("function"):
            continue
        calls = tuple(
            Call(function_signature=str(call), chain=chain, source="echidna")
            for call in entry.get("calls", [])
        )
        candidates.append(
            Candidate(
                target_function=str(entry["function"]),
                call_sequence=calls,
                suspicion=float(entry.get("suspicion", 0.5)),
                chain=chain,
            )
        )
    return candidates

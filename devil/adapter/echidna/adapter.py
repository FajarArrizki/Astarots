"""Echidna candidate generation against a declared RPC fork base."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devil.adapter.protocol import (
    ArtifactRef,
    ChainProjection,
    Diagnostic,
    ProbeArtifact,
    ReplayResult,
    ToolCapabilities,
    ToolRunResult,
    WitnessProjection,
    artifact_digest,
)
from devil.core.snapshot import BaseForkFingerprint
from devil.core.types import (
    Actor,
    Call,
    Candidate,
    ChainId,
    Constraint,
    CrossChainStep,
    Evidence,
    Outcome,
)

ECHIDNA_CAPABILITIES = ToolCapabilities(
    name="echidna",
    stateful_fuzzing=True,
    shrinking=True,
    supported_artifacts=("seed_corpus", "candidate_trace", "fork_cache"),
)


@dataclass(frozen=True)
class EchidnaForkConfig:
    rpc_env: str
    rpc_block: int
    test_limit: int = 50_000
    corpus_dir: str = ""
    rpc_url: str = ""


class EchidnaAdapter:
    capabilities = ECHIDNA_CAPABILITIES

    def __init__(self, binary: str = "echidna", *, timeout: int = 600) -> None:
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
        if projection.chain_id is not chain:
            return _unsupported("projection chain does not match requested chain")
        fork = options.get("fork_config")
        if not isinstance(fork, EchidnaForkConfig):
            return _unsupported("Echidna fork_config is required")
        expected_block = projection.base_fingerprint.block_number + projection.block_number_delta
        if fork.rpc_block != expected_block:
            return _unsupported("Echidna rpc_block does not match the projected state")
        rpc_url = fork.rpc_url or os.environ.get(fork.rpc_env, "")
        if not rpc_url:
            return ToolRunResult(
                Outcome.TOOL_ERROR,
                diagnostics=(Diagnostic(f"RPC environment variable {fork.rpc_env!r} is not set"),),
            )
        contract_name = str(options.get("contract", ""))
        target_address = str(options.get("target_address", ""))
        if not target_address:
            fingerprints = projection.base_fingerprint.targets
            if len(fingerprints) == 1:
                target_address = next(iter(fingerprints.values())).address
        if not target_address:
            return _unsupported("target_address is required for Echidna state-network forking")
        config = {
            "testMode": "property",
            "testLimit": fork.test_limit,
            "rpcUrl": rpc_url,
            "rpcBlock": fork.rpc_block,
            "contractAddr": target_address,
            "corpusDir": fork.corpus_dir or None,
        }
        command_result = self._run(target, contract_name, config)
        if isinstance(command_result, ToolRunResult):
            return command_result
        completed, redacted_config = command_result
        raw = _redact((completed.stdout or "") + (completed.stderr or ""), rpc_url)
        payload = _parse_output(completed.stdout or "")
        actor = options.get("actor")
        if not isinstance(actor, Actor):
            return _unsupported("Echidna candidate generation requires an explicit actor")
        candidates = _candidates(payload, chain, invariant_id, actor)
        outcome = (
            Outcome.COUNTEREXAMPLE
            if candidates or completed.returncode == 1
            else Outcome.SUCCESS
            if completed.returncode == 0
            else Outcome.TOOL_ERROR
        )
        diagnostics = ()
        if outcome is Outcome.TOOL_ERROR:
            diagnostics = (Diagnostic(f"Echidna exited with status {completed.returncode}"),)
        evidence = Evidence("echidna", outcome, raw, artifact_digest(raw))
        artifact_payload = {
            "invariant": invariant_id,
            "chain": chain.value,
            "base": projection.base_fingerprint.digest,
            "config": redacted_config,
            "constraints": [vars(item) for item in constraints],
            "candidates": payload.get("candidates", []),
        }
        artifact = ArtifactRef("candidate_trace", artifact_digest(artifact_payload))
        return ToolRunResult(
            outcome,
            list(candidates),
            (evidence,),
            (artifact,),
            diagnostics,
            bounds={"test_limit": fork.test_limit, "rpc_block": fork.rpc_block},
        )

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
            diagnostics=(Diagnostic("Echidna fixed-trace replay is not configured"),),
        )

    def confirm(
        self,
        target: str,
        witness_projection: WitnessProjection,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[Any]:
        return ToolRunResult(
            Outcome.UNSUPPORTED,
            diagnostics=(Diagnostic("Echidna cannot independently confirm its own traces"),),
        )

    def _run(
        self, target: str, contract_name: str, config: dict[str, Any]
    ) -> (
        tuple[subprocess.CompletedProcess[str], dict[str, Any]] | ToolRunResult[list[ProbeArtifact]]
    ):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        path = Path(handle.name)
        try:
            os.chmod(path, 0o600)
            for key, value in config.items():
                if value is None:
                    continue
                rendered = json.dumps(value) if isinstance(value, str) else str(value).lower()
                handle.write(f"{key}: {rendered}\n")
            handle.close()
            command = [self._binary, target, "--config", str(path), "--format", "json"]
            if contract_name:
                command.extend(("--contract", contract_name))
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raw = _redact((exc.stdout or "") + (exc.stderr or ""), str(config["rpcUrl"]))
                return ToolRunResult(
                    Outcome.TIMEOUT,
                    evidence=(Evidence("echidna", Outcome.TIMEOUT, raw, artifact_digest(raw)),),
                    diagnostics=(Diagnostic(f"Echidna timed out after {self._timeout}s"),),
                    bounds={"timeout": self._timeout},
                )
            except OSError as exc:
                return ToolRunResult(
                    Outcome.TOOL_ERROR,
                    diagnostics=(Diagnostic(f"cannot execute Echidna: {exc.strerror}"),),
                )
            redacted = dict(config)
            rpc_env_name = next(
                (key for key, value in os.environ.items() if value == config["rpcUrl"]),
                "REDACTED",
            )
            redacted["rpcUrl"] = f"env:{rpc_env_name}"
            return completed, redacted
        finally:
            handle.close()
            path.unlink(missing_ok=True)


def _parse_output(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"result": value}


def _candidates(
    payload: dict[str, Any],
    chain: ChainId,
    invariant_id: str,
    actor: Actor,
) -> tuple[Candidate, ...]:
    result: list[Candidate] = []
    for entry in payload.get("candidates", []):
        if not isinstance(entry, dict):
            continue
        calls = tuple(
            Call(
                str(call.get("function", "")),
                tuple(call.get("args", ())),
                chain,
                str(call.get("context_id", "")),
                str(call.get("calldata", "")),
                int(call.get("value", 0)),
                actor,
                source="echidna",
            )
            for call in entry.get("calls", [])
            if isinstance(call, dict) and call.get("function") and call.get("calldata")
        )
        if not calls:
            continue
        evidence_raw = json.dumps(entry, sort_keys=True)
        result.append(
            Candidate(
                target_function=calls[-1].function_signature,
                call_sequence=calls,
                suspicion=float(entry.get("suspicion", 0.8)),
                evidence=Evidence(
                    "echidna", Outcome.COUNTEREXAMPLE, evidence_raw, artifact_digest(entry)
                ),
                chain=chain,
            )
        )
    return tuple(result)


def _unsupported(message: str) -> ToolRunResult[list[ProbeArtifact]]:
    return ToolRunResult(Outcome.UNSUPPORTED, diagnostics=(Diagnostic(message),))


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "<redacted>") if secret else value

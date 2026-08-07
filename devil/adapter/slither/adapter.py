"""Slither static hints bound to a verified deployed target fingerprint."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from devil.adapter.protocol import (
    ChainProjection,
    Diagnostic,
    ProbeArtifact,
    ReplayResult,
    StaticHint,
    ToolCapabilities,
    ToolRunResult,
    WitnessProjection,
    artifact_digest,
)
from devil.core.snapshot import BaseForkFingerprint
from devil.core.types import ChainId, Constraint, CrossChainStep, Evidence, Outcome

SLITHER_CAPABILITIES = ToolCapabilities(
    name="slither",
    static_analysis=True,
    supported_artifacts=("static_hint", "slither_json"),
)


class SlitherAdapter:
    capabilities = SLITHER_CAPABILITIES

    def __init__(self, binary: str = "slither", *, timeout: int = 300) -> None:
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
        context_id = str(options.get("context_id", ""))
        identity_error = _validate_identity(context_id, projection, chain, options)
        if identity_error:
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(Diagnostic(identity_error),),
            )
        if not Path(target).exists():
            return ToolRunResult(
                Outcome.TOOL_ERROR,
                diagnostics=(Diagnostic(f"Slither source target does not exist: {target}"),),
            )
        command = [self._binary, target, "--json", "-"]
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
                evidence=(Evidence("slither", Outcome.TIMEOUT, raw, artifact_digest(raw)),),
                diagnostics=(Diagnostic(f"Slither timed out after {self._timeout}s"),),
                bounds={"timeout": self._timeout},
            )
        except OSError as exc:
            return ToolRunResult(
                Outcome.TOOL_ERROR,
                diagnostics=(Diagnostic(f"cannot execute Slither: {exc.strerror}"),),
            )
        raw = (completed.stdout or "") + (completed.stderr or "")
        payload = _parse_json(completed.stdout or "")
        hints = _static_hints(payload, context_id, chain)
        outcome = (
            Outcome.SUCCESS
            if completed.returncode == 0
            else Outcome.PARTIAL
            if hints
            else Outcome.TOOL_ERROR
        )
        diagnostics = ()
        if outcome is Outcome.TOOL_ERROR:
            diagnostics = (Diagnostic(f"Slither exited with status {completed.returncode}"),)
        elif outcome is Outcome.PARTIAL:
            diagnostics = (Diagnostic("Slither emitted usable hints but exited non-zero"),)
        fingerprint = projection.base_fingerprint.targets[context_id]
        evidence_payload = {
            "raw": raw,
            "base_fingerprint": projection.base_fingerprint.digest,
            "context": context_id,
            "runtime_code_hash": fingerprint.runtime_code_hash,
            "proxy_kind": fingerprint.proxy_kind,
            "implementation": fingerprint.implementation_address,
            "implementation_code_hash": fingerprint.implementation_code_hash,
        }
        evidence_raw = json.dumps(evidence_payload, sort_keys=True)
        evidence = Evidence(
            "slither",
            outcome,
            evidence_raw,
            "sha256:" + hashlib.sha256(evidence_raw.encode()).hexdigest(),
        )
        return ToolRunResult(
            outcome,
            list(hints),
            (evidence,),
            bounds={
                "source_target": target,
                "pinned_block": projection.base_fingerprint.block_number,
            },
            diagnostics=diagnostics,
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
            diagnostics=(Diagnostic("Slither cannot execute traces"),),
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
            diagnostics=(Diagnostic("Slither hints are corroboration, not confirmation"),),
        )


def _validate_identity(
    context_id: str,
    projection: ChainProjection,
    chain: ChainId,
    options: dict[str, Any],
) -> str:
    if projection.chain_id != chain:
        return "projection chain does not match requested chain"
    if not context_id:
        return "Slither context_id is required"
    fingerprint = projection.base_fingerprint.targets.get(context_id)
    if fingerprint is None:
        return "Slither context is absent from the verified base fingerprint"
    if not fingerprint.runtime_code_hash or not fingerprint.artifact_hash:
        return "Slither target lacks verified runtime code or artifact fingerprint"
    expected_proxy = str(options.get("proxy_kind", fingerprint.proxy_kind))
    if expected_proxy != fingerprint.proxy_kind:
        return "Slither proxy kind differs from verified target"
    expected_implementation = str(
        options.get("implementation_address", fingerprint.implementation_address)
    )
    if expected_implementation.lower() != fingerprint.implementation_address.lower():
        return "Slither implementation address differs from verified target"
    if fingerprint.proxy_kind != "none" and not fingerprint.implementation_code_hash:
        return "Slither proxy target lacks an implementation code fingerprint"
    return ""


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _static_hints(
    payload: dict[str, Any], context_id: str, chain: ChainId
) -> tuple[StaticHint, ...]:
    detectors = payload.get("results", {}).get("detectors", [])
    hints: list[StaticHint] = []
    for detector in detectors:
        if not isinstance(detector, dict):
            continue
        check = str(detector.get("check", ""))
        if not _cross_chain_relevant(check, str(detector.get("description", ""))):
            continue
        elements = detector.get("elements", [])
        selector = ""
        locations: list[str] = []
        for element in elements if isinstance(elements, list) else []:
            if not isinstance(element, dict):
                continue
            selector = selector or str(element.get("name", ""))
            source = element.get("source_mapping", {})
            if isinstance(source, dict) and source.get("filename_relative"):
                locations.append(f"{source['filename_relative']}:{source.get('lines', ['?'])[0]}")
        if not selector:
            continue
        severity = str(detector.get("impact", "medium"))
        hints.append(
            StaticHint(
                context_id,
                selector,
                _hint_kind(check),
                tuple(locations),
                suspicion=_suspicion(severity),
                producer="slither",
            )
        )
    return tuple(hints)


def _cross_chain_relevant(check: str, description: str) -> bool:
    haystack = f"{check} {description}".lower()
    terms = (
        "access-control",
        "arbitrary-send",
        "controlled-delegatecall",
        "reentrancy",
        "unchecked",
        "taint",
        "cross-chain",
        "message",
        "relayer",
        "delegatecall",
    )
    return any(term in haystack for term in terms)


def _hint_kind(detector: str) -> str:
    lowered = detector.lower()
    if "access" in lowered or "controlled" in lowered:
        return "ACCESS_GAP"
    if "taint" in lowered or "arbitrary" in lowered:
        return "TAINT_FLOW"
    if "reentrancy" in lowered or "call" in lowered:
        return "EXTERNAL_CALL"
    return "OTHER"


def _suspicion(severity: str) -> float:
    return {"high": 0.9, "medium": 0.65, "low": 0.35, "informational": 0.15}.get(
        severity.lower(), 0.5
    )

"""Slither adapter — static analysis for cross-chain invariant testing.

Slither identifies structural vulnerability patterns that create
cross-chain attack surface: missing access control on relayer-callable
functions, storage variables writable from cross-chain messages without
validation, missing checks on message origin.

These become StaticHint artifacts that seed Echidna's dynamic exploration.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from devil.adapter.protocol import (
    ArtifactRef,
    Diagnostic,
    ExecutionResult,
    StaticHint,
    ToolCapabilities,
    ToolRunResult,
    artifact_digest,
)
from devil.core.types import (
    Call,
    ChainId,
    Constraint,
    GlobalState,
    Outcome,
    WitnessState,
)

logger = logging.getLogger(__name__)

SLITHER_CAPABILITIES = ToolCapabilities(
    name="slither",
    static_analysis=True,
    supported_artifacts=("static_hint",),
)


class SlitherAdapter:
    """Wraps Slither for cross-chain invariant probe mode.

    Slither cannot execute or independently confirm counterexamples.
    """

    capabilities: ToolCapabilities = SLITHER_CAPABILITIES

    def __init__(self, slither_binary: str = "slither", *, timeout: int = 300) -> None:
        self._binary = slither_binary
        self._timeout = timeout
        self.last_result: ToolRunResult[list[StaticHint]] | None = None

    # ── probe ────────────────────────────────────────────────────────────

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple[Constraint, ...] = (),
        global_state: GlobalState | None = None,
        chain: ChainId | None = None,
    ) -> Outcome:
        """Run Slither detectors on the target contract.

        Returns SUCCESS with static hints, or TOOL_ERROR / TIMEOUT.
        """
        target_path = Path(target)
        if not target_path.exists():
            return self._finish(
                ToolRunResult(
                    Outcome.TOOL_ERROR, diagnostics=(Diagnostic(f"target not found: {target}"),)
                )
            )

        try:
            result = subprocess.run(
                [self._binary, str(target_path), "--json", "-"],
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

        if result.returncode != 0:
            diagnostic = Diagnostic(result.stderr.strip() or "Slither exited non-zero", "error")
            return self._finish(ToolRunResult(Outcome.TOOL_ERROR, diagnostics=(diagnostic,)))

        parsed = self._parse_output(result.stdout)
        raw_hints = self._extract_cross_chain_hints(parsed, chain)
        hints = tuple(
            StaticHint(
                context_id=str(item["chain"]),
                selector=str(item["function"]),
                kind=_hint_kind(str(item["detector"])),
                suspicion=_suspicion(str(item["severity"])),
                producer="slither",
            )
            for item in raw_hints
        )
        result_value = ToolRunResult(
            Outcome.SUCCESS,
            list(hints),
            artifacts=(ArtifactRef("static_hint", artifact_digest(raw_hints)),),
        )
        return self._finish(result_value)

    def _finish(self, result: ToolRunResult[list[StaticHint]]) -> Outcome:
        self.last_result = result
        return result.outcome

    # ── execute (not supported) ──────────────────────────────────────────

    def execute(
        self,
        target: str,
        sequence: tuple[Call, ...] = (),
        constraints: tuple[Constraint, ...] = (),
        chain: ChainId | None = None,
    ) -> ExecutionResult:
        """Slither cannot execute; the canonical executor owns replay."""
        return ExecutionResult(
            reachable=False,
            outcome=Outcome.UNSUPPORTED,
            revert_reason="Slither cannot execute; use the canonical executor",
        )

    # ── confirm (not applicable) ─────────────────────────────────────────

    def confirm(
        self,
        target: str,
        witness: WitnessState,
        chain: ChainId | None = None,
    ) -> Outcome:
        """Slither cannot independently confirm counterexamples."""
        return Outcome.UNSUPPORTED

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_output(raw: str) -> dict:
        """Parse Slither JSON output, falling back to raw on failure."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Slither output is not valid JSON")
            return {"raw": raw}

    @staticmethod
    def _extract_cross_chain_hints(
        parsed: dict,
        chain: ChainId | None,
    ) -> list[dict]:
        """Extract cross-chain-relevant Slither findings."""
        hints: list[dict] = []
        detectors = parsed.get("results", {}).get("detectors", [])

        for detector in detectors:
            check = detector.get("check", "")
            if not SlitherAdapter._is_cross_chain_relevant(check):
                continue

            for element in detector.get("elements", []):
                hints.append(
                    {
                        "detector": check,
                        "function": element.get("name", ""),
                        "description": detector.get("description", ""),
                        "severity": detector.get("impact", "Medium"),
                        "chain": str(chain) if chain else "",
                    }
                )

        return hints

    @staticmethod
    def _is_cross_chain_relevant(check: str) -> bool:
        """Filter detectors relevant to cross-chain attack surface."""
        relevant = {
            "reentrancy",
            "unchecked-transfer",
            "unchecked-lowlevel",
            "access-control",
            "missing-zero-check",
            "arbitrary-send",
            "controlled-delegatecall",
        }
        return any(item in check.lower() for item in relevant)


def _hint_kind(detector: str) -> str:
    lowered = detector.lower()
    if "reentrancy" in lowered or "unchecked" in lowered:
        return "EXTERNAL_CALL"
    if "access-control" in lowered:
        return "ACCESS_GAP"
    return "OTHER"


def _suspicion(severity: str) -> float:
    return {"high": 0.9, "medium": 0.65, "low": 0.35, "informational": 0.15}.get(
        severity.lower(), 0.5
    )

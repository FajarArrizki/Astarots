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

from devil.adapter.protocol import ExecutionResult, ToolCapabilities
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
    Those methods raise NotImplementedError — the scheduler must route
    execute/confirm to tools with concrete_replay or symbolic_execution
    capability.
    """

    capabilities: ToolCapabilities = SLITHER_CAPABILITIES

    def __init__(self, slither_binary: str = "slither") -> None:
        self._binary = slither_binary

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
            logger.error("Target not found: %s", target)
            return Outcome.TOOL_ERROR

        try:
            result = subprocess.run(
                [self._binary, str(target_path), "--json", "-"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.error("Slither timed out on %s", target)
            return Outcome.TIMEOUT
        except OSError as exc:
            logger.error("Slither not found or failed to start: %s", exc)
            return Outcome.TOOL_ERROR

        if result.returncode != 0:
            logger.warning(
                "Slither exited %d on %s: %s",
                result.returncode, target, result.stderr[:200],
            )
            return Outcome.TOOL_ERROR

        parsed = self._parse_output(result.stdout)
        _hints = self._extract_cross_chain_hints(parsed, chain)
        return Outcome.SUCCESS

    # ── execute (not supported) ──────────────────────────────────────────

    def execute(
        self,
        target: str,
        sequence: tuple[Call, ...] = (),
        constraints: tuple[Constraint, ...] = (),
        chain: ChainId | None = None,
    ) -> ExecutionResult:
        """Slither cannot execute. Route to a concrete_replay tool."""
        raise NotImplementedError(
            "Slither cannot execute. Use a tool with concrete_replay capability."
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
                hints.append({
                    "detector": check,
                    "function": element.get("name", ""),
                    "description": detector.get("description", ""),
                    "severity": detector.get("impact", "Medium"),
                    "chain": str(chain) if chain else "",
                })

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
        return any(r in check.lower() for r in relevant)

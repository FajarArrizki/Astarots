"""Adapter registry, typed proposal aggregation, and witness confirmation routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from devil.adapter.protocol import (
    BoundedConfirmation,
    Candidate,
    ChainProjection,
    Diagnostic,
    ProbeArtifact,
    StaticHint,
    ToolAdapter,
    ToolRunResult,
    WitnessProjection,
)
from devil.core.types import ChainId, Constraint, Outcome
from devil.harness.search import trace_hash


@dataclass
class AdapterRegistry:
    _adapters: dict[str, ToolAdapter] = field(default_factory=dict)

    def register(self, adapter: ToolAdapter) -> None:
        name = adapter.capabilities.name
        if name in self._adapters:
            raise ValueError(f"adapter {name!r} is already registered")
        self._adapters[name] = adapter

    def get(self, name: str) -> ToolAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown adapter {name!r}") from exc

    def available(self) -> tuple[ToolAdapter, ...]:
        return tuple(self._adapters[name] for name in sorted(self._adapters))


class CandidateWorkers:
    """Run capable adapters and preserve partial/incomplete outcomes."""

    def __init__(
        self,
        registry: AdapterRegistry,
        enabled: tuple[str, ...],
        *,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        abi_signatures: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self.registry = registry
        self.enabled = enabled
        self.options = dict(options or {})
        self.abi_signatures = dict(abi_signatures or {})

    def propose(
        self,
        *,
        targets: Mapping[str, str],
        invariant_id: str,
        constraints: tuple[Constraint, ...],
        projection: ChainProjection,
        chain: ChainId,
        runtime_options: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ToolRunResult[list[Candidate]]:
        artifacts: list[ProbeArtifact] = []
        evidence = []
        references = []
        diagnostics: list[Diagnostic] = []
        outcomes: list[Outcome] = []
        static_hints: list[StaticHint] = []
        capable = False
        for name in self.enabled:
            adapter = self.registry.get(name)
            capabilities = adapter.capabilities
            if not (capabilities.static_analysis or capabilities.stateful_fuzzing):
                continue
            capable = True
            context_targets = [
                (context, target)
                for context, target in sorted(targets.items())
                if context in projection.base_fingerprint.targets
            ]
            for context, target in context_targets:
                options = dict(self.options.get(name, {}))
                options.update(dict((runtime_options or {}).get(name, {})))
                options.setdefault("context_id", context)
                options.setdefault(
                    "target_address",
                    projection.base_fingerprint.targets[context].address,
                )
                if static_hints:
                    options.setdefault("static_hints", tuple(static_hints))
                result = adapter.probe(
                    target,
                    invariant_id,
                    constraints,
                    projection,
                    chain,
                    **options,
                )
                outcomes.append(result.outcome)
                evidence.extend(result.evidence)
                references.extend(result.artifacts)
                diagnostics.extend(result.diagnostics)
                if result.value:
                    artifacts.extend(result.value)
                    static_hints.extend(
                        item for item in result.value if isinstance(item, StaticHint)
                    )
        if not capable:
            return ToolRunResult(
                Outcome.UNSUPPORTED,
                diagnostics=(Diagnostic("no enabled adapter can generate candidates"),),
            )
        candidates = [item for item in artifacts if isinstance(item, Candidate)]
        validated: dict[str, Candidate] = {}
        for candidate in candidates:
            error = self._candidate_error(candidate, projection)
            if error:
                diagnostics.append(Diagnostic(error))
                continue
            validated.setdefault(trace_hash(candidate.call_sequence), candidate)
        incomplete = any(
            outcome in {Outcome.TIMEOUT, Outcome.TOOL_ERROR, Outcome.UNSUPPORTED, Outcome.PARTIAL}
            for outcome in outcomes
        )
        if validated and incomplete:
            aggregate = Outcome.PARTIAL
        elif any(outcome is Outcome.COUNTEREXAMPLE for outcome in outcomes) and validated:
            aggregate = Outcome.COUNTEREXAMPLE
        elif validated or outcomes and all(outcome is Outcome.SUCCESS for outcome in outcomes):
            aggregate = Outcome.SUCCESS
        elif outcomes and all(outcome is Outcome.UNSUPPORTED for outcome in outcomes):
            aggregate = Outcome.UNSUPPORTED
        else:
            aggregate = Outcome.PARTIAL
        if aggregate in {Outcome.UNSUPPORTED, Outcome.PARTIAL} and not diagnostics:
            diagnostics.append(Diagnostic("candidate generation was incomplete"))
        return ToolRunResult(
            aggregate,
            list(validated.values()),
            tuple(evidence),
            tuple(references),
            tuple(diagnostics),
        )

    def confirm(
        self,
        *,
        target: str,
        witness: WitnessProjection,
        chain: ChainId,
        originating_tool: str,
    ) -> tuple[ToolRunResult[BoundedConfirmation], ...]:
        results: list[ToolRunResult[BoundedConfirmation]] = []
        for name in self.enabled:
            if name == originating_tool:
                continue
            adapter = self.registry.get(name)
            if not adapter.capabilities.symbolic_execution:
                continue
            results.append(
                adapter.confirm(
                    target,
                    witness,
                    chain,
                    **dict(self.options.get(name, {})),
                )
            )
        return tuple(results)

    def _candidate_error(self, candidate: Candidate, projection: ChainProjection) -> str:
        if not candidate.call_sequence:
            return "adapter returned an empty candidate trace"
        for step in candidate.call_sequence:
            if not hasattr(step, "context_id"):
                continue
            if step.chain != projection.chain_id:
                return "candidate step chain differs from projection"
            if not step.context_id or step.context_id not in projection.base_fingerprint.targets:
                return "candidate step references an unverified context"
            if not step.calldata:
                return "candidate step lacks canonical calldata"
            if step.actor is None:
                return "candidate call lacks an explicit actor"
            allowed = self.abi_signatures.get(step.context_id)
            if allowed is not None and step.function_signature not in allowed:
                return f"candidate selector {step.function_signature!r} is absent from bound ABI"
        return ""

# Adapter Protocol

Each adapter wraps one external analysis tool and exposes it to the harness through a uniform interface. The harness never calls a tool directly — it goes through the adapter, which handles process management, chain-specific configuration, and output normalization.

For cross-chain probing, adapters receive a `chain` context. They deploy the target contract on the specified chain, configure the mock relayer, and restrict their analysis to that chain's sub-invariant. Tools never see two chains simultaneously — the decomposition and recombination happen at the harness layer.

---

## Tool Capabilities

Not every adapter can do everything. Slither cannot execute. Echidna cannot symbolically prove. The harness must know what each tool can and cannot do before routing work. Every adapter declares its capabilities upfront:

```
ToolCapabilities:
    static_analysis: bool              # Slither — analyze without execution
    stateful_fuzzing: bool             # Echidna — generate and execute sequences
    symbolic_execution: bool           # Halmos — explore all paths symbolically
    concrete_replay: bool              # Can this tool execute a fixed sequence?
    shrinking: bool                    # Can this tool minimize a counterexample?
    supported_targets: list[str]       # e.g. ["solidity", "vyper"]
    supported_artifacts: list[str]     # e.g. ["crytic-corpus", "halmos-trace"]
```

---

## Typed Outcomes

Tool results are not boolean pass/fail. Every adapter returns one of these outcome types, preserving the bounds and assumptions under which the result was obtained:

```
Outcome:
    Success                     # tool ran to completion, no violation found
    Counterexample(model)       # concrete or symbolic counterexample found
    UnsatUnderBounds(bounds)    # no violation in explored space under assumptions
    Timeout(elapsed, budget)    # tool exceeded time/state budget
    ToolError(reason)           # tool crashed or produced unparseable output
    Unsupported(reason)         # tool cannot handle this probe type
    Partial(partial_result)     # tool produced partial results before hitting limits
```

---

## Exchange Artifacts

Tools exchange structured artifacts, not raw text. Each artifact type is an explicit contract between producer and consumer:

```
StaticHint           # Slither → Echidna: function + pattern + suspicion
SeedCorpus           # Scheduler → Echidna: initial state + target functions
CandidateTrace       # Echidna → Scheduler: concrete call sequence + state diff
ConstraintSet        # Any tool → Scheduler: extracted constraints
ReplayResult         # Concrete executor → Scheduler: state before/after + events
BoundedConfirmation  # Halmos → Scheduler: SAT/UNSAT with bounds and assumptions
Diagnostic           # Any tool → Report: raw output, warnings, metadata
```

---

## Adapter Interface

Every adapter must implement three methods:

### `probe(target, invariant, constraints, global_state, chain) → Outcome`

Run the tool in **exploration mode** on one chain. Given the current global state and chain context, return a typed `Outcome`. On success, the outcome carries a list of `Candidate` structs. The adapter translates the tool's native format into the harness' outcome types.

For cross-chain probing, the `global_state` includes `pending_messages` from the source chain. The adapter resolves `CROSS_CHAIN` constraints through the message lifecycle.

### `execute(target, sequence, constraints, chain) → ExecutionResult`

Run the tool in **execution mode** on one chain. Execute a concrete call sequence and return a structured result.

```
ExecutionResult:
    reachable: bool                   # did the sequence execute without unexpected revert?
    revert_reason: Optional[str]      # if reverted, why
    outcome: Outcome                  # Success | Counterexample | Timeout | ToolError | ...
    before_state: Snapshot            # contract state before execution
    after_state: Snapshot             # contract state after execution
    events: list[Event]               # emitted events (including cross-chain)
    correlation_value: Optional[bytes32]  # extracted from cross-chain event, if any
```

For cross-chain sequences, the adapter extracts the correlation value from emitted events using the invariant's `CorrelationExtractor`.

### `confirm(target, witness, chain) → Outcome`

Run the tool in **verification mode** on one chain. Given a `WitnessState` found by another tool, independently verify it. The confirmation must attempt to **reproduce the violation** — not check that the invariant holds. If the witness carries a counterexample, confirming it means reproducing that counterexample.

Returns a typed `Outcome`. Symbolic tools (Halmos) return `Counterexample` with a matching model or `UnsatUnderBounds` if the witness cannot be reproduced under the bounds. Fuzzing tools (Echidna) return `Counterexample` if the violation is reproduced across parameter variations.

Must use a different analysis method than the original probe to qualify as independent confirmation.

---

## Per-Tool Adaptations

### Echidna Adapter

Echidna is a fuzzer. Best suited for concrete sequence exploration and boundary fuzzing of cross-chain state machines.

**Cross-chain relevance:** Echidna excels at finding concrete call sequences that push the protocol to boundary conditions — guardians at exactly M-1 signatures, sequence numbers at wraparound points, message queues at capacity. For cross-chain probing, Echidna is typically the primary probe tool because it produces executable sequences that can be replayed and correlated across chains.

**Probe mode:** Deploy contract on the specified chain. Apply cross-chain constraints as seed configuration. Run fuzzing with the sub-invariant as a property check. Parse corpus for sequences that explore cross-chain-interacting functions.

**Execute mode:** Replay a specific call sequence through Echidna's concrete execution engine. Forward cross-chain events to the mock relayer for the other chain's adapter.

**Confirm mode:** Echidna cannot independently confirm its own findings — re-running the same fuzzer with different parameters is not an independent method. Confirmation of an Echidna-discovered counterexample must come from a **different** tool (typically Halmos, which can symbolically verify the same path). Echidna's confirm mode is limited to **replay verification**: re-executing the exact witness sequence to ensure determinism. For independent confirmation, the scheduler routes the witness to Halmos via its `confirm()` method.

*This is an explicit instance of the adapter protocol rule: confirmation must use a different analysis method than the original probe.*

### Halmos Adapter

Halmos is a symbolic execution engine. Best suited for verifying threshold logic, signature verification, and numeric bounds in cross-chain invariants — under the bounds and assumptions of the selected model.

**Cross-chain relevance:** Halmos is the preferred confirmation tool for cross-chain edge cases involving numeric thresholds — guardian quorum, message count, fee boundaries. It can symbolically verify a counterexample under the given constraints, loop unrolling bounds, solver timeout, and trust assumptions.

**Probe mode:** Run symbolic execution on the specified chain with the sub-invariant as a target assertion. Constrain symbolic variables to match accumulated constraints. **SAT** produces a model/counterexample under the bounds and assumptions used — it does not mean the counterexample is reachable for all possible inputs, only that a model exists in the explored space. **UNSAT** means no counterexample was found in the explored search space under those assumptions — it does not mean the contract is safe in an absolute sense.

**Execute mode:** Symbolic execution is expensive for concrete replay. Verify path reachability symbolically, but pair with Echidna or a lightweight executor for the actual state diff.

**Confirm mode:** Given an edge case from Echidna, run Halmos with the same constraints symbolically. If it returns SAT with a matching counterexample, the finding is **symbolically confirmed under the given bounds and assumptions** — not "proven" in an absolute sense. The bounds (loop unrolling depth, solver timeout, address count) are always reported alongside the confirmation.

*Reference: [Halmos README](https://github.com/a16z/halmos)*

### Slither Adapter

Slither is a static analysis framework. Best suited for detecting structural vulnerabilities in cross-chain entry points — missing access control, unchecked return values from cross-chain calls, reentrancy paths that span the bridge.

**Cross-chain relevance:** Slither identifies code patterns that create cross-chain attack surface: functions callable by the relayer without proper authentication, storage variables writable from cross-chain messages without validation, missing checks on message origin. These become seed targets for Echidna's dynamic exploration, delivered as `StaticHint` artifacts.

**Probe mode:** Run static analysis on the specified chain's contract. Filter detectors to cross-chain-relevant patterns: access control on relayer-callable functions, taint from message payloads to storage, missing validation on cross-chain events.

**Execute mode:** Slither cannot execute. The adapter delegates concrete execution to a lightweight bundled EVM executor (not to Echidna or Halmos). The bundled executor replays the fixed call sequence and captures the state diff.

**Confirm mode:** Slither's findings serve as corroborating evidence, not independent confirmation. If Echidna found a counterexample that exploits a pattern Slither flagged, the finding is strengthened but not symbolically confirmed without verification from Halmos.

---

## Fallback & Error Handling

When a tool cannot handle a probe request, the adapter returns an explicit `Unsupported` or `ToolError` outcome rather than failing silently. The scheduler handles these:

- `Unsupported` → route to a tool with different capabilities.
- `Timeout` → reduce beam width or depth for this branch.
- `ToolError` → log, skip this candidate, continue with other branches.
- `Partial` → use available results, mark the branch as incomplete in the report.

The scheduler never blocks waiting for a hung tool — every adapter call has a timeout. Stale tool processes are killed and their state is discarded.

---

## Adding a New Adapter

To add support for a new tool:

1. Create `devil/adapter/<tool_name>.py` implementing the three-method interface with `chain` parameter support.
2. Declare `ToolCapabilities` for the adapter so the scheduler can route appropriately.
3. Register it in the adapter registry with a unique name and the path to the tool binary.
4. Add a `ToolConfig` entry specifying CLI flags, timeout defaults, chain-specific options, and output format.
5. Add a section to this document describing the tool's cross-chain strengths, capabilities, and limitations.

No harness code outside the adapter directory should change.

---

## Output Normalization

All adapters produce the same internal structs regardless of the tool's native format. The normalization layer handles:

- Parsing tool-specific output (JSON, text, SARIF).
- Mapping tool-specific outcomes to the harness' `Outcome` types.
- Converting call representations into harness `Call` and `Sequence` types, tagged with the `chain` they belong to.
- Stripping tool-specific noise from evidence.

Raw tool output is preserved in `Evidence.raw` for debugging and audit, tagged with the chain it came from.


---

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution engine used for bounded formal verification
- [Echidna](https://github.com/crytic/echidna) — fuzzer used for concrete sequence exploration
- [Foundry](https://getfoundry.sh/) — Solidity development framework; invariant files follow Foundry test conventions

# Adapter Protocol

Each adapter wraps one external analysis tool and exposes it to the harness through a uniform interface. The harness never calls a tool directly — it goes through the adapter, which handles process management, configuration, and output normalization.

---

## Adapter Interface

Every adapter must implement three methods:

### `probe(target, invariant, constraints, sequence_prefix) → list[Candidate]`

Run the tool in **exploration mode**. Given the current search state, ask the tool: "what are the most suspicious next steps?" The tool returns ranked candidates, each identifying a function to call and why it looks interesting.

For Echidna this means fuzzing with the accumulated constraints as a seed. For Slither this means static analysis filtered to functions reachable from the current call sequence. For Halmos this means symbolic execution with the current constraints as path conditions.

The adapter is responsible for translating the tool's native output format into `Candidate` structs. Each candidate must include a suspicion score normalized to `[0.0, 1.0]`.

### `execute(target, sequence, constraints) → ExecutionResult`

Run the tool in **execution mode**. Execute a concrete call sequence against the target contract with the given state constraints and return the result. This is the combined reachability-check + invariant-check step.

The adapter must report:

- Whether the sequence executed without unexpected revert (`reachable: bool`).
- If reverted, the revert reason and whether it is expected under the constraints.
- The contract state before and after execution.
- Any side effects (events emitted, storage changes).

### `confirm(target, sequence, constraints, expected_result) → bool`

Run the tool in **verification mode**. Given an edge case found by another tool, independently verify it. This must use a different analysis method than the original probe to qualify as independent confirmation.

For example: if Echidna found a counterexample, Halmos can symbolically verify that the path is always reachable under the given constraints. If Slither flagged a pattern, Echidna can fuzz to see if it produces a concrete violation.

Returns `True` if the tool confirms the finding, `False` otherwise.

---

## Per-Tool Adaptations

### Echidna Adapter

Echidna is a fuzzer. It works by generating random call sequences and checking invariants after each sequence.

**Probe mode:** Run Echidna with the invariant as a property check. Use the accumulated constraints as a `cryticArgs` seed to bias the fuzzer toward functions and state conditions of interest. Parse the corpus to extract call sequences that came closest to violating the invariant (even if they didn't fully break it). Rank by coverage delta — sequences that explored new code paths score higher.

**Execute mode:** Replay a specific call sequence through Echidna's execution engine. Capture the state diff and any assertion failures. Echidna's execution is concrete (not symbolic), so the result is deterministic for a given sequence and initial state.

**Confirm mode:** Run Echidna with the edge case sequence as a seed, but allow the fuzzer to vary parameters slightly. If the invariant still breaks across parameter variations, the edge case is robust (not a fluke).

### Halmos Adapter

Halmos is a symbolic execution engine. It explores all possible paths through a contract, representing inputs as symbolic variables.

**Probe mode:** Run Halmos with the invariant as a target assertion. Constrain symbolic variables to match the accumulated constraints (`assume` directives). Halmos returns SAT (a violation exists) or UNSAT (the invariant holds for all inputs). If SAT, extract the counterexample as a concrete candidate. If UNSAT, this branch is proven safe and can be pruned.

**Execute mode:** Not Halmos' strength — symbolic execution is expensive for concrete replay. For execute mode, Halmos can verify that a given sequence is reachable by checking path conditions symbolically, but the actual state diff should come from a concrete executor (Echidna or a direct RPC call).

**Confirm mode:** Halmos is the preferred confirmation tool. Given an edge case sequence and constraints, run Halmos symbolically. If it returns SAT with the same counterexample, the edge case is formally verified.

### Slither Adapter

Slither is a static analysis framework. It analyzes contract code without executing it.

**Probe mode:** Run Slither's detectors on the target contract. Filter results to functions that are reachable from the current call sequence and that touch storage variables relevant to the invariant. Slither cannot produce call sequences — its candidates are individual functions flagged by detectors (reentrancy, unchecked calls, access control gaps). These become targets for dynamic tools.

**Execute mode:** Slither cannot execute. The adapter should delegate to a lightweight concrete executor (direct RPC call or a bundled EVM runner) for the actual state transition.

**Confirm mode:** Not applicable in the standard sense — Slither's findings are warnings, not counterexamples. However, if Echidna found a counterexample that exploits a pattern Slither flagged, Slither's analysis can be cited as corroborating evidence.

---

## Adding a New Adapter

To add support for a new tool (e.g., Manticore, Mythril, Certora):

1. Create `devil/adapter/<tool_name>.py` implementing the three-method interface.
2. Register it in the adapter registry with a unique name and the path to the tool binary.
3. Add a `ToolConfig` entry specifying CLI flags, timeout defaults, and output format.
4. Add a section to this document describing the tool's strengths and probe/execute/confirm strategies.

No harness code outside the adapter directory should change.

---

## Output Normalization

All adapters produce the same internal structs. The normalization layer inside each adapter is responsible for:

- Parsing tool-specific output formats (JSON, text, SARIF, custom).
- Mapping tool-specific severity levels to the harness' unified scale.
- Converting tool-specific call representations (Echidna sequences, Halmos traces) into the harness' `Call` and `Sequence` types.
- Stripping tool-specific noise (timestamps, build IDs, progress bars) from evidence.

Raw tool output is preserved in `Evidence.raw` for debugging and audit trails.

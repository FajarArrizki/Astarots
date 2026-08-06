# Adapter Protocol

Each adapter wraps one external analysis tool and exposes it to the harness through a uniform interface. The harness never calls a tool directly — it goes through the adapter, which handles process management, chain-specific configuration, and output normalization.

For cross-chain probing, adapters receive a `chain` context. They deploy the target contract on the specified chain, configure the mock relayer, and restrict their analysis to that chain's sub-invariant. Tools never see two chains simultaneously — the decomposition and recombination happen at the harness layer.

---

## Adapter Interface

Every adapter must implement three methods:

### `probe(target, invariant, constraints, sequence_prefix, chain) → list[Candidate]`

Run the tool in **exploration mode** on one chain. Given the current search state and chain context, ask the tool: "what are the most suspicious next steps on this chain?" The tool returns ranked candidates.

For cross-chain probing, constraints may include `CROSS_CHAIN` conditions that reference the other chain's state. The adapter translates these into concrete preconditions on its chain — for example, a constraint that "source chain emitted TokensLocked(100)" becomes "mock relayer delivers a message with amount=100" on the destination chain.

### `execute(target, sequence, constraints, chain) → ExecutionResult`

Run the tool in **execution mode** on one chain. Execute a concrete call sequence against the target contract deployed on the specified chain. This is the combined reachability-check + invariant-check step.

For cross-chain sequences, the harness' mock relayer feeds events from the source chain into the destination chain's adapter. The adapter itself only sees its own chain.

### `confirm(target, sequence, constraints, chain, expected_result) → bool`

Run the tool in **verification mode** on one chain. Independently verify an edge case found by another tool on the same chain. Must use a different analysis method than the original probe.

---

## Per-Tool Adaptations

### Echidna Adapter

Echidna is a fuzzer. Best suited for concrete sequence exploration and boundary fuzzing of cross-chain state machines.

**Cross-chain relevance:** Echidna excels at finding concrete call sequences that push the protocol to boundary conditions — guardians at exactly M-1 signatures, sequence numbers at wraparound points, message queues at capacity. For cross-chain probing, Echidna is typically the primary probe tool because it produces executable sequences that can be replayed and correlated across chains.

**Probe mode:** Deploy contract on the specified chain. Apply cross-chain constraints as seed configuration. Run fuzzing with the sub-invariant as a property check. Parse corpus for sequences that explore cross-chain-interacting functions.

**Execute mode:** Replay a specific call sequence through Echidna's concrete execution engine. Forward cross-chain events to the mock relayer for the other chain's adapter.

**Confirm mode:** Re-run fuzzing with the edge case sequence as seed, varying parameters. If the sub-invariant holds across variations, the edge case is robust.

### Halmos Adapter

Halmos is a symbolic execution engine. Best suited for formally verifying threshold logic, signature verification, and numeric bounds in cross-chain invariants.

**Cross-chain relevance:** Halmos is the preferred confirmation tool for cross-chain edge cases involving numeric thresholds — guardian quorum, message count, fee boundaries. It can symbolically prove that a counterexample is reachable for all inputs within the given constraints, not just the concrete values Echidna found.

**Probe mode:** Run symbolic execution on the specified chain with the sub-invariant as a target assertion. Constrain symbolic variables to match accumulated constraints. SAT → extract counterexample. UNSAT → branch is proven safe on this chain.

**Execute mode:** Verify path reachability symbolically for a given sequence. Returns SAT/UNSAT but does not produce a concrete state diff — pair with Echidna for concrete execution.

**Confirm mode:** Given an edge case from Echidna, symbolically verify the path on the same chain. SAT with matching counterexample → confirmed.

### Slither Adapter

Slither is a static analysis framework. Best suited for detecting structural vulnerabilities in cross-chain entry points — missing access control, unchecked return values from cross-chain calls, reentrancy paths that span the bridge.

**Cross-chain relevance:** Slither identifies code patterns that create cross-chain attack surface: functions callable by the relayer without proper authentication, storage variables writable from cross-chain messages without validation, missing checks on message origin. These become seed targets for Echidna's dynamic exploration.

**Probe mode:** Run static analysis on the specified chain's contract. Filter detectors to cross-chain-relevant patterns: access control on relayer-callable functions, taint from message payloads to storage, missing validation on cross-chain events.

**Execute mode:** Slither cannot execute. Delegate to a lightweight concrete executor for state transitions.

**Confirm mode:** Slither's findings serve as corroborating evidence, not independent confirmation in the formal sense. If Echidna found a counterexample that exploits a pattern Slither flagged, the finding is strengthened but not "proven" without symbolic verification from Halmos.

---

## Adding a New Adapter

To add support for a new tool:

1. Create `devil/adapter/<tool_name>.py` implementing the three-method interface with `chain` parameter support.
2. Register it in the adapter registry with a unique name and the path to the tool binary.
3. Add a `ToolConfig` entry specifying CLI flags, timeout defaults, chain-specific options, and output format.
4. Add a section to this document describing the tool's cross-chain strengths.

No harness code outside the adapter directory should change.

---

## Output Normalization

All adapters produce the same internal structs regardless of the tool's native format. The normalization layer handles:

- Parsing tool-specific output (JSON, text, SARIF).
- Mapping severity levels to the harness' unified scale.
- Converting call representations into harness `Call` and `Sequence` types, tagged with the `chain` they belong to.
- Stripping tool-specific noise from evidence.

Raw tool output is preserved in `Evidence.raw` for debugging and audit, tagged with the chain it came from.

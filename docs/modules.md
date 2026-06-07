# Module Reference

All modules live under `src/npc_analysis/` and the public ones are re-exported from the `npc_analysis` package root.

| Module | Exports | Purpose |
|--------|---------|---------|
| `callback.py` | `TranspileCapture` | Pass-manager callback that snapshots routed + final circuits, the property-set layout, and the authoritative `TranspileLayout` |
| `swap_tracker.py` | `SwapTracker` | Classify gates as SWAP/real and track each qubit's physical trajectory |
| `qinfo.py` | `Qinfo` | Per-logical-qubit state container |
| `gate_calibration.py` | `GateCalibration` | Per-gate calibration + depolarizing probability |
| `qubit_calibration.py` | `QubitCalibration` | Per-qubit T1 / T2 / readout error |
| `noise_events.py` | `DepolarizingEvent`, `ThermalRelaxationEvent`, `ReadoutEvent`, `SwapNoiseEvent` | Event dataclasses populated during the walk |
| `fidelity_walker.py` | `FidelityWalker`, `QubitFidelityResult`, `CircuitFidelityResult` | Core noise application + per-qubit walk |
| `npc_analyzer.py` | `NPCAnalyzer`, `NPCResult`, `analyze_circuit` | Top-level orchestrator + one-shot helper |
| `swap_decomp_check.py` | `swap_decomp_2qubit` | Utility: transpile a lone SWAP to verify the backend's decomposition |

---

## callback.py — TranspileCapture

Hooks into Qiskit's `PassManager.run(..., callback=...)` to capture intermediate circuit states.

After every compiler pass the callback:

1. Snapshots the **routed circuit** if the pass is `FilterOpNodes` (SWAPs still explicit at this point).
2. Updates the **final circuit** from the current DAG on every invocation.
3. Captures `property_set["layout"]` whenever it appears (stored as `layout`).
4. Rebuilds the authoritative `TranspileLayout` via `TranspileLayout.from_property_set(dag, property_set)` on every invocation and keeps the latest non-`None` result as `transpile_layout`.

Step 4 exists because `dag_to_circuit` does **not** attach a `TranspileLayout` to the circuits captured here — so `final_index_layout()` (used for the analyzer's layout tripwire) reads from this separately-recorded `transpile_layout` rather than from `final.layout`.

### Captured fields

| Field | Type | Contents |
|-------|------|----------|
| `input_circuit` | `QuantumCircuit \| None` | The user's original circuit (passed in) |
| `routed` | `QuantumCircuit \| None` | Circuit after routing — SWAPs still explicit |
| `final` | `QuantumCircuit \| None` | Fully decomposed ISA circuit |
| `layout` | `Layout \| None` | Raw `property_set["layout"]` mapping |
| `transpile_layout` | `TranspileLayout \| None` | Authoritative layout for the final logical→physical check |

### Construction

```python
cap = TranspileCapture(input_circuit=qc)
```

You **must** pass `input_circuit` — `initial_index_layout()` uses it to distinguish your original qubits from ancillas the pass manager may add.

### `initial_index_layout(filter_ancillas=True) → list[int]`

Returns `[p_for_v0, p_for_v1, ...]` for the user's original qubits — mirrors `TranspileLayout.initial_index_layout(filter_ancillas=True)` on the bare `Layout` object.

Only `filter_ancillas=True` is implemented; passing `False` raises `NotImplementedError`.

### `final_index_layout(filter_ancillas=True) → list[int]`

Returns the **final** logical→physical array (after all routing SWAPs) by delegating to the captured `TranspileLayout.final_index_layout(...)`. Raises `RuntimeError` if no `TranspileLayout` was captured (i.e. the pass manager never finished). `NPCAnalyzer` uses this as the ground-truth for its layout tripwire when no explicit `expected_final_layout` is supplied.

---

## swap_tracker.py — SwapTracker

Classifies every gate in the final (decomposed) circuit as either part of a SWAP decomposition or a "real" gate, and tracks each logical qubit's physical trajectory.

### Constructor

| Parameter | Type | Description |
|-----------|------|-------------|
| `routed_qc` | `QuantumCircuit` | Circuit after routing (SWAPs explicit) |
| `final_qc` | `QuantumCircuit` | Fully decomposed ISA circuit |
| `backend` | IBM backend | Source of calibration data |
| `initial_layout` | `list[int] \| None` | Override layout; falls back to `final_qc.layout` or `capture` |
| `capture` | `TranspileCapture \| None` | Fallback layout source |

At least one of `initial_layout`, `final_qc.layout`, or `capture` must be set, otherwise the constructor raises `ValueError`.

### Native 2q gate resolution

`_resolve_native_2q_gate()` scans `backend.target.operation_names` for a single 2-qubit op that is not `swap`. If the backend exposes zero or more than one, the constructor raises `ValueError`.

### Oracle mechanism

`_initialize_oracle()` walks the routed circuit and builds per-qubit queues of expected 2q operations: `["swap", (p_c, p_t)]` or `["gate_name", (p_c, p_t)]`.

### SWAP detection: `_check_swap()`

When a native 2q gate is encountered in the final circuit:

1. Both involved logical qubits increment `pending_2q_count`.
2. `_check_swap()` asks: is the oracle's next op `"swap"`?
3. If yes and `pending_2q_count >= 3` for both qubits, verify the last 3 native 2q gates were on the same physical pair in the same order.
4. On confirmation (`track()` does the rest): bound the SWAP window, tag its ops, build a swap segment for each logical qubit, update qubit locations, and append the new location to each `path`.

### Segment construction (on confirmation)

When `_check_swap()` returns `True`, `track()`:

1. **Bounds the window** to the last 3 native 2q gates on `v_c` whose `swap_segment_index` is still unset (`candidate_czs` → `cz_steps`). Filtering to *unassigned* native 2q gates keeps an earlier SWAP's gates from being pulled into this one when a qubit also has pending real 2q traffic; if no unassigned native 2q gates remain it raises `RuntimeError`.
2. **Tags** every still-untagged op in `[first_step, last_step]` for both logical qubits via `_tag_swap_segment()`, setting `is_swap_gate=True` and a per-`Qinfo` `swap_segment_index`.
3. **Decrements** `pending_2q_count` by 3 on both qubits.
4. **Builds a `swap_segments` entry** per qubit via `collect_segment_ops()`: the tagged ops from *both* logical qubits are merged, de-duplicated by `(step_index, qubits)`, and sorted by `step_index`. Each segment records `phys_i`/`phys_j` as the **low/high** physical index of the swapped pair (`min`/`max`, not control/target order).
5. **Updates locations**: swaps `location` and the `p_contains` mapping for both qubits and appends the new physical to each `path`.

### `track() → SwapTracker`

Runs the full classification pass; returns `self` for chaining.

### `to_dict()`

Returns `{"native_2q_gate", "initial_layout", "final_v_to_p", "qubits", "qubit_calibrations", "gate_calibrations"}`. `final_v_to_p` and `qubits` are restricted to the user's logical qubits (`v < n_logical`); the idle/ancilla `Qinfo` entries the tracker keeps internally are excluded. The calibration sub-dicts are populated lazily — `to_dict()` first ensures every physical qubit on the initial layout or in any `Qinfo.location` has a cached `QubitCalibration`.

---

## qinfo.py — Qinfo

Per-logical-qubit state container. Tracks everything about one virtual qubit's journey through the circuit.

| Field | Type | Description |
|-------|------|-------------|
| `logic_ind` | `int` | Virtual qubit index |
| `location` | `int` | Current physical qubit |
| `path` | `list[int]` | Physical locations over time |
| `ops` | `list[dict]` | Every gate touching this qubit |
| `waiting_2q_ops` | `list` | Oracle queue |
| `pending_2q_count` | `int` | Unattributed native 2q gates |
| `swap_segments` | `list[dict]` | Full native gate sequences per SWAP |
| `measured` | `bool` | Whether the qubit was measured |
| `meas_error` | `float \| None` | Readout error at measurement |

`Qinfo.to_dict()` returns a JSON-friendly view used by `SwapTracker.to_dict()`.

---

## gate_calibration.py — GateCalibration

Pulls a specific gate's calibration from `backend.target` and computes the derived depolarizing probability.

### `GateCalibration.from_backend(backend, name, qubits) → GateCalibration`

Looks up `backend.target[name].get(qubits)`. If the entry is missing, or has `duration is None` or `duration == 0.0`, the gate is marked as **virtual** (zero noise).

### `depolarizing_probability` (property)

$$p = r \times \frac{d}{d-1}, \quad d = 2^{n_{qubits}}$$

| Gate type | $d$ | Formula |
|-----------|-----|---------|
| 1-qubit | 2 | $p = 2r$ |
| 2-qubit | 4 | $p = \frac{4}{3}r$ |

Returns `0.0` if the gate is virtual or `error is None`.

---

## qubit_calibration.py — QubitCalibration

Pulls a physical qubit's decoherence properties from the backend.

### `QubitCalibration.from_backend(backend, physical_index) → QubitCalibration`

Reads from `backend.target`:

- **T1** from `target.qubit_properties[i].t1`
- **T2** from `target.qubit_properties[i].t2`
- **readout_error** from `target["measure"][(physical_index,)].error`

Each field falls back to `None` if its source is unavailable, so missing data does not crash construction — the walker simply skips noise application for that channel.

---

## fidelity_walker.py — FidelityWalker

The core fidelity calculator. Walks each logical qubit's op list and applies noise channels.

### Static noise math

```python
FidelityWalker.apply_depolarizing(f, p)
    # 0.5 + (f - 0.5) * (1 - p)

FidelityWalker.apply_thermal_relaxation(f, t, t1, t2)
    # 0.5 + (f - 0.5) * (2/3 * exp(-t/t2) + 1/3 * exp(-t/t1))

FidelityWalker.apply_readout_error(f, e)
    # f * (1 - e)
```

### `FidelityWalker.from_swap_tracker(tracker, *, trace=False)`

Builds a walker from a tracker's calibration caches. Tops up the `QubitCalibration` cache so every physical qubit in every qubit's `path` has an entry (important because a logical qubit's path may include physicals that weren't part of any cached gate lookup).

### `walk_qubit(qi: Qinfo) → QubitFidelityResult`

Processes operations in order:

- **Regular gate**: apply depolarizing then thermal relaxation
- **SWAP segments**: each swap-tagged op carries a `swap_segment_index`; the mini-NPC fires once, when the segment's **last** op is reached (`step_index == max step in the segment`) and segments are applied strictly in order (`next_swap_segment`). It walks both physical qubits through the full native sequence via `_run_swap_mini_npc()`, sets `f = (f_i + f_j) / 2`, emits a `SwapNoiseEvent`, and advances `path_idx`.
- **Measure**: apply readout error
- **Virtual gates**: skipped (unless they're tagged `is_swap_gate`, in which case they participate in the mini-NPC walk)

After the walk, `walk_qubit` asserts its bookkeeping: every `swap_segments` entry was applied, `path_idx` landed on the last path index, and the walked `current_physical` equals `qi.location` — any mismatch raises `RuntimeError` rather than returning a silently wrong fidelity.

### `walk_circuit(quantuminfo, n_logical) → CircuitFidelityResult`

Walks all logical qubits and returns `circuit_fidelity = prod(per_qubit_fidelities)`.

---

## noise_events.py — Event Dataclasses

Four event types populated during the walk when `trace=True` (always on inside `NPCAnalyzer`).

### `DepolarizingEvent`

Emitted for every non-virtual gate with `depolarizing_probability > 0`.

| Field | Description |
|-------|-------------|
| `gate_name` | e.g. `"sx"`, `"cz"` |
| `qubits` | Physical qubit indices |
| `p` | Depolarizing probability |
| `f_before`, `f_after` | Fidelity before and after |

### `ThermalRelaxationEvent`

Emitted for every gate with non-zero duration when the qubit has both T1 and T2.

| Field | Description |
|-------|-------------|
| `gate_name` | Gate that caused the wait |
| `qubits` | Gate qubits |
| `duration` | Gate duration (seconds) |
| `t1`, `t2` | Qubit's relaxation times |
| `physical_qubit` | Which physical qubit |
| `f_before`, `f_after` | Fidelity before and after |

### `ReadoutEvent`

Emitted at measurement (if `meas_error > 0`).

| Field | Description |
|-------|-------------|
| `physical_qubit` | Measured physical qubit |
| `error` | Measurement error probability |
| `f_before`, `f_after` | Fidelity before and after |

### `SwapNoiseEvent`

Emitted per confirmed SWAP.

| Field | Description |
|-------|-------------|
| `physical_qubits` | The two physical qubits involved (`(phys_i, phys_j)`, low/high) |
| `native_ops` | Full list of native gates in the SWAP segment |
| `f_before` | Fidelity entering the SWAP |
| `f_phys_i`, `f_phys_j` | Fidelity of each physical qubit after the native sequence |
| `f_after` | Average: `(f_phys_i + f_phys_j) / 2` |
| `swap_segment_index` | Index of this segment within the qubit's `swap_segments` |
| `trigger_step` | `step_index` of the op that triggered the mini-NPC (the segment's last step) |

`swap_segment_index` and `trigger_step` exist for ordering/debugging and are **not** emitted in the JSON output (`_event_to_dict` omits them).

---

## npc_analyzer.py — NPCAnalyzer, NPCResult, analyze_circuit

### `NPCAnalyzer(backend, capture, output_path=None, expected_final_layout=None)`

Validates that `capture.routed` and `capture.final` are populated (raises `ValueError` otherwise). `output_path` may be `str` or `Path`; the parent directory is created on write.

`expected_final_layout` is the ground-truth final logical→physical layout for the tripwire (below). When omitted, `analyze()` falls back to `capture.final_index_layout()`. `analyze_circuit` always supplies it explicitly from the real transpiled output.

### `NPCAnalyzer.analyze() → NPCResult`

1. Build `SwapTracker` from capture's routed + final circuits and call `.track()`.
2. **Layout tripwire**: compare tracker's final per-qubit locations against `_expected_final_layout()` — the explicit `expected_final_layout` if given, else `capture.final_index_layout()` (which reads the captured `TranspileLayout`). Raise `AssertionError` on mismatch.
3. Build `FidelityWalker.from_swap_tracker(tracker, trace=True)`.
4. Compute `walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)`.
5. Merge tracker metadata + fidelity results into `NPCResult`.
6. If `output_path` is set, write `result.to_json()` to disk.

### `NPCResult`

Dataclass:

| Field | Type |
|-------|------|
| `circuit_fidelity` | `float` |
| `per_qubit` | `dict[int, QubitFidelityResult]` |
| `native_2q_gate` | `str` |
| `initial_layout` | `list[int]` |
| `final_layout` | `dict[int, int]` |
| `qubit_details` | `dict[int, dict]` |
| `qubit_calibrations` | `dict` |
| `gate_calibrations` | `dict` |

Methods: `to_dict()` (JSON-ready, stringifies int keys), `to_json(indent=2)`.

### `analyze_circuit(backend, circuit, *, output_path=None, optimization_level=0) → NPCResult`

One-shot helper. Transpiles `circuit` with `generate_preset_pass_manager` (capturing via `TranspileCapture`), then reads the **real** transpiled output's `out.layout.final_index_layout(filter_ancillas=True)` and passes it to `NPCAnalyzer` as `expected_final_layout` — so the tripwire checks the tracker against Qiskit's own final layout. Raises `RuntimeError` if `out.layout` is missing.

Defaults to `optimization_level=0` because the proxy assumes intact SWAP boundaries — higher levels can fold 1-qubit gates across SWAPs and break the oracle's invariants.

---

## swap_decomp_check.py — swap_decomp_2qubit

Transpiles a lone `swap(0, 1)` against the backend to verify the native decomposition. Returns:

```python
{
    "native_2q_gates": [str, ...],   # detected native 2q gate(s)
    "all_ops": {gate_name: count, ...},
    "two_qubit_ops": {gate_name: count, ...},
    "depth": int,
    "circuit": QuantumCircuit,
}
```

Useful for confirming the backend uses exactly 3 native 2q gates per SWAP — the invariant `SwapTracker._check_swap()` relies on.

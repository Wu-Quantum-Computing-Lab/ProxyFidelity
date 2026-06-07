# Module Reference

All modules live under `src/npc_analysis/` and the public ones are re-exported from the `npc_analysis` package root.

| Module | Exports | Purpose |
|--------|---------|---------|
| `callback.py` | `TranspileCapture` | Pass-manager callback that snapshots routed + final circuits + layout |
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
3. Captures `property_set["layout"]` whenever it appears.

### Construction

```python
cap = TranspileCapture(input_circuit=qc)
```

You **must** pass `input_circuit` — `initial_index_layout()` uses it to distinguish your original qubits from ancillas the pass manager may add.

### `initial_index_layout(filter_ancillas=True) → list[int]`

Returns `[p_for_v0, p_for_v1, ...]` for the user's original qubits — mirrors `TranspileLayout.initial_index_layout(filter_ancillas=True)` on the bare `Layout` object.

Only `filter_ancillas=True` is implemented; passing `False` raises `NotImplementedError`.

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
4. On confirmation: tag all ops in the step range as `is_swap_gate=True`, build a swap segment containing every 1q + 2q op in the range, update qubit locations, append to `path`.

### `track() → SwapTracker`

Runs the full classification pass; returns `self` for chaining.

### `to_dict()`

Returns `{"native_2q_gate", "initial_layout", "final_v_to_p", "qubits", "qubit_calibrations", "gate_calibrations"}`. The calibration sub-dicts are populated lazily — `to_dict()` first ensures every physical qubit on the initial layout or in any `Qinfo.location` has a cached `QubitCalibration`.

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
- **SWAP segments**: emit a `SwapNoiseEvent` once the 3rd swap-tagged 2q gate is seen; walk both physical qubits through the full native sequence via `_run_swap_mini_npc()` and set `f = (f_i + f_j) / 2`
- **Measure**: apply readout error
- **Virtual gates**: skipped (unless they're tagged `is_swap_gate`, in which case they participate in the mini-NPC walk)

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
| `physical_qubits` | The two physical qubits involved (`(phys_i, phys_j)`) |
| `native_ops` | Full list of native gates in the SWAP segment |
| `f_before` | Fidelity entering the SWAP |
| `f_phys_i`, `f_phys_j` | Fidelity of each physical qubit after the native sequence |
| `f_after` | Average: `(f_phys_i + f_phys_j) / 2` |

---

## npc_analyzer.py — NPCAnalyzer, NPCResult, analyze_circuit

### `NPCAnalyzer(backend, capture, output_path=None)`

Validates that `capture.routed` and `capture.final` are populated (raises `ValueError` otherwise). `output_path` may be `str` or `Path`; the parent directory is created on write.

### `NPCAnalyzer.analyze() → NPCResult`

1. Build `SwapTracker` from capture's routed + final circuits and call `.track()`.
2. **Layout tripwire**: compare tracker's final per-qubit locations against `cap.final.layout.final_index_layout(filter_ancillas=True)`. Raise `AssertionError` on mismatch.
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

One-shot helper. Defaults to `optimization_level=0` because the proxy assumes intact SWAP boundaries — higher levels can fold 1-qubit gates across SWAPs and break the oracle's invariants.

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

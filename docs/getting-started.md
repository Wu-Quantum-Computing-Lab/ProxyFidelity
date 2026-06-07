# Getting Started

## Prerequisites

- Python 3.13+
- An IBM Quantum account (for accessing backend calibration data via `QiskitRuntimeService`)

Pinned dependencies (resolved by `uv`):

- `qiskit >= 2.3.1`
- `qiskit-ibm-runtime >= 0.46.1`

## Installation

The project is managed with [uv](https://docs.astral.sh/uv/). From the package root:

```bash
cd npc_analysis/
uv sync
```

This creates `.venv/` and installs `npc_analysis` in editable mode. Run any script with `uv run` (which uses the venv) or invoke the interpreter directly via `.venv/bin/python`.

## Two Ways to Use It

### Option A — One-shot helper

`analyze_circuit()` does the transpile + analyze step for you. Use this when you don't need to customize the pass manager.

```python
from qiskit import QuantumCircuit
from qiskit_ibm_runtime import QiskitRuntimeService

from npc_analysis import analyze_circuit

qc = QuantumCircuit(3)
qc.h(0)
qc.cx(0, 1)
qc.cx(1, 2)
qc.measure_all()

backend = QiskitRuntimeService().backend("ibm_kingston")
result = analyze_circuit(backend, qc, output_path="results/my_analysis.json")
```

!!! important
    The proxy assumes intact SWAP boundaries (3 native 2q gates per SWAP). `analyze_circuit` defaults to `optimization_level=0` for this reason. Pass a higher level only if you are confident 1-qubit optimization will not fold gates across the SWAP boundary.

### Option B — Manual transpile + analyze

If you need custom layout / routing methods, or want to inspect the captured circuits, drive `TranspileCapture` and `NPCAnalyzer` yourself.

```python
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService

from npc_analysis import TranspileCapture, NPCAnalyzer

qc = QuantumCircuit(3)
qc.h(0)
qc.cx(0, 1)
qc.cx(1, 2)
qc.measure_all()

backend = QiskitRuntimeService().backend("ibm_kingston")

cap = TranspileCapture(input_circuit=qc)
pm = generate_preset_pass_manager(
    optimization_level=0,
    backend=backend,
    layout_method="sabre",
    routing_method="sabre",
)
pm.run(qc, callback=cap)

result = NPCAnalyzer(backend, cap, output_path="results/my_analysis.json").analyze()
```

!!! important
    You **must** set `input_circuit=qc` when constructing `TranspileCapture`. This lets the layout resolver distinguish your original qubits from ancillas the pass manager may add.

After `pm.run()` completes, the capture holds:

| Attribute | Contents |
|-----------|----------|
| `cap.routed` | Circuit after routing — SWAP gates are explicit `swap` instructions |
| `cap.final` | Fully decomposed ISA circuit — native gates only (e.g. `cz`, `sx`, `rz`) |
| `cap.layout` | The virtual→physical mapping captured from `property_set["layout"]` |

## Reading the Result

`NPCAnalyzer.analyze()` returns an `NPCResult`:

| Attribute | Type | Description |
|-----------|------|-------------|
| `circuit_fidelity` | `float` | Product of all per-qubit fidelities |
| `per_qubit` | `dict[int, QubitFidelityResult]` | Virtual qubit → `(fidelity, events)` |
| `native_2q_gate` | `str` | Name of the backend's native 2q gate (e.g. `"cz"`) |
| `initial_layout` | `list[int]` | `[p_for_v0, p_for_v1, ...]` after layout pass |
| `final_layout` | `dict[int, int]` | Virtual → physical after all SWAPs |
| `qubit_details` | `dict[int, dict]` | Per-qubit `path`, `location`, `ops` |
| `qubit_calibrations` | `dict` | T1 / T2 / readout error per physical qubit used |
| `gate_calibrations` | `dict` | Error / duration / depolarizing probability per gate used |

Tracing is always on, so every `QubitFidelityResult` carries the full event list:

```python
for v, qfr in result.per_qubit.items():
    print(f"qubit {v}: fidelity={qfr.fidelity:.4f}, events={len(qfr.events)}")
    for event in qfr.events:
        print(f"  {type(event).__name__}: {event.f_before:.4f} → {event.f_after:.4f}")
```

To dump JSON (also written automatically when `output_path` is set):

```python
print(result.to_json())
```

## Layout Tripwire

`NPCAnalyzer.analyze()` compares the `SwapTracker`'s final qubit locations against `cap.final.layout.final_index_layout()` and raises `AssertionError` if they disagree. A mismatch means readout / T1 / T2 would be pulled from the wrong physical qubits — surfacing the bug loudly is preferable to silently bad fidelity numbers.

If you see this assertion, your transpiler configuration is likely folding gates across SWAP boundaries; drop `optimization_level` back to `0` and rerun.

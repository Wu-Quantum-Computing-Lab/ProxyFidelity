from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .callback import TranspileCapture
from .swap_tracker import SwapTracker
from .fidelity_walker import FidelityWalker, CircuitFidelityResult, QubitFidelityResult
from .noise_events import (
    DepolarizingEvent,
    ThermalRelaxationEvent,
    ReadoutEvent,
    SwapNoiseEvent,
)


def _event_to_dict(event) -> dict:
    if isinstance(event, DepolarizingEvent):
        return {
            "type": "depolarizing",
            "gate": event.gate_name,
            "qubits": list(event.qubits),
            "p": event.p,
            "f_before": event.f_before,
            "f_after": event.f_after,
        }
    if isinstance(event, ThermalRelaxationEvent):
        return {
            "type": "thermal_relaxation",
            "gate": event.gate_name,
            "qubits": list(event.qubits),
            "duration": event.duration,
            "t1": event.t1,
            "t2": event.t2,
            "physical_qubit": event.physical_qubit,
            "f_before": event.f_before,
            "f_after": event.f_after,
        }
    if isinstance(event, ReadoutEvent):
        return {
            "type": "readout",
            "physical_qubit": event.physical_qubit,
            "error": event.error,
            "f_before": event.f_before,
            "f_after": event.f_after,
        }
    if isinstance(event, SwapNoiseEvent):
        return {
            "type": "swap",
            "physical_qubits": list(event.physical_qubits),
            "native_ops": event.native_ops,
            "f_before": event.f_before,
            "f_phys_i": event.f_phys_i,
            "f_phys_j": event.f_phys_j,
            "f_after": event.f_after,
        }
    return asdict(event)


@dataclass
class NPCResult:
    circuit_fidelity: float
    per_qubit: dict[int, QubitFidelityResult]
    native_2q_gate: str
    initial_layout: list[int]
    final_layout: dict[int, int]
    qubit_details: dict[int, dict]
    qubit_calibrations: dict
    gate_calibrations: dict

    def to_dict(self) -> dict:
        per_qubit_out = {}
        for v, qfr in self.per_qubit.items():
            detail = self.qubit_details.get(v, {})
            per_qubit_out[v] = {
                "fidelity": qfr.fidelity,
                "path": detail.get("path", []),
                "location": detail.get("location"),
                "events": [_event_to_dict(e) for e in qfr.events],
            }

        return {
            "circuit_fidelity": self.circuit_fidelity,
            "native_2q_gate": self.native_2q_gate,
            "initial_layout": self.initial_layout,
            "final_layout": {str(k): v for k, v in self.final_layout.items()},
            "per_qubit": {str(k): v for k, v in per_qubit_out.items()},
            "qubit_calibrations": self.qubit_calibrations,
            "gate_calibrations": self.gate_calibrations,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class NPCAnalyzer:

    def __init__(
        self,
        backend,
        capture: TranspileCapture,
        output_path=None,
        expected_final_layout: list[int] | None = None,
    ):
        if capture.routed is None:
            raise ValueError("capture.routed is None — transpilation must have run")
        if capture.final is None:
            raise ValueError("capture.final is None — transpilation must have run")

        self.backend = backend
        self.capture = capture
        self.output_path = Path(output_path) if output_path is not None else None
        self.expected_final_layout = (
            list(expected_final_layout)
            if expected_final_layout is not None
            else None
        )

    def _expected_final_layout(self) -> list[int]:
        if self.expected_final_layout is not None:
            return self.expected_final_layout
        return self.capture.final_index_layout()

    def analyze(self) -> NPCResult:
        tracker = SwapTracker(
            routed_qc=self.capture.routed,
            final_qc=self.capture.final,
            backend=self.backend,
            capture=self.capture,
        ).track()

        qiskit_final = self._expected_final_layout()
        tracker_final = [
            tracker.quantuminfo[v].location for v in range(tracker.n_logical)
        ]
        if tracker_final != qiskit_final:
            raise AssertionError(
                "SwapTracker layout mismatch:\n"
                f"  tracker: {tracker_final}\n"
                f"  qiskit : {qiskit_final}"
            )

        walker = FidelityWalker.from_swap_tracker(tracker, trace=True)
        fidelity_result = walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)
        tracker_dict = tracker.to_dict()

        result = NPCResult(
            circuit_fidelity=fidelity_result.circuit_fidelity,
            per_qubit=fidelity_result.per_qubit,
            native_2q_gate=tracker_dict["native_2q_gate"],
            initial_layout=tracker_dict["initial_layout"],
            final_layout=tracker_dict["final_v_to_p"],
            qubit_details=tracker_dict["qubits"],
            qubit_calibrations=tracker_dict["qubit_calibrations"],
            gate_calibrations=tracker_dict["gate_calibrations"],
        )

        if self.output_path is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(result.to_json())

        return result


def analyze_circuit(
    backend,
    circuit,
    *,
    output_path=None,
    optimization_level: int = 0,
) -> NPCResult:
    """Transpile ``circuit`` for ``backend`` and run the NPC proxy in one call.

    The proxy assumes intact SWAP boundaries, so the default
    ``optimization_level=0`` matches what ``NPCAnalyzer`` was validated against.
    Pass a higher level only if you know your circuit won't have its SWAPs
    folded by 1q optimization.

    Returns an :class:`NPCResult`. If ``output_path`` is given, the JSON
    representation is also written to disk.
    """
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    capture = TranspileCapture(input_circuit=circuit)
    pm = generate_preset_pass_manager(
        optimization_level=optimization_level, backend=backend
    )
    out = pm.run(circuit, callback=capture)
    if out.layout is None:
        raise RuntimeError("PassManager output is missing a TranspileLayout")
    expected_final_layout = list(
        out.layout.final_index_layout(filter_ancillas=True)
    )
    return NPCAnalyzer(
        backend,
        capture,
        output_path=output_path,
        expected_final_layout=expected_final_layout,
    ).analyze()

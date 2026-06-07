from dataclasses import dataclass, field
from math import exp, prod

from .qinfo import Qinfo
from .gate_calibration import GateCalibration
from .qubit_calibration import QubitCalibration
from .noise_events import (
    DepolarizingEvent,
    ThermalRelaxationEvent,
    ReadoutEvent,
    SwapNoiseEvent,
)


@dataclass
class QubitFidelityResult:
    logic_ind: int
    fidelity: float
    events: list = field(default_factory=list)


@dataclass
class CircuitFidelityResult:
    per_qubit: dict[int, QubitFidelityResult] = field(default_factory=dict)
    circuit_fidelity: float = 1.0


class FidelityWalker:

    def __init__(self, gate_cal_cache, qubit_cal_cache, *, trace=False):
        self._gate_cal: dict[tuple, GateCalibration] = dict(gate_cal_cache)
        self._qubit_cal: dict[int, QubitCalibration] = dict(qubit_cal_cache)
        self._trace = trace

    @classmethod
    def from_swap_tracker(cls, tracker, *, trace=False):
        qubit_cal = dict(tracker._qubit_cal_cache)
        for v, qi in tracker.quantuminfo.items():
            for p in qi.path:
                if p not in qubit_cal:
                    qubit_cal[p] = QubitCalibration.from_backend(tracker.backend, p)
        return cls(tracker._gate_cal_cache, qubit_cal, trace=trace)

    # ---- static noise math ----

    @staticmethod
    def apply_depolarizing(f: float, p: float) -> float:
        return 0.5 + (f - 0.5) * (1 - p)

    @staticmethod
    def apply_thermal_relaxation(f: float, t: float, t1: float, t2: float) -> float:
        return 0.5 + (f - 0.5) * (2 / 3 * exp(-t / t2) + 1 / 3 * exp(-t / t1))

    @staticmethod
    def apply_readout_error(f: float, e: float) -> float:
        return f * (1 - e)

    # ---- walking ----

    def _get_gate_cal(self, name: str, qubits) -> GateCalibration:
        return self._gate_cal[(name, tuple(qubits))]

    def _get_qubit_cal(self, physical: int) -> QubitCalibration:
        return self._qubit_cal[physical]

    def _apply_gate_noise(self, f: float, op: dict, physical: int) -> float:
        """Apply depolarizing then thermal relaxation for a single gate op."""
        cal = self._get_gate_cal(op["name"], op["qubits"])
        p = cal.depolarizing_probability
        if p > 0:
            f = self.apply_depolarizing(f, p)
        if cal.duration and cal.duration > 0:
            qcal = self._get_qubit_cal(physical)
            if qcal.t1 and qcal.t2:
                f = self.apply_thermal_relaxation(f, cal.duration, qcal.t1, qcal.t2)
        return f

    def _run_swap_mini_npc(self, f: float, segment: dict) -> tuple[float, float]:
        """Walk both physical qubits through the SWAP's full native gate
        sequence in circuit order (single-qubit AND two-qubit gates).
        Returns (f_phys_i, f_phys_j) where i and j are the two physical qubits."""
        p_i = segment["phys_i"]
        p_j = segment["phys_j"]
        f_i = f
        f_j = f

        for op in segment["all_ops"]:
            qubits = op["qubits"]
            if p_i in qubits:
                f_i = self._apply_gate_noise(f_i, op, p_i)
            if p_j in qubits:
                f_j = self._apply_gate_noise(f_j, op, p_j)

        return f_i, f_j

    def walk_qubit(self, qi: Qinfo) -> QubitFidelityResult:
        f = 1.0
        path_idx = 0
        current_physical = qi.path[0] if qi.path else 0
        events = []
        applied_swap_segments = set()
        next_swap_segment = 0
        segment_last_steps = []
        for segment_index, segment in enumerate(qi.swap_segments):
            if not segment["all_ops"]:
                raise RuntimeError(
                    f"swap segment {segment_index} for logical qubit "
                    f"{qi.logic_ind} has no native ops"
                )
            segment_last_steps.append(
                max(op["step_index"] for op in segment["all_ops"])
            )

        for op in qi.ops:
            if op.get("is_virtual", False) and not op.get("is_swap_gate", False):
                continue

            if op.get("is_swap_gate", False):
                segment_index = op.get("swap_segment_index")
                if segment_index is None:
                    raise RuntimeError(
                        f"swap-tagged op for logical qubit {qi.logic_ind} "
                        "has no swap_segment_index"
                    )
                if segment_index < next_swap_segment:
                    continue
                if not 0 <= segment_index < len(qi.swap_segments):
                    raise RuntimeError(
                        f"swap_segment_index {segment_index} out of range for "
                        f"logical qubit {qi.logic_ind}"
                    )
                if segment_index != next_swap_segment:
                    raise RuntimeError(
                        f"logical qubit {qi.logic_ind} encountered swap "
                        f"segment {segment_index} before segment "
                        f"{next_swap_segment}"
                    )
                if op["step_index"] != segment_last_steps[segment_index]:
                    continue

                segment = qi.swap_segments[segment_index]
                f_before = f
                f_i, f_j = self._run_swap_mini_npc(f, segment)
                f = (f_i + f_j) / 2

                if self._trace:
                    events.append(SwapNoiseEvent(
                        physical_qubits=(segment["phys_i"], segment["phys_j"]),
                        native_ops=[dict(o) for o in segment["all_ops"]],
                        f_before=f_before,
                        f_phys_i=f_i,
                        f_phys_j=f_j,
                        f_after=f,
                        swap_segment_index=segment_index,
                        trigger_step=op["step_index"],
                    ))

                applied_swap_segments.add(segment_index)
                next_swap_segment += 1
                path_idx += 1
                if path_idx < len(qi.path):
                    current_physical = qi.path[path_idx]
                continue

            if op["name"] == "measure":
                if qi.meas_error is not None and qi.meas_error > 0:
                    f_before = f
                    f = self.apply_readout_error(f, qi.meas_error)
                    if self._trace:
                        events.append(ReadoutEvent(
                            physical_qubit=current_physical,
                            error=qi.meas_error,
                            f_before=f_before,
                            f_after=f,
                        ))
                continue

            cal = self._get_gate_cal(op["name"], op["qubits"])

            p = cal.depolarizing_probability
            if p > 0:
                f_before = f
                f = self.apply_depolarizing(f, p)
                if self._trace:
                    events.append(DepolarizingEvent(
                        gate_name=op["name"],
                        qubits=tuple(op["qubits"]),
                        p=p,
                        f_before=f_before,
                        f_after=f,
                    ))

            if cal.duration and cal.duration > 0:
                qcal = self._get_qubit_cal(current_physical)
                if qcal.t1 and qcal.t2:
                    f_before = f
                    f = self.apply_thermal_relaxation(
                        f, cal.duration, qcal.t1, qcal.t2
                    )
                    if self._trace:
                        events.append(ThermalRelaxationEvent(
                            gate_name=op["name"],
                            qubits=tuple(op["qubits"]),
                            duration=cal.duration,
                            t1=qcal.t1,
                            t2=qcal.t2,
                            physical_qubit=current_physical,
                            f_before=f_before,
                            f_after=f,
                        ))

        if len(applied_swap_segments) != len(qi.swap_segments):
            missing = [
                i for i in range(len(qi.swap_segments))
                if i not in applied_swap_segments
            ]
            raise RuntimeError(
                f"logical qubit {qi.logic_ind} did not apply swap segment(s) "
                f"{missing}"
            )
        expected_path_idx = len(qi.path) - 1 if qi.path else 0
        if path_idx != expected_path_idx:
            raise RuntimeError(
                f"logical qubit {qi.logic_ind} ended at path index {path_idx}; "
                f"expected {expected_path_idx}"
            )
        if qi.path and current_physical != qi.location:
            raise RuntimeError(
                f"logical qubit {qi.logic_ind} ended on physical "
                f"{current_physical}; expected {qi.location}"
            )

        return QubitFidelityResult(
            logic_ind=qi.logic_ind,
            fidelity=f,
            events=events if self._trace else [],
        )

    def walk_circuit(self, quantuminfo, n_logical) -> CircuitFidelityResult:
        per_qubit = {}
        fidelities = []
        for v in range(n_logical):
            result = self.walk_qubit(quantuminfo[v])
            per_qubit[v] = result
            fidelities.append(result.fidelity)

        return CircuitFidelityResult(
            per_qubit=per_qubit,
            circuit_fidelity=prod(fidelities) if fidelities else 1.0,
        )

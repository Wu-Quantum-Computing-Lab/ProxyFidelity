from copy import deepcopy

from qiskit import QuantumCircuit

from .qubit_calibration import QubitCalibration
from .gate_calibration import GateCalibration
from .qinfo import Qinfo


class SwapTracker:
    """Track logical-qubit trajectories through a routed + decomposed circuit.

    Given the routed circuit (explicit `swap` gates, pre-translation) and the
    final decomposed circuit (swaps lowered to the backend's native 2q gate),
    classify each native 2q gate in the final circuit as either part of a
    SWAP or a real 2q op, and record each virtual qubit's physical trajectory.
    """

    def __init__(
        self,
        routed_qc: QuantumCircuit,
        final_qc: QuantumCircuit,
        backend,
        initial_layout: list[int] | None = None,
        capture=None,
    ):
        self.routed_qc = routed_qc
        self.final_qc = final_qc
        self.backend = backend
        self.target = backend.target

        self.native_2q_name = self._resolve_native_2q_gate()

        if initial_layout is not None:
            self.initial_layout = list(initial_layout)
        elif final_qc.layout is not None:
            self.initial_layout = final_qc.layout.initial_index_layout(filter_ancillas=True)
        elif capture is not None:
            self.initial_layout = capture.initial_index_layout()
        else:
            raise ValueError(
                "No layout available; pass initial_layout= or capture= "
                "(TranspileCapture with input_circuit set)"
            )
        self.n_logical = len(self.initial_layout)
        self.n_total = final_qc.num_qubits

        self.quantuminfo: dict[int, Qinfo] = {}
        self.p_contains: dict[int, int] = {}
        self._init_qinfo()

        self._gate_cal_cache: dict[tuple, GateCalibration] = {}
        self._qubit_cal_cache: dict[int, QubitCalibration] = {}

    def _resolve_native_2q_gate(self) -> str:
        candidates = [
            n for n in self.target.operation_names
            if self.target.operation_from_name(n).num_qubits == 2 and n != "swap"
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Expected exactly one native 2q gate on backend, found {candidates}"
            )
        return candidates[0]

    def _init_qinfo(self) -> None:
        for v, p in enumerate(self.initial_layout):
            self.quantuminfo[v] = Qinfo(logic_ind=v, location=p, path=[p])
            self.p_contains[p] = v

        idle = [p for p in range(self.n_total) if p not in self.initial_layout]
        for i, p in enumerate(idle):
            v = self.n_logical + i
            self.quantuminfo[v] = Qinfo(logic_ind=v, location=p, path=[p])
            self.p_contains[p] = v

    def _qubit_indices(self, qc: QuantumCircuit, qargs) -> tuple:
        return tuple(qc.find_bit(q).index for q in qargs)

    def _gate_cal(self, name: str, qubits: tuple) -> GateCalibration:
        key = (name, tuple(qubits))
        if key not in self._gate_cal_cache:
            self._gate_cal_cache[key] = GateCalibration.from_backend(
                self.backend, name, key[1]
            )
        return self._gate_cal_cache[key]

    def _qubit_cal(self, physical_index: int) -> QubitCalibration:
        if physical_index not in self._qubit_cal_cache:
            self._qubit_cal_cache[physical_index] = QubitCalibration.from_backend(
                self.backend, physical_index
            )
        return self._qubit_cal_cache[physical_index]

    def _initialize_oracle(self) -> None:
        p_contains = deepcopy(self.p_contains)
        for instr in self.routed_qc.data:
            op = instr.operation
            if op.num_qubits != 2:
                continue
            p_c, p_t = self._qubit_indices(self.routed_qc, instr.qubits)
            v_c = p_contains[p_c]
            v_t = p_contains[p_t]
            if op.name == "swap":
                self.quantuminfo[v_c].waiting_2q_ops.append(["swap", (p_c, p_t)])
                self.quantuminfo[v_t].waiting_2q_ops.append(["swap", (p_t, p_c)])
                p_contains[p_c] = v_t
                p_contains[p_t] = v_c
            else:
                self.quantuminfo[v_c].waiting_2q_ops.append([op.name, (p_c, p_t)])
                self.quantuminfo[v_t].waiting_2q_ops.append([op.name, (p_c, p_t)])

    def _check_swap(self, v_c: int, v_t: int) -> bool:
        # pending_2q_count tracks unattributed native 2q gates per qubit.
        # Real ops: +1 in track(), -1 here = net 0.
        # SWAP ops: +1 three times in track(), then -3 on confirmation = net 0.
        qi_c = self.quantuminfo[v_c]
        qi_t = self.quantuminfo[v_t]
        if not qi_c.waiting_2q_ops or not qi_t.waiting_2q_ops:
            return False

        if qi_c.waiting_2q_ops[0][0] != "swap":
            del qi_c.waiting_2q_ops[0]
            del qi_t.waiting_2q_ops[0]
            qi_c.pending_2q_count -= 1
            qi_t.pending_2q_count -= 1
            return False

        if qi_c.pending_2q_count < 3 or qi_t.pending_2q_count < 3:
            return False

        links_c = [o["qubits"] for o in qi_c.ops if o["name"] == self.native_2q_name][-3:]
        links_t = [o["qubits"] for o in qi_t.ops if o["name"] == self.native_2q_name][-3:]
        for i in range(3):
            if links_c[i] != links_t[i]:
                return False

        del qi_c.waiting_2q_ops[0]
        del qi_t.waiting_2q_ops[0]
        return True

    def _tag_swap_segment(
        self,
        qi: Qinfo,
        first_step: int,
        last_step: int,
        segment_index: int,
    ) -> None:
        """Tag unassigned ops in the swap window with this qubit's segment index."""
        for entry in reversed(qi.ops):
            step = entry.get("step_index", -1)
            if step < first_step:
                break
            if first_step <= step <= last_step:
                if entry.get("swap_segment_index") is not None:
                    continue
                entry["is_swap_gate"] = True
                entry["swap_segment_index"] = segment_index

    def _record_op(self, v: int, entry: dict) -> None:
        self.quantuminfo[v].ops.append(entry)

    def track(self) -> "SwapTracker":
        self._initialize_oracle()

        for step_index, instr in enumerate(self.final_qc.data):
            op = instr.operation
            name = op.name
            qubits = self._qubit_indices(self.final_qc, instr.qubits)

            if name == "barrier":
                continue

            if name == "measure":
                p = qubits[0]
                v = self.p_contains[p]
                cal = self._gate_cal("measure", qubits)
                qi = self.quantuminfo[v]
                qi.measured = True
                qi.meas_error = cal.error
                qi.ops.append({
                    "name": "measure",
                    "qubits": list(qubits),
                    "step_index": step_index,
                    "is_swap_gate": False,
                    "is_virtual": cal.is_virtual,
                })
                continue

            if op.num_qubits == 2 and name == self.native_2q_name:
                p_c, p_t = qubits
                v_c = self.p_contains[p_c]
                v_t = self.p_contains[p_t]
                cal = self._gate_cal(name, qubits)
                entry_c = {
                    "name": name,
                    "qubits": list(qubits),
                    "step_index": step_index,
                    "is_swap_gate": False,
                    "is_virtual": cal.is_virtual,
                }
                entry_t = dict(entry_c)
                self.quantuminfo[v_c].ops.append(entry_c)
                self.quantuminfo[v_t].ops.append(entry_t)
                self.quantuminfo[v_c].pending_2q_count += 1
                self.quantuminfo[v_t].pending_2q_count += 1

                if self._check_swap(v_c, v_t):
                    qi_c = self.quantuminfo[v_c]
                    qi_t = self.quantuminfo[v_t]

                    # Start from the same native-2q window that confirmed this
                    # routed SWAP, then drop gates already owned by an earlier
                    # segment. Looking across all unassigned CZs can reach far
                    # behind the current SWAP when a logical qubit has pending
                    # real 2q traffic.
                    candidate_czs = [
                        o for o in qi_c.ops
                        if o["name"] == self.native_2q_name
                    ][-3:]
                    cz_steps = [
                        o["step_index"] for o in candidate_czs
                        if o.get("swap_segment_index") is None
                    ]
                    if not cz_steps:
                        raise RuntimeError(
                            f"confirmed swap on {(p_c, p_t)} without unassigned "
                            "native 2q gates"
                        )
                    first_step, last_step = cz_steps[0], cz_steps[-1]

                    # Tag unassigned ops in the selected range for both logical
                    # qubits. Segment indexes are local to each Qinfo.
                    segment_index_c = len(qi_c.swap_segments)
                    segment_index_t = len(qi_t.swap_segments)
                    self._tag_swap_segment(qi_c, first_step, last_step, segment_index_c)
                    self._tag_swap_segment(qi_t, first_step, last_step, segment_index_t)

                    qi_c.pending_2q_count -= 3
                    qi_t.pending_2q_count -= 3

                    def collect_segment_ops(output_segment_index: int):
                        all_ops = []
                        seen = set()
                        for source_qi, source_segment_index in (
                            (qi_c, segment_index_c),
                            (qi_t, segment_index_t),
                        ):
                            for o in source_qi.ops:
                                if o.get("swap_segment_index") != source_segment_index:
                                    continue
                                key = (o.get("step_index", -1), tuple(o["qubits"]))
                                if key in seen:
                                    continue
                                seen.add(key)
                                copied = dict(o)
                                copied["swap_segment_index"] = output_segment_index
                                all_ops.append(copied)
                        all_ops.sort(key=lambda o: o["step_index"])
                        return all_ops

                    p_lo, p_hi = min(p_c, p_t), max(p_c, p_t)
                    qi_c.swap_segments.append({
                        "phys_i": p_lo,
                        "phys_j": p_hi,
                        "all_ops": collect_segment_ops(segment_index_c),
                    })
                    qi_t.swap_segments.append({
                        "phys_i": p_lo,
                        "phys_j": p_hi,
                        "all_ops": collect_segment_ops(segment_index_t),
                    })

                    qi_c.path.append(p_t)
                    qi_t.path.append(p_c)
                    qi_c.location = p_t
                    qi_t.location = p_c
                    self.p_contains[p_t] = v_c
                    self.p_contains[p_c] = v_t
                continue

            if op.num_qubits == 1:
                p = qubits[0]
                if p not in self.p_contains:
                    continue
                v = self.p_contains[p]
                cal = self._gate_cal(name, qubits)
                self.quantuminfo[v].ops.append({
                    "name": name,
                    "qubits": list(qubits),
                    "step_index": step_index,
                    "is_swap_gate": False,
                    "is_virtual": cal.is_virtual,
                })
                continue

        return self

    def to_dict(self) -> dict:
        for p in self.initial_layout:
            if p not in self._qubit_cal_cache:
                self._qubit_cal(p)
        for v, qi in self.quantuminfo.items():
            if qi.location not in self._qubit_cal_cache:
                self._qubit_cal(qi.location)

        return {
            "native_2q_gate": self.native_2q_name,
            "initial_layout": list(self.initial_layout),
            "final_v_to_p": {
                v: qi.location for v, qi in self.quantuminfo.items()
                if v < self.n_logical
            },
            "qubits": {
                v: qi.to_dict() for v, qi in self.quantuminfo.items()
                if v < self.n_logical
            },
            "qubit_calibrations": {
                p: cal.to_dict() for p, cal in sorted(self._qubit_cal_cache.items())
            },
            "gate_calibrations": {
                f"{name}{list(qs)}": cal.to_dict()
                for (name, qs), cal in self._gate_cal_cache.items()
            },
        }

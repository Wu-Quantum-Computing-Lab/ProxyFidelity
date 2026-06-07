import warnings
from copy import deepcopy
from math import exp, prod

import pytest
from qiskit import QuantumCircuit
from qiskit.circuit.library import QFT
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeFez, FakeTorino

from npc_analysis import (
    TranspileCapture,
    SwapTracker,
    FidelityWalker,
    QubitFidelityResult,
    CircuitFidelityResult,
    DepolarizingEvent,
    ThermalRelaxationEvent,
    ReadoutEvent,
    SwapNoiseEvent,
)
from npc_analysis.qinfo import Qinfo
from npc_analysis.gate_calibration import GateCalibration
from npc_analysis.qubit_calibration import QubitCalibration


@pytest.fixture(scope="session")
def backend():
    return FakeFez()


def _build_tracker(backend, qc, **pm_kwargs):
    cap = TranspileCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend, **pm_kwargs)
    final = pm.run(qc, callback=cap)
    tracker = SwapTracker(
        routed_qc=cap.routed,
        final_qc=final,
        backend=backend,
        capture=cap,
    ).track()
    return tracker, cap, final


@pytest.fixture(scope="module")
def qft7_tracker():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        backend = FakeTorino()
        qc = QFT(7).decompose()
        tracker, _, _ = _build_tracker(backend, qc, seed_transpiler=0)
    return tracker


def _swap_events_for(result, v):
    return [
        event for event in result.per_qubit[v].events
        if isinstance(event, SwapNoiseEvent)
    ]


def _native_2q_steps(segment):
    return [
        op["step_index"] for op in segment["all_ops"]
        if len(op["qubits"]) == 2
    ]


# ---- static math tests (no backend needed) ----


def test_apply_depolarizing():
    f = FidelityWalker.apply_depolarizing(1.0, 0.1)
    assert f == pytest.approx(0.5 + 0.5 * 0.9)


def test_apply_depolarizing_zero_noise():
    assert FidelityWalker.apply_depolarizing(0.8, 0.0) == pytest.approx(0.8)


def test_apply_depolarizing_full_noise():
    assert FidelityWalker.apply_depolarizing(0.8, 1.0) == pytest.approx(0.5)


def test_apply_thermal_relaxation():
    f = FidelityWalker.apply_thermal_relaxation(1.0, 1e-6, 100e-6, 50e-6)
    expected = 0.5 + 0.5 * (2 / 3 * exp(-1e-6 / 50e-6) + 1 / 3 * exp(-1e-6 / 100e-6))
    assert f == pytest.approx(expected)


def test_apply_readout_error():
    assert FidelityWalker.apply_readout_error(0.9, 0.05) == pytest.approx(0.9 * 0.95)


# ---- walk_qubit tests using fake backend calibration data ----


def test_empty_ops_gives_unity(backend):
    qi = Qinfo(logic_ind=0, location=0, path=[0])
    gate_cal_cache = {}
    phys = 0
    qubit_cal_cache = {phys: QubitCalibration.from_backend(backend, phys)}
    walker = FidelityWalker(gate_cal_cache, qubit_cal_cache)
    result = walker.walk_qubit(qi)
    assert result.fidelity == pytest.approx(1.0)
    assert result.events == []


def test_virtual_gate_no_noise(backend):
    phys = 0
    rz_cal = GateCalibration.from_backend(backend, "rz", (phys,))
    assert rz_cal.is_virtual, "rz should be virtual on Heron-class backends"

    qi = Qinfo(logic_ind=0, location=phys, path=[phys])
    qi.ops.append({
        "name": "rz",
        "qubits": [phys],
        "step_index": 0,
        "is_swap_gate": False,
        "is_virtual": True,
    })

    walker = FidelityWalker(
        {("rz", (phys,)): rz_cal},
        {phys: QubitCalibration.from_backend(backend, phys)},
    )
    result = walker.walk_qubit(qi)
    assert result.fidelity == pytest.approx(1.0)


def test_single_depolarizing(backend):
    edge = backend.coupling_map.get_edges()[0]
    p_c, p_t = edge
    native_2q = [
        n for n in backend.target.operation_names
        if backend.target.operation_from_name(n).num_qubits == 2 and n != "swap"
    ][0]
    cal = GateCalibration.from_backend(backend, native_2q, (p_c, p_t))
    p = cal.depolarizing_probability
    assert p > 0, "need a gate with nonzero error for this test"

    expected = 0.5 + (1.0 - 0.5) * (1 - p)

    qi = Qinfo(logic_ind=0, location=p_c, path=[p_c])
    qi.ops.append({
        "name": native_2q,
        "qubits": [p_c, p_t],
        "step_index": 0,
        "is_swap_gate": False,
        "is_virtual": cal.is_virtual,
    })

    qcal_c = QubitCalibration.from_backend(backend, p_c)
    qcal_t = QubitCalibration.from_backend(backend, p_t)
    walker = FidelityWalker(
        {(native_2q, (p_c, p_t)): cal},
        {p_c: qcal_c, p_t: qcal_t},
    )
    result = walker.walk_qubit(qi)

    # Fidelity after depolarizing + thermal (both applied)
    f_after_depol = expected
    if cal.duration and cal.duration > 0 and qcal_c.t1 and qcal_c.t2:
        f_after_both = 0.5 + (f_after_depol - 0.5) * (
            2 / 3 * exp(-cal.duration / qcal_c.t2)
            + 1 / 3 * exp(-cal.duration / qcal_c.t1)
        )
        assert result.fidelity == pytest.approx(f_after_both)
    else:
        assert result.fidelity == pytest.approx(f_after_depol)


def test_readout_error(backend):
    phys = 0
    meas_cal = GateCalibration.from_backend(backend, "measure", (phys,))
    qcal = QubitCalibration.from_backend(backend, phys)
    e = meas_cal.error
    assert e is not None and e > 0, "need nonzero readout error"

    qi = Qinfo(logic_ind=0, location=phys, path=[phys])
    qi.measured = True
    qi.meas_error = e
    qi.ops.append({
        "name": "measure",
        "qubits": [phys],
        "step_index": 0,
        "is_swap_gate": False,
        "is_virtual": meas_cal.is_virtual,
    })

    walker = FidelityWalker(
        {("measure", (phys,)): meas_cal},
        {phys: qcal},
    )
    result = walker.walk_qubit(qi)
    assert result.fidelity == pytest.approx(1.0 * (1 - e))


def test_circuit_fidelity_is_product(backend):
    qc = QuantumCircuit(3)
    qc.h(0)
    qc.cx(0, 1)
    qc.cx(1, 2)
    qc.measure_all()

    tracker, _, _ = _build_tracker(backend, qc)
    walker = FidelityWalker.from_swap_tracker(tracker, trace=False)
    result = walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)

    per_qubit_fids = [r.fidelity for r in result.per_qubit.values()]
    assert result.circuit_fidelity == pytest.approx(prod(per_qubit_fids))
    assert result.circuit_fidelity < 1.0, "noisy circuit should have fidelity < 1"


def test_swap_mini_npc(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)

    tracker, cap, _ = _build_tracker(
        backend, qc, layout_method="sabre", routing_method="sabre"
    )
    n_swaps = sum(1 for instr in cap.routed.data if instr.operation.name == "swap")
    if n_swaps == 0:
        pytest.skip("no swaps inserted by routing, can't test swap mini-NPC")

    walker = FidelityWalker.from_swap_tracker(tracker, trace=True)
    result = walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)

    swap_events = []
    for v, qr in result.per_qubit.items():
        for ev in qr.events:
            if isinstance(ev, SwapNoiseEvent):
                swap_events.append(ev)

    assert len(swap_events) > 0, "expected at least one SwapNoiseEvent"
    for ev in swap_events:
        assert ev.f_after == pytest.approx((ev.f_phys_i + ev.f_phys_j) / 2)
        assert ev.f_after < ev.f_before, "SWAP should degrade fidelity"
        n_2q = sum(1 for o in ev.native_ops if len(o["qubits"]) == 2)
        assert n_2q == 3, f"expected 3 two-qubit gates in swap, got {n_2q}"
        assert len(ev.native_ops) > 3, "swap segment should include single-qubit gates"


def test_swap_advances_path(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)

    tracker, cap, _ = _build_tracker(
        backend, qc, layout_method="sabre", routing_method="sabre"
    )
    n_swaps = sum(1 for instr in cap.routed.data if instr.operation.name == "swap")
    if n_swaps == 0:
        pytest.skip("no swaps inserted")

    for v in range(tracker.n_logical):
        qi = tracker.quantuminfo[v]
        if len(qi.path) > 1:
            result = FidelityWalker.from_swap_tracker(tracker, trace=True).walk_qubit(qi)
            swap_events = [e for e in result.events if isinstance(e, SwapNoiseEvent)]
            assert len(swap_events) == len(qi.path) - 1, (
                f"v={v}: {len(swap_events)} swap events but path has "
                f"{len(qi.path)} entries (expected {len(qi.path) - 1} swaps)"
            )


def test_trace_records_events(backend):
    qc = QuantumCircuit(2)
    edge = backend.coupling_map.get_edges()[0]
    qc.cx(0, 1)
    qc.measure_all()

    tracker, _, _ = _build_tracker(
        backend, qc, initial_layout=[edge[0], edge[1]]
    )
    walker = FidelityWalker.from_swap_tracker(tracker, trace=True)
    result = walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)

    all_events = []
    for qr in result.per_qubit.values():
        all_events.extend(qr.events)

    assert len(all_events) > 0, "trace should produce events"
    event_types = {type(e) for e in all_events}
    assert DepolarizingEvent in event_types or ThermalRelaxationEvent in event_types, (
        "expected at least depolarizing or thermal events from a cx gate"
    )
    assert ReadoutEvent in event_types, "expected readout events from measure"

    for ev in all_events:
        if isinstance(ev, DepolarizingEvent):
            assert ev.f_after == pytest.approx(
                0.5 + (ev.f_before - 0.5) * (1 - ev.p)
            )
        elif isinstance(ev, ThermalRelaxationEvent):
            assert ev.f_after == pytest.approx(
                0.5 + (ev.f_before - 0.5) * (
                    2 / 3 * exp(-ev.duration / ev.t2)
                    + 1 / 3 * exp(-ev.duration / ev.t1)
                )
            )
        elif isinstance(ev, ReadoutEvent):
            assert ev.f_after == pytest.approx(ev.f_before * (1 - ev.error))


def test_fidelity_walker_from_swap_tracker(backend):
    qc = QuantumCircuit(3)
    qc.h(0)
    qc.cx(0, 1)
    qc.cx(1, 2)
    qc.measure_all()

    tracker, _, _ = _build_tracker(backend, qc)
    walker = FidelityWalker.from_swap_tracker(tracker, trace=False)
    result = walker.walk_circuit(tracker.quantuminfo, tracker.n_logical)

    assert isinstance(result, CircuitFidelityResult)
    assert len(result.per_qubit) == 3
    assert 0.0 < result.circuit_fidelity < 1.0


def test_every_swap_segment_applied_once_qft7(qft7_tracker):
    walker = FidelityWalker.from_swap_tracker(qft7_tracker, trace=True)
    result = walker.walk_circuit(qft7_tracker.quantuminfo, qft7_tracker.n_logical)

    for v in range(qft7_tracker.n_logical):
        assert len(_swap_events_for(result, v)) == len(
            qft7_tracker.quantuminfo[v].swap_segments
        )


def test_swap_segments_do_not_share_czs(qft7_tracker):
    for v in range(qft7_tracker.n_logical):
        seen = {}
        duplicates = []
        for segment_index, segment in enumerate(qft7_tracker.quantuminfo[v].swap_segments):
            for step in _native_2q_steps(segment):
                if step in seen:
                    duplicates.append((step, seen[step], segment_index))
                else:
                    seen[step] = segment_index

        assert not duplicates, f"v={v} has CZ step(s) in multiple swap segments: {duplicates}"


def test_walker_applies_owning_segment(qft7_tracker):
    walker = FidelityWalker.from_swap_tracker(qft7_tracker, trace=True)
    result = walker.walk_circuit(qft7_tracker.quantuminfo, qft7_tracker.n_logical)

    for v in range(qft7_tracker.n_logical):
        qi = qft7_tracker.quantuminfo[v]
        events = _swap_events_for(result, v)
        assert len(events) == len(qi.swap_segments)

        for segment_index, (event, segment) in enumerate(zip(events, qi.swap_segments)):
            assert event.swap_segment_index == segment_index
            assert event.trigger_step == max(
                op["step_index"] for op in segment["all_ops"]
            )
            assert any(
                op.get("swap_segment_index") == segment_index
                and op["step_index"] == event.trigger_step
                for op in qi.ops
            )
            assert [
                op["step_index"] for op in event.native_ops
            ] == [
                op["step_index"] for op in segment["all_ops"]
            ]
            assert all(
                op.get("swap_segment_index") == segment_index
                for op in event.native_ops
            )


def test_current_physical_fully_advances(qft7_tracker):
    walker = FidelityWalker.from_swap_tracker(qft7_tracker, trace=True)

    for v in range(qft7_tracker.n_logical):
        qi = deepcopy(qft7_tracker.quantuminfo[v])
        if not qi.swap_segments:
            continue

        qi.measured = True
        qi.meas_error = 0.25
        qi.ops.append({
            "name": "measure",
            "qubits": [qi.location],
            "step_index": max(op["step_index"] for op in qi.ops) + 1,
            "is_swap_gate": False,
            "is_virtual": False,
        })

        result = walker.walk_qubit(qi)
        readout_events = [
            event for event in result.events
            if isinstance(event, ReadoutEvent)
        ]
        assert readout_events[-1].physical_qubit == qi.location


def test_walk_qubit_applies_two_swap_segments_numerically():
    cal = GateCalibration(
        name="cz",
        qubits=(0, 1),
        error=0.03,
        duration=0.0,
        is_virtual=False,
    )
    walker = FidelityWalker(
        {("cz", (0, 1)): cal},
        {
            0: QubitCalibration(physical_index=0, t1=None, t2=None, readout_error=None),
            1: QubitCalibration(physical_index=1, t1=None, t2=None, readout_error=None),
        },
    )

    def op(step_index, segment_index):
        return {
            "name": "cz",
            "qubits": [0, 1],
            "step_index": step_index,
            "is_swap_gate": True,
            "is_virtual": False,
            "swap_segment_index": segment_index,
        }

    ops = [op(1, 0), op(2, 0), op(3, 0), op(4, 1), op(5, 1)]
    segments = [
        {"phys_i": 0, "phys_j": 1, "all_ops": [op(1, 0), op(2, 0), op(3, 0)]},
        {"phys_i": 0, "phys_j": 1, "all_ops": [op(4, 1), op(5, 1)]},
    ]
    qi = Qinfo(
        logic_ind=0,
        location=0,
        path=[0, 1, 0],
        ops=ops,
        swap_segments=segments,
    )

    expected = 1.0
    for segment in segments:
        f_i, f_j = walker._run_swap_mini_npc(expected, segment)
        expected = (f_i + f_j) / 2

    assert walker.walk_qubit(qi).fidelity == pytest.approx(expected)


def test_walk_qubit_rejects_out_of_order_swap_segments():
    def op(step_index, segment_index):
        return {
            "name": "cz",
            "qubits": [0, 1],
            "step_index": step_index,
            "is_swap_gate": True,
            "is_virtual": False,
            "swap_segment_index": segment_index,
        }

    qi = Qinfo(
        logic_ind=0,
        location=0,
        path=[0, 1, 0],
        ops=[op(5, 1), op(10, 0)],
        swap_segments=[
            {"phys_i": 0, "phys_j": 1, "all_ops": [op(10, 0)]},
            {"phys_i": 0, "phys_j": 1, "all_ops": [op(5, 1)]},
        ],
    )

    with pytest.raises(RuntimeError, match="before segment 0"):
        FidelityWalker({}, {}).walk_qubit(qi)

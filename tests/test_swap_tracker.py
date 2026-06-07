import pytest
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeFez

from npc_analysis import TranspileCapture, SwapTracker
from npc_analysis.swap_decomp_check import swap_decomp_2qubit


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


def _count_routed_swaps(routed_qc):
    return sum(1 for instr in routed_qc.data if instr.operation.name == "swap")


def _count_routed_non_swap_2q(routed_qc):
    n = 0
    for instr in routed_qc.data:
        op = instr.operation
        if op.num_qubits == 2 and op.name != "swap":
            n += 1
    return n


def _replay_routed_layout(routed_qc, initial_layout):
    p_contains = {p: None for p in range(routed_qc.num_qubits)}
    for v, p in enumerate(initial_layout):
        p_contains[p] = v
    loc = {v: p for v, p in enumerate(initial_layout)}
    for instr in routed_qc.data:
        if instr.operation.name != "swap":
            continue
        p_a, p_b = (routed_qc.find_bit(q).index for q in instr.qubits)
        v_a, v_b = p_contains[p_a], p_contains[p_b]
        p_contains[p_a], p_contains[p_b] = v_b, v_a
        if v_a is not None:
            loc[v_a] = p_b
        if v_b is not None:
            loc[v_b] = p_a
    return loc


def test_native_2q_gate_resolves(backend):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)
    tracker, _, _ = _build_tracker(backend, qc)

    target = backend.target
    expected = [
        n for n in target.operation_names
        if target.operation_from_name(n).num_qubits == 2 and n != "swap"
    ]
    assert tracker.native_2q_name in target.operation_names
    assert tracker.native_2q_name == expected[0]
    assert len(expected) == 1, f"Heron-class backend should have one native 2q gate, found {expected}"

    decomp = swap_decomp_2qubit(backend)
    assert decomp["two_qubit_ops"].get(tracker.native_2q_name, 0) == 3, (
        f"expected SWAP→3×{tracker.native_2q_name}, got {decomp['two_qubit_ops']}"
    )


def test_no_swap_paths_are_trivial(backend):
    edge = backend.coupling_map.get_edges()[0]
    qc = QuantumCircuit(2)
    qc.cx(0, 1)

    tracker, cap, _ = _build_tracker(
        backend, qc, initial_layout=[edge[0], edge[1]]
    )

    assert _count_routed_swaps(cap.routed) == 0

    for v in range(tracker.n_logical):
        qi = tracker.quantuminfo[v]
        assert qi.path == [tracker.initial_layout[v]]
        assert qi.location == tracker.initial_layout[v]
        for op in qi.ops:
            assert not op.get("is_swap_gate", False), (
                f"trivial routing should produce no swap-tagged ops; got {op}"
            )


def test_swap_trajectory_matches_routed_permutation(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)

    tracker, cap, _ = _build_tracker(
        backend, qc, layout_method="sabre", routing_method="sabre"
    )

    n_swaps = _count_routed_swaps(cap.routed)
    n_non_swap_2q = _count_routed_non_swap_2q(cap.routed)
    print(f"\n[trajectory] routed swaps={n_swaps}, non-swap 2q={n_non_swap_2q}")

    expected_loc = _replay_routed_layout(cap.routed, tracker.initial_layout)

    for v in range(tracker.n_logical):
        qi = tracker.quantuminfo[v]
        assert qi.location == expected_loc[v], (
            f"v={v}: tracker location {qi.location} != replay {expected_loc[v]}"
        )
        assert qi.path[0] == tracker.initial_layout[v]
        assert qi.path[-1] == qi.location

    swap_tagged = 0
    real_native_2q = 0
    for v in range(tracker.n_logical):
        for op in tracker.quantuminfo[v].ops:
            if op["name"] != tracker.native_2q_name:
                continue
            if op["is_swap_gate"]:
                swap_tagged += 1
            else:
                real_native_2q += 1

    assert swap_tagged == 6 * n_swaps, (
        f"expected {6 * n_swaps} swap-tagged native 2q entries, got {swap_tagged}"
    )
    assert real_native_2q == 2 * n_non_swap_2q, (
        f"expected {2 * n_non_swap_2q} real native-2q entries, got {real_native_2q}"
    )


def test_to_dict_populates_calibrations(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)

    tracker, _, _ = _build_tracker(
        backend, qc, layout_method="sabre", routing_method="sabre"
    )
    d = tracker.to_dict()

    assert set(d.keys()) >= {
        "native_2q_gate", "initial_layout", "final_v_to_p",
        "qubits", "qubit_calibrations", "gate_calibrations",
    }

    cal_keys = set(d["qubit_calibrations"].keys())
    for v, qi_dict in d["qubits"].items():
        assert qi_dict["location"] in cal_keys, (
            f"v={v} ends at p={qi_dict['location']} but no qubit_calibration entry"
        )
    for p in d["initial_layout"]:
        assert p in cal_keys, f"initial-layout p={p} missing from qubit_calibrations"

    for p, qcal in d["qubit_calibrations"].items():
        assert {"physical_index", "t1", "t2", "readout_error"} <= set(qcal.keys())

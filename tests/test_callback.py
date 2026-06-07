import pytest
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeFez

from npc_analysis import TranspileCapture


class LoggingCapture(TranspileCapture):
    def __init__(self, input_circuit: QuantumCircuit = None):
        super().__init__(input_circuit=input_circuit)
        self.pass_names: list[str] = []

    def __call__(self, **kwargs):
        self.pass_names.append(type(kwargs["pass_"]).__name__)
        super().__call__(**kwargs)


@pytest.fixture(scope="session")
def backend():
    return FakeFez()


def _two_qubit_pairs(circuit: QuantumCircuit) -> list[tuple[int, int]]:
    pairs = []
    for instr in circuit.data:
        if instr.operation.name == "barrier":
            continue
        if len(instr.qubits) == 2:
            a = circuit.find_bit(instr.qubits[0]).index
            b = circuit.find_bit(instr.qubits[1]).index
            pairs.append((a, b))
    return pairs


def test_baseline_captures_routed_and_final(backend):
    qc = QuantumCircuit(3)
    qc.h(0)
    qc.cx(0, 1)
    qc.cx(1, 2)

    cap = LoggingCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend)
    pm.run(qc, callback=cap)

    print(f"\n[baseline] pass sequence: {cap.pass_names}")

    assert cap.routed is not None
    assert cap.final is not None
    assert cap.layout is not None
    assert "FilterOpNodes" in cap.pass_names

    idx_layout = cap.initial_index_layout()
    assert len(idx_layout) == qc.num_qubits
    assert all(isinstance(p, int) for p in idx_layout)
    assert len(set(idx_layout)) == len(idx_layout)
    assert all(0 <= p < backend.num_qubits for p in idx_layout)

    op_names = set(backend.target.operation_names)
    coupling = {tuple(e) for e in backend.coupling_map.get_edges()}
    for a, b in _two_qubit_pairs(cap.final):
        assert (a, b) in coupling or (b, a) in coupling, (
            f"final circuit has 2q gate on non-edge pair ({a},{b})"
        )
    for instr in cap.final.data:
        name = instr.operation.name
        assert name in op_names or name == "barrier", (
            f"final circuit uses non-basis op {name!r}"
        )


def test_sabre_layout_produces_nontrivial_mapping(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)

    cap = LoggingCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(
        optimization_level=0,
        backend=backend,
        layout_method="sabre",
        routing_method="sabre",
    )
    pm.run(qc, callback=cap)

    print(f"\n[sabre] pass sequence: {cap.pass_names}")

    assert cap.routed is not None
    assert cap.final is not None
    assert cap.layout is not None
    assert "FilterOpNodes" in cap.pass_names

    idx_layout = cap.initial_index_layout()
    assert len(idx_layout) == qc.num_qubits
    assert len(set(idx_layout)) == len(idx_layout)
    assert all(0 <= p < backend.num_qubits for p in idx_layout)

    coupling = {tuple(e) for e in backend.coupling_map.get_edges()}
    routed_pairs = _two_qubit_pairs(cap.routed)
    assert routed_pairs, "routed circuit has no 2q gates"
    for a, b in routed_pairs:
        assert (a, b) in coupling or (b, a) in coupling, (
            f"routed circuit has 2q gate on non-edge pair ({a},{b}) -- "
            "routing did not produce ISA-compatible layout"
        )

    original_pairs = {(0, 2), (0, 3), (0, 4), (2, 4)}
    non_edge_originals = {
        p for p in original_pairs
        if p not in coupling and (p[1], p[0]) not in coupling
    }
    assert non_edge_originals, (
        "test precondition: at least one original CX pair must not be a hardware edge"
    )


def test_already_routed_circuit_still_invokes_filteropnodes(backend):
    edge = backend.coupling_map.get_edges()[0]
    qc = QuantumCircuit(backend.num_qubits)
    qc.cx(edge[0], edge[1])

    cap = LoggingCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend)
    pm.run(qc, callback=cap)

    print(f"\n[already-routed] pass sequence: {cap.pass_names}")

    assert cap.routed is not None
    assert cap.final is not None
    assert cap.layout is not None
    assert "FilterOpNodes" in cap.pass_names

    idx_layout = cap.initial_index_layout()
    assert len(idx_layout) == qc.num_qubits


def test_initial_index_layout_requires_input_circuit(backend):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)

    cap = LoggingCapture()
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend)
    pm.run(qc, callback=cap)

    assert cap.layout is not None
    with pytest.raises(RuntimeError, match="input_circuit"):
        cap.initial_index_layout()


def test_initial_index_layout_rejects_unfiltered_ancillas(backend):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)

    cap = LoggingCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend)
    pm.run(qc, callback=cap)

    with pytest.raises(NotImplementedError):
        cap.initial_index_layout(filter_ancillas=False)

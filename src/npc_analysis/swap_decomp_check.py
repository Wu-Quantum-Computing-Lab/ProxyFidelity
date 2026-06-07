from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager


def swap_decomp_2qubit(backend):
    '''
    Assuming a homogeneous backend (same native 2q gate on every edge),
    transpile a lone SWAP and return the resulting op counts.
    '''
    target = backend.target
    native_2q_gates = [
        n for n in target.operation_names
        if target.operation_from_name(n).num_qubits == 2 and n != "swap"
    ]

    qc = QuantumCircuit(2)
    qc.swap(0, 1)

    pm = generate_preset_pass_manager(optimization_level=0, backend=backend)
    dswap = pm.run(qc)
    ops = dswap.count_ops()

    return {
        "native_2q_gates": native_2q_gates,
        "all_ops": dict(ops),
        "two_qubit_ops": {g: c for g, c in ops.items() if g in native_2q_gates},
        "depth": dswap.depth(),
        "circuit": dswap,
    }

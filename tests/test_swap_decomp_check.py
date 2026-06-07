import pytest
from qiskit import QuantumCircuit
from qiskit_ibm_runtime.fake_provider import FakeFez

from npc_analysis.swap_decomp_check import swap_decomp_2qubit


@pytest.fixture(scope="session")
def backend():
    return FakeFez()


def test_real_backend_smoke(backend):
    result = swap_decomp_2qubit(backend)

    print(
        f"\n[{backend.name}] native_2q={result['native_2q_gates']} "
        f"all_ops={result['all_ops']} depth={result['depth']}"
    )

    assert isinstance(result["native_2q_gates"], list) and result["native_2q_gates"]
    assert isinstance(result["all_ops"], dict) and result["all_ops"]
    assert isinstance(result["two_qubit_ops"], dict) and result["two_qubit_ops"]
    assert all(g in result["native_2q_gates"] for g in result["two_qubit_ops"])
    assert isinstance(result["depth"], int) and result["depth"] > 0
    assert isinstance(result["circuit"], QuantumCircuit)

import json
import pytest
from pathlib import Path
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeFez, FakeTorino
from qiskit.circuit.library import QFT

from npc_analysis import TranspileCapture, NPCAnalyzer, NPCResult, analyze_circuit
from npc_analysis.swap_tracker import SwapTracker


@pytest.fixture(scope="session")
def backend():
    return FakeFez()

@pytest.fixture(scope="session")
def torino_backend():
    return FakeTorino()


def _run_analyzer(backend, qc, output_path=None, **pm_kwargs):
    cap = TranspileCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(optimization_level=0, backend=backend, **pm_kwargs)
    pm.run(qc, callback=cap)
    analyzer = NPCAnalyzer(backend, cap, output_path=output_path)
    return analyzer.analyze()

def test_tracker_matches_qiskit_layout_clean(torino_backend):
    qc = QuantumCircuit(8)
    qc.h(0)
    for target in range(1, 8):
        qc.cx(0, target)

    cap = TranspileCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(
        optimization_level=0, backend=torino_backend, seed_transpiler=0
    )
    out = pm.run(qc, callback=cap)

    tracker = SwapTracker(
        routed_qc=cap.routed,
        final_qc=cap.final,
        backend=torino_backend,
        capture=cap,
    ).track()

    assert [tracker.quantuminfo[v].location for v in range(tracker.n_logical)] == list(
        out.layout.final_index_layout(filter_ancillas=True)
    )


def test_tripwire_raises_on_layout_mismatch(torino_backend):
    qc = QFT(7).decompose()

    with pytest.raises(AssertionError, match="SwapTracker layout mismatch"):
        analyze_circuit(torino_backend, qc, optimization_level=0)


def test_tripwire_actually_executes(torino_backend, monkeypatch):
    qc = QuantumCircuit(3)
    qc.cx(0, 1)
    qc.cx(1, 2)

    cap = TranspileCapture(input_circuit=qc)
    pm = generate_preset_pass_manager(
        optimization_level=0, backend=torino_backend, seed_transpiler=0
    )
    pm.run(qc, callback=cap)

    original_track = SwapTracker.track

    def corrupt_track(self):
        tracker = original_track(self)
        bad_location = (tracker.quantuminfo[0].location + 1) % tracker.n_total
        tracker.quantuminfo[0].location = bad_location
        tracker.quantuminfo[0].path[-1] = bad_location
        return tracker

    monkeypatch.setattr(SwapTracker, "track", corrupt_track)

    with pytest.raises(AssertionError, match="SwapTracker layout mismatch"):
        NPCAnalyzer(torino_backend, cap).analyze()


def test_analyze_returns_npc_result(backend):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)
    qc.measure_all()
    result = _run_analyzer(backend, qc)

    assert isinstance(result, NPCResult)
    assert 0.0 < result.circuit_fidelity <= 1.0
    assert len(result.per_qubit) > 0
    assert len(result.initial_layout) > 0


def test_per_qubit_has_trace_events(backend):
    qc = QuantumCircuit(3)
    qc.cx(0, 1)
    qc.cx(1, 2)
    qc.measure_all()
    result = _run_analyzer(backend, qc)

    has_events = False
    for v, qfr in result.per_qubit.items():
        if len(qfr.events) > 0:
            has_events = True
            break
    assert has_events, "trace is always on — at least one qubit should have events"


def test_to_dict_structure(backend):
    qc = QuantumCircuit(3)
    qc.cx(0, 1)
    qc.cx(1, 2)
    qc.measure_all()
    result = _run_analyzer(backend, qc)
    d = result.to_dict()

    expected_keys = {
        "circuit_fidelity", "native_2q_gate", "initial_layout",
        "final_layout", "per_qubit", "qubit_calibrations", "gate_calibrations",
    }
    assert expected_keys <= set(d.keys())

    for v_str, qubit_data in d["per_qubit"].items():
        assert "fidelity" in qubit_data
        assert "path" in qubit_data
        assert "events" in qubit_data
        assert isinstance(qubit_data["events"], list)


def test_to_json_is_valid(backend):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)
    qc.measure_all()
    result = _run_analyzer(backend, qc)
    j = result.to_json()
    parsed = json.loads(j)
    assert parsed["circuit_fidelity"] == result.circuit_fidelity


def test_writes_json_file(backend, tmp_path):
    qc = QuantumCircuit(2)
    qc.cx(0, 1)
    qc.measure_all()
    out = tmp_path / "result.json"
    result = _run_analyzer(backend, qc, output_path=out)

    assert out.exists()
    parsed = json.loads(out.read_text())
    assert parsed["circuit_fidelity"] == result.circuit_fidelity
    assert "per_qubit" in parsed


def test_events_have_fidelity_progression(backend):
    qc = QuantumCircuit(5)
    qc.cx(0, 2)
    qc.cx(0, 3)
    qc.cx(0, 4)
    qc.cx(2, 4)
    qc.measure_all()
    result = _run_analyzer(
        backend, qc, layout_method="sabre", routing_method="sabre"
    )
    d = result.to_dict()

    for v_str, qubit_data in d["per_qubit"].items():
        for event in qubit_data["events"]:
            assert "f_before" in event, f"event missing f_before: {event}"
            assert "f_after" in event, f"event missing f_after: {event}"
            assert event["f_after"] <= event["f_before"], (
                f"fidelity should not increase: {event}"
            )


def test_capture_validation():
    cap = TranspileCapture()
    with pytest.raises(ValueError, match="capture.routed is None"):
        NPCAnalyzer(None, cap)

    cap.routed = QuantumCircuit(1)
    with pytest.raises(ValueError, match="capture.final is None"):
        NPCAnalyzer(None, cap)

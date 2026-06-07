"""NPC proxy fidelity analyzer.

Predicts circuit fidelity on IBM hardware by walking a transpiled circuit
gate-by-gate on top of the device calibration data, accumulating per-event
fidelity drops (depolarizing, thermal relaxation, readout, SWAP).

Quick start::

    from qiskit_ibm_runtime import QiskitRuntimeService
    from npc_analysis import analyze_circuit

    backend = QiskitRuntimeService().backend("ibm_kingston")
    result = analyze_circuit(backend, my_circuit, output_path="result.json")
    print(result.circuit_fidelity)

For more control over transpilation, build a ``TranspileCapture`` yourself
and pass it to :class:`NPCAnalyzer`.
"""

from .npc_analyzer import NPCAnalyzer, NPCResult, analyze_circuit
from .callback import TranspileCapture
from .fidelity_walker import (
    FidelityWalker,
    CircuitFidelityResult,
    QubitFidelityResult,
)
from .swap_tracker import SwapTracker
from .noise_events import (
    DepolarizingEvent,
    ThermalRelaxationEvent,
    ReadoutEvent,
    SwapNoiseEvent,
)

__all__ = [
    "NPCAnalyzer",
    "NPCResult",
    "analyze_circuit",
    "TranspileCapture",
    "FidelityWalker",
    "CircuitFidelityResult",
    "QubitFidelityResult",
    "SwapTracker",
    "DepolarizingEvent",
    "ThermalRelaxationEvent",
    "ReadoutEvent",
    "SwapNoiseEvent",
]

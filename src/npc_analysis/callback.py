from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy

from qiskit import QuantumCircuit
from qiskit.transpiler import Layout, PropertySet, TranspileLayout
from qiskit.transpiler.passes import FilterOpNodes
from qiskit.converters import dag_to_circuit


@dataclass
class TranspileCapture:
    """Captures pass-manager artifacts needed by the NPC tracker.

    `dag_to_circuit` does not attach a `TranspileLayout` to the circuits built
    in this callback. We therefore record the transpiler property-set layouts
    separately, including the authoritative `TranspileLayout` used for final
    logical→physical layout checks.
    """

    input_circuit: QuantumCircuit | None = None
    routed: QuantumCircuit | None = None
    final: QuantumCircuit | None = None
    layout: Layout | None = None
    transpile_layout: TranspileLayout | None = None

    def __call__(self, **kwargs):
        pass_ = kwargs["pass_"]
        dag = kwargs["dag"]
        property_set = kwargs.get("property_set")
        if property_set is None:
            property_set = PropertySet()
        elif not isinstance(property_set, PropertySet):
            property_set = PropertySet(property_set)

        if isinstance(pass_, FilterOpNodes):
            self.routed = dag_to_circuit(deepcopy(dag))

        self.final = dag_to_circuit(dag)

        ps_layout = property_set.get("layout")
        if ps_layout is not None:
            self.layout = ps_layout


        transpile_layout = TranspileLayout.from_property_set(dag, property_set)
        if transpile_layout is not None:
            self.transpile_layout = transpile_layout

    def initial_index_layout(self, filter_ancillas: bool = True) -> list[int]:
        """Virtual→physical array for the input qubits.

        Mirrors `TranspileLayout.initial_index_layout(filter_ancillas=True)`:
        returns `[p_for_v0, p_for_v1, ...]` for the user's original qubits.
        """
        if self.layout is None:
            raise RuntimeError("No layout captured — did the layout pass run?")
        if self.input_circuit is None:
            raise RuntimeError("TranspileCapture.input_circuit must be set")
        if not filter_ancillas:
            raise NotImplementedError("Only filter_ancillas=True is supported")

        virtual_map = self.layout.get_virtual_bits()
        input_qubits = list(self.input_circuit.qubits)
        input_pos = {q: i for i, q in enumerate(input_qubits)}
        output: list[int | None] = [None] * len(input_qubits)
        for vq, p in virtual_map.items():
            if vq in input_pos:
                output[input_pos[vq]] = p
        if any(x is None for x in output):
            missing = [i for i, x in enumerate(output) if x is None]
            raise RuntimeError(f"Input qubits missing from captured layout: {missing}")
        return output

    def final_index_layout(self, filter_ancillas: bool = True) -> list[int]:
        """Final logical→physical array from Qiskit's `TranspileLayout`."""
        if self.transpile_layout is None:
            raise RuntimeError(
                "No TranspileLayout captured — did the pass manager finish?"
            )
        return list(
            self.transpile_layout.final_index_layout(
                filter_ancillas=filter_ancillas
            )
        )

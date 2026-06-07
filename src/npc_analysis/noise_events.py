from dataclasses import dataclass


@dataclass
class DepolarizingEvent:
    gate_name: str
    qubits: tuple
    p: float
    f_before: float
    f_after: float


@dataclass
class ThermalRelaxationEvent:
    gate_name: str
    qubits: tuple
    duration: float
    t1: float
    t2: float
    physical_qubit: int
    f_before: float
    f_after: float


@dataclass
class ReadoutEvent:
    physical_qubit: int
    error: float
    f_before: float
    f_after: float


@dataclass
class SwapNoiseEvent:
    physical_qubits: tuple[int, int]
    native_ops: list[dict]
    f_before: float
    f_phys_i: float
    f_phys_j: float
    f_after: float
    swap_segment_index: int
    trigger_step: int

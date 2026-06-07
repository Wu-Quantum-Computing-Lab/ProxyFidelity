from dataclasses import dataclass


@dataclass
class GateCalibration:
    name: str
    qubits: tuple
    error: float | None
    duration: float | None
    is_virtual: bool

    @property
    def depolarizing_probability(self) -> float:
        """Convert raw error rate to depolarizing probability.
        Paper Section 4: p = r * d / (d - 1), where d = 2^n_qubits.
        Always 0.0 for virtual gates."""

        if self.is_virtual:
            return 0.0
        if self.error is None:
            return 0.0
        d = 2 ** len(self.qubits)
        return (self.error * d / (d - 1))

    @classmethod
    def from_backend(cls, backend, name: str, qubits: tuple) -> "GateCalibration":
        qubits = tuple(qubits)
        props = backend.target[name].get(qubits)
        if props is None:
            return cls(name=name, qubits=qubits, error=None, duration=None, is_virtual=True)

        duration = props.duration
        error = props.error
        is_virtual = duration is None or duration == 0.0
        return cls(
            name=name,
            qubits=qubits,
            error=error,
            duration=duration,
            is_virtual=is_virtual,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "qubits": list(self.qubits),
            "error": self.error,
            "duration": self.duration,
            "is_virtual": self.is_virtual,
            "depolarizing_probability": 
            self.depolarizing_probability
        }

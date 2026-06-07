from dataclasses import dataclass


@dataclass
class QubitCalibration:
    physical_index: int
    t1: float | None
    t2: float | None
    readout_error: float | None

    @classmethod
    def from_backend(cls, backend, physical_index: int) -> "QubitCalibration":
        target = backend.target

        t1 = t2 = None
        qprops = target.qubit_properties
        if qprops is not None and physical_index < len(qprops):
            qp = qprops[physical_index]
            t1 = getattr(qp, "t1", None)
            t2 = getattr(qp, "t2", None)

        readout_error = None
        if "measure" in target.operation_names:
            props = target["measure"].get((physical_index,))
            if props is not None:
                readout_error = props.error

        return cls(
            physical_index=physical_index,
            t1=t1,
            t2=t2,
            readout_error=readout_error,
        )

    def to_dict(self) -> dict:
        return {
            "physical_index": self.physical_index,
            "t1": self.t1,
            "t2": self.t2,
            "readout_error": self.readout_error,
        }

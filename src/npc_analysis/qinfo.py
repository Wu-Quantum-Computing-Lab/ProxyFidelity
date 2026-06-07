from dataclasses import dataclass, field


@dataclass
class Qinfo:
    logic_ind: int
    location: int
    path: list = field(default_factory=list)
    ops: list = field(default_factory=list)
    waiting_2q_ops: list = field(default_factory=list)
    pending_2q_count: int = 0
    measured: bool = False
    meas_error: float | None = None
    swap_segments: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "logic_ind": self.logic_ind,
            "location": self.location,
            "path": list(self.path),
            "ops": [dict(o) for o in self.ops],
            "waiting_2q_ops": [
                [entry[0], list(entry[1])] for entry in self.waiting_2q_ops
            ],
            "pending_2q_count": self.pending_2q_count,
            "measured": self.measured,
            "meas_error": self.meas_error,
            "swap_segments": [dict(s) for s in self.swap_segments],
        }

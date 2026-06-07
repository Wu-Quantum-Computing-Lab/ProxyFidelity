# Noise Models

NPCProxyFidelity applies four noise channels as it walks each qubit through the circuit: depolarizing, thermal relaxation, SWAP (mini-NPC), and readout. The math lives as static methods on `FidelityWalker`, so you can call them directly.

## Depolarizing

Applied after every non-virtual gate with non-zero depolarizing probability.

$$f' = \frac{1}{2} + \left(f - \frac{1}{2}\right)(1 - p)$$

where $p$ is derived from the backend-reported gate error rate $r$:

$$p = r \times \frac{d}{d-1}, \quad d = 2^{n_{qubits}}$$

| Gate type | $d$ | Example | $r = 0.001 \Rightarrow p$ |
|-----------|:---:|---------|---|
| 1-qubit | 2 | `sx` | $0.002$ |
| 2-qubit | 4 | `cz` | $0.00133$ |

**Intuition:** depolarizing noise pushes the state toward the maximally mixed state ($f = 0.5$); higher $p$ pushes faster.

```python
FidelityWalker.apply_depolarizing(f, p)
```

## Thermal Relaxation

Applied after every gate with non-zero duration on a qubit that has both T1 and T2 known.

$$f' = \frac{1}{2} + \left(f - \frac{1}{2}\right)\left(\frac{2}{3}e^{-t/T_2} + \frac{1}{3}e^{-t/T_1}\right)$$

where:

- $t$ — gate duration in seconds (`GateCalibration.duration`)
- $T_1$ — energy relaxation time
- $T_2$ — dephasing time

**Intuition:** while the qubit waits for a gate to physically execute, it loses coherence. Longer gates and shorter T1/T2 mean more fidelity loss. Virtual gates (e.g. `rz`) have zero duration and skip this channel entirely.

```python
FidelityWalker.apply_thermal_relaxation(f, t, t1, t2)
```

If either `t1` or `t2` is `None` (rare, but possible when a backend reports incomplete calibration), the walker skips thermal relaxation for that gate.

## SWAP (Mini-NPC)

SWAPs are not treated as a single gate — they're decomposed into their full native gate sequence (typically 3 native 2q gates + interleaving single-qubit gates). When the walker reaches the **last op of a swap segment** (the highest `step_index` among the segment's tagged ops, normally the 3rd native 2q gate), it runs a "mini-NPC":

1. Walk **physical qubit i** through the full SWAP segment (every 1q + 2q op touching qubit $i$), applying depolarizing + thermal relaxation per gate.
2. Walk **physical qubit j** independently through the same segment.
3. Combine:

$$f_{after} = \frac{f_{phys_i} + f_{phys_j}}{2}$$

**Why average?** After a SWAP, the logical qubit's state has been physically moved. Both physical qubits' noise histories contributed during the SWAP, so averaging reflects the genuine uncertainty about which physical-qubit noise the logical qubit inherits.

The full segment (including 1q gates) is stored on the `Qinfo.swap_segments` list so the walker has everything it needs without re-deriving anything from the circuit.

## Readout

Applied once per qubit at measurement.

$$f' = f \times (1 - e)$$

where $e$ is the readout error from `QubitCalibration.readout_error` (queried at the qubit's **final** physical location, not its initial one).

**Intuition:** even if the qubit state is perfect, the classical readout hardware can misidentify $|0\rangle$ as $|1\rangle$ or vice versa.

```python
FidelityWalker.apply_readout_error(f, e)
```

## Circuit Fidelity

The overall circuit fidelity is the product of all per-qubit fidelities:

$$F_{circuit} = \prod_{v=0}^{n-1} f_v$$

This assumes independent per-qubit noise — a standard approximation in the NPC model and adequate as a fast proxy for whole-circuit success.

## Virtual Gates

On IBM backends, `rz` and certain other single-qubit gates are **virtual** — frame changes in the control software, not physical microwave pulses. The proxy detects this from `backend.target`:

- `duration is None` or `duration == 0.0` → marked virtual
- Virtual gates report `depolarizing_probability = 0.0`
- The walker skips them entirely (no events emitted)

Virtual gates *can* still appear in a SWAP segment (e.g. `rz` between the 3 native 2q gates). When that happens they're still skipped inside `_run_swap_mini_npc` because `_apply_gate_noise` short-circuits on `p == 0` and `duration <= 0`.

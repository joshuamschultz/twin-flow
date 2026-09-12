# RM-10 benchmark evidence

Measured 2026-09-12 with the documented command against `examples/active-control`:

- Environment: Python 3.13.13, macOS 26.6.2 arm64, 18 logical CPUs.
- Conditions: 3 sequential repetitions, seed 42; limits of 1,000,000 simulated seconds,
  100,000 events, and 30 wall seconds per repetition.
- Observed: 0.1252 wall seconds, 765 total events, 9,146.48 total simulated seconds,
  and 52,476 artifact bytes. All three runs terminated with `demand_completed`.
- Cost proxies: 0.0417 wall seconds per repetition, 6,112 events per wall second, and
  68.60 artifact bytes per event.

These figures are one local measurement. They are not throughput guarantees or evidence
of enterprise scale, concurrent tenancy, production cost, or customer workload fitness.

"""The plant-state layer: one record shape for every engine (TF-006).

`twinflow.state` imports no other twinflow package (D-131); it uses only the standard
library, numpy and polars (D-140). Engines, policy files, Markov maps and agents all read
the same `Snapshot` / `Move` / `Ledger` records (D-130).
"""

"""Operation-level DES driver: plans and dispatch baselines run on the v2 shop (D-085).

`twinflow.execution` builds a shop model from a compiled v2 problem, executes a verified
plan (or the same problem under a dispatch rule) in SimPy with keyed common random
numbers, and evaluates candidates. Its rules are the verifier's rules; it does no
scheduling math of its own and no file or network I/O.
"""

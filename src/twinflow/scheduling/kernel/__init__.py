"""Shared scheduling kernel: index, statistics, decoder, graph, moves and scoring (COMP-003..007).

Import rule (SDD Architecture, boundary test `test_kernel_imports_only_core_modules_*`): a
kernel module imports only `twinflow.scheduling.{model, setups, calendar_math, timeline,
objectives, seam}`, other kernel modules, the standard library and numpy; never a solver,
the baseline, the verifier, the registry or an algorithm leaf. Callers import the submodule
they need (`from twinflow.scheduling.kernel.index import ProblemIndex`); this file imports
none of them, so importing the package costs nothing.
"""

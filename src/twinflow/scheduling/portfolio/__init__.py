"""Portfolio layer: load-dependent weights, Pareto menu and Pareto search (COMP-021..COMP-024).

Import rule (SDD Architecture): a portfolio module imports the registry, the kernel and the
core modules, never an algorithm module (solvers are looked up by registry name at call
time). Callers import the submodule they need (`from twinflow.scheduling.portfolio.pareto
import menu`); this file imports none of them.
"""

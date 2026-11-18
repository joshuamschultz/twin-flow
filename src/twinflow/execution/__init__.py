"""Operation-level DES driver: plans and dispatch baselines run on the v2 shop (D-085).

`twinflow.execution` builds a shop model from a compiled v2 problem, executes a verified
plan (or the same problem under a dispatch rule) in SimPy with keyed common random
numbers, and evaluates candidates. Its rules are the verifier's rules; it does no
scheduling math of its own and no file or network I/O.

This facade is the application layer's entry point (SDD "facade: build_shop, execute,
evaluate_candidates, SimulationPolicy, sources"); everything else stays module-qualified.
"""

from twinflow.execution.engine import (
    DispatchControl,
    ExecutionRun,
    Limits,
    PlanControl,
    RepDraws,
    execute,
)
from twinflow.execution.evaluate import (
    BudgetExhausted,
    CandidateEvaluation,
    CandidateInput,
    EvaluationResult,
    PairedDelta,
    Stats,
    evaluate_candidates,
)
from twinflow.execution.policy import SimulationPolicy, policy_digest
from twinflow.execution.shop import ShopModel, build_shop, censor_time
from twinflow.execution.sources import (
    CapsuleSource,
    DeterministicSource,
    DistributionSource,
    ProfileSource,
    resolve,
)

__all__ = [
    "BudgetExhausted",
    "CandidateEvaluation",
    "CandidateInput",
    "CapsuleSource",
    "DeterministicSource",
    "DispatchControl",
    "DistributionSource",
    "EvaluationResult",
    "ExecutionRun",
    "Limits",
    "PairedDelta",
    "PlanControl",
    "ProfileSource",
    "RepDraws",
    "ShopModel",
    "SimulationPolicy",
    "Stats",
    "build_shop",
    "censor_time",
    "evaluate_candidates",
    "execute",
    "policy_digest",
    "resolve",
]

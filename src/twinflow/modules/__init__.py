"""twinflow.modules — the unified module surface.

Four kinds of pluggable module attach to one scoring seam:

    ScoringSurface(model, plan) -- Scenario -> Evaluation (KPIs + confidence band)
        |                                    |                     |
    Optimizer  ------ ranks -----------> Objective  <-- money --  CostFunction
        |                                                          |
    SurrogateModel (learns the surface to screen scenarios cheaply)

Each kind lives in its own registry (`OBJECTIVES`, `COSTS`, `OPTIMIZERS`, `MODELS`);
adding one is a `register(name, factory)` call, never a core edit — the module-system
analogue of the compiler's single growth point. The `optimize` facade wires a chosen
optimizer + objective over a space and returns an `OptimizationResult`; the twin
scores, the module proposes, the human decides.
"""

from __future__ import annotations

from twinflow.modules.calibrate import (
    CalibrationResult,
    CalibrationTarget,
    calibrate,
    calibration_objective,
    distance_to_observed,
)
from twinflow.modules.costs import (
    COSTS,
    CapacityCost,
    CostFunction,
    InventoryHoldingCost,
    LaborCost,
    LatenessPenalty,
    StockoutPenalty,
    TotalCost,
)
from twinflow.modules.models import (
    MODELS,
    LinearSurrogate,
    NearestNeighborSurrogate,
    SurrogateModel,
    predicted_ranking,
)
from twinflow.modules.objectives import (
    OBJECTIVES,
    CostObjective,
    MetricObjective,
    Objective,
    WeightedObjective,
)
from twinflow.modules.optimizers import (
    OPTIMIZERS,
    OptimizationResult,
    Optimizer,
)
from twinflow.modules.registry import Registry
from twinflow.modules.space import Choice, Domain, IntRange, LeverSpace
from twinflow.modules.surface import Evaluation, Scenario, ScoringSurface
from twinflow.plan.loader import WorkOrder

__all__ = [
    "COSTS",
    "MODELS",
    "OBJECTIVES",
    "OPTIMIZERS",
    "CalibrationResult",
    "CalibrationTarget",
    "CapacityCost",
    "Choice",
    "CostFunction",
    "CostObjective",
    "Domain",
    "calibrate",
    "calibration_objective",
    "distance_to_observed",
    "Evaluation",
    "IntRange",
    "InventoryHoldingCost",
    "LaborCost",
    "LatenessPenalty",
    "LeverSpace",
    "StockoutPenalty",
    "LinearSurrogate",
    "MetricObjective",
    "NearestNeighborSurrogate",
    "Objective",
    "OptimizationResult",
    "Optimizer",
    "Registry",
    "Scenario",
    "ScoringSurface",
    "SurrogateModel",
    "TotalCost",
    "WeightedObjective",
    "optimize",
    "predicted_ranking",
]


def optimize(
    model_path: str,
    plan: list[WorkOrder],
    space: LeverSpace,
    objective: Objective,
    optimizer: Optimizer,
    budget: int,
    reps: int = 20,
    base_seed: int = 0,
    seed: int = 0,
) -> OptimizationResult:
    """Run one optimization study end to end.

    Builds a `ScoringSurface` over `model_path` + `plan` (every scenario scored on
    the same `base_seed`, so CRN pairing holds), then hands it to `optimizer` under
    `objective` for at most `budget` real evaluations. `seed` seeds the optimizer's
    own randomness (sampling, mutation), separate from the simulator's `base_seed`.
    """
    surface = ScoringSurface(model_path, plan, reps=reps, base_seed=base_seed)
    return optimizer.optimize(surface, space, objective, budget=budget, seed=seed)

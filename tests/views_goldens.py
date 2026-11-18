"""Write the browser-test fixtures for the states, improve and project views (TF-006 B-28 to B-30).

    uv run python tests/views_goldens.py

After running, run `npx prettier --write tests/browser/views` in web/.
Each file is a real view model built from the example projects, so the Playwright tests and
the screenshots show true data. Re-run when a view model changes on purpose and review the diff.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "tests" / "browser" / "views" / "fixtures"
PRESS = ROOT / "examples" / "molding-press" / "molding-press.twin.yaml"
SHOP = ROOT / "examples" / "cnc-shop" / "cnc-shop.twin.yaml"


def write(name: str, model: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    model["generated_at"] = "2026-11-18T09:00:00+00:00"
    (OUT / name).write_text(json.dumps(model, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT / name}")


def states() -> None:
    from twinflow.application.scenarios import evaluate_capsule
    from twinflow.cli.main import main
    from twinflow.extensions.states_model import states_view_model
    from twinflow.scenario import load

    with tempfile.TemporaryDirectory() as scratch:
        run = Path(scratch) / "molding-press"
        main(["scenario", "run", str(PRESS), "--reps", "5", "--seed", "3", "--out", str(run)])
        write("states.family.json", states_view_model(run, "family", 15, 0.05))
        shop = Path(scratch) / "cnc-shop"
        data = copy.deepcopy(load(SHOP).to_dict())
        data["experiment"]["state_capture"] = {"every_hours": 8}
        evaluate_capsule(data, shop, 2, 0, lambda: False)
        write("states.grid.json", states_view_model(shop, "family", 12, 0.05))
        empty = Path(scratch) / "empty-run"
        empty.mkdir()
        write("states.empty.json", states_view_model(empty, "family", 15, 0.05))


def improve() -> None:
    sys.path.insert(0, str(ROOT / "tests" / "cli"))
    import test_improve_view_model as t

    from twinflow.cli.improve import paired, passes
    from twinflow.extensions.improve_model import improve_view_model
    from twinflow.extensions.project import Project, RaceSettings
    from twinflow.extensions.race import race

    result = race(
        Project.open(ROOT / "examples" / "molding-press"),
        ["family_campaign", "lowest_stock_first"],
        settings=RaceSettings(episodes=24, seed=4, tune_episodes=6),
    )
    first, second = result.ranking()[0], result.ranking()[1]
    score = paired(first.episode_score, second.episode_score, 0.95)
    won = t._proof(result, first.policy, score, passes(score, "interval_above_zero"))
    write("improve.won.json", improve_view_model(won, result))
    # a real close call: five test runs of every built-in rule, first against second
    many = race(
        Project.open(ROOT / "examples" / "molding-press"),
        settings=RaceSettings(episodes=5, seed=2, tune_episodes=2),
    )
    top, next_ = many.ranking()[0], many.ranking()[1]
    near = paired(top.episode_score, next_.episode_score, 0.95)
    close = t._proof(many, top.policy, near, passes(near, "interval_above_zero"))
    write("improve.close.json", improve_view_model(close, many))
    behind = paired(second.episode_score, first.episode_score, 0.95)
    lost = t._proof(result, second.policy, behind, False)
    write("improve.lost.json", improve_view_model(lost, result))


if __name__ == "__main__":
    which = sys.argv[1:] or ["states"]
    for name in which:
        globals()[name]()

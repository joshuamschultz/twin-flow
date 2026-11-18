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


if __name__ == "__main__":
    which = sys.argv[1:] or ["states"]
    for name in which:
        globals()[name]()

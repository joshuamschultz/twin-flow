"""COMP-163: a twin project, its capsule settings and the extensions it picks (REQ-217,
REQ-220, REQ-221).

A project folder holds one `.twin.yaml` capsule plus optional extension folders
(`policies/`, `rewards/`, `views/`, `features/`, `demand/`, `observations/`, `engines/`).
`Project.open(path)` reads the capsule, the model, the `experiment` settings (every one
has a default in the `DEFAULTS` table, which the docs print) and the extensions.

The race and the reports use the project to:

- build demand draws once per seed with the chosen demand file (D-153),
- add feature values to every policy state (REQ-219),
- hide what the chosen observation hides (REQ-226),
- call `decide`, and
- score each move with the chosen reward while the dollar ledger stays on the move (D-152).

Only the CLI and in-process Python may open a project: it loads local code (D-139).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from twinflow.engines.step.engine import DemandDraws, StepEngine
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.extensions.loader import Extension, Extensions, Problem, load_project
from twinflow.extensions.view import ModelFacts
from twinflow.scenario import ScenarioCapsule
from twinflow.state.issues import did_you_mean
from twinflow.state.records import Move

CAPSULE_SUFFIX = ".twin.yaml"
Check = Literal["name", "names", "whole", "number", "share", "choice"]


@dataclass(frozen=True)
class Setting:
    """One capsule setting with its default (REQ-221)."""

    key: str
    default: Any
    meaning: str
    check: Check
    choices: tuple[str, ...] = ()
    minimum: float = 0.0


DEFAULTS: tuple[Setting, ...] = (
    Setting(
        "experiment.score",
        "dollars",
        "The reward file the race ranks policies by. Dollars are always shown next to it.",
        "name",
    ),
)

# Experiment keys read elsewhere (the capsule, the adapter, the DES capture).
OTHER_EXPERIMENT_KEYS = ("id", "replications", "seed", "policy", "state_capture")


class ProjectError(ValueError):
    """The project cannot be opened. `problems` says why, in plain words."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(str(p) for p in self.problems))


@dataclass(frozen=True)
class ProjectConfig:
    """The `experiment` settings, one value per `DEFAULTS` key."""

    values: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def score(self) -> str:
        return str(self.values["experiment.score"])


def _config_problem(file: Path, key: str, message: str, fix: str) -> Problem:
    return Problem("config", "bad_setting", message, fix, file, field=key)


def _lookup(experiment: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    node: Any = experiment
    for part in key.split(".")[1:]:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _checked(setting: Setting, value: Any, file: Path) -> tuple[Any, Problem | None]:
    """The value, or the default and a problem when it does not fit the setting."""
    key, check = setting.key, setting.check

    def bad(message: str, fix: str) -> tuple[Any, Problem]:
        return setting.default, _config_problem(file, key, message, fix)

    if check == "name":
        if isinstance(value, str) and value.strip():
            return value.strip(), None
        return bad(f"{key} is {value!r}; it must be a name.", f"Write {key}: {setting.default}.")
    if check == "names":
        items = [value] if isinstance(value, str) else value
        if isinstance(items, list) and all(isinstance(v, str) and v for v in items):
            return tuple(items), None
        return bad(f"{key} is {value!r}; it must be a list of names.", f"Write {key}: [a, b].")
    if check == "choice":
        if value in setting.choices:
            return value, None
        return bad(f"{key} is {value!r}.", did_you_mean(str(value), setting.choices))
    whole = check == "whole"
    number = isinstance(value, int | float) and not isinstance(value, bool)
    if not number or not math.isfinite(value) or (whole and not float(value).is_integer()):
        kind = "a whole number" if whole else "a number"
        return bad(f"{key} is {value!r}; it must be {kind}.", f"Write {key}: {setting.default}.")
    if check == "share" and not 0.0 < value < 1.0:
        return bad(f"{key} is {value}; it must be between 0 and 1.", "Write a share, like 0.95.")
    if value < setting.minimum:
        return bad(f"{key} is {value}; it must be at least {setting.minimum:g}.", "Raise it.")
    return (int(value) if whole else float(value)), None


def read_config(experiment: Mapping[str, Any], file: Path) -> tuple[ProjectConfig, list[Problem]]:
    """Every `DEFAULTS` key, from the capsule or its default; bad values are problems."""
    values: dict[str, Any] = {}
    problems: list[Problem] = []
    for setting in DEFAULTS:
        present, raw = _lookup(experiment, setting.key)
        if not present:
            values[setting.key] = setting.default
            continue
        value, problem = _checked(setting, raw, file)
        values[setting.key] = value
        if problem is not None:
            problems.append(problem)
    return ProjectConfig(MappingProxyType(values)), problems


def _capsule_file(path: Path) -> Path | Problem:
    if path.is_file():
        return path
    found = sorted(path.glob(f"*{CAPSULE_SUFFIX}")) if path.is_dir() else []
    if len(found) == 1:
        return found[0]
    if not found:
        return Problem(
            "project",
            "no_capsule",
            f"No capsule was found in {path}.",
            f"Put one {CAPSULE_SUFFIX} file in the folder, or name the capsule file.",
            path,
        )
    names = ", ".join(p.name for p in found)
    return Problem(
        "project",
        "many_capsules",
        f"The folder {path} holds more than one capsule: {names}.",
        "Name the capsule file you mean.",
        path,
    )


def _load_capsule(file: Path) -> ScenarioCapsule:
    try:
        return ScenarioCapsule.load(file)
    except (OSError, ValueError) as exc:
        raise ProjectError(
            [
                Problem(
                    "project",
                    "bad_capsule",
                    f"The capsule could not be read: {exc}",
                    "Run `twinflow scenario validate` on it and fix what it names.",
                    file,
                )
            ]
        ) from exc


def _model(capsule: ScenarioCapsule, file: Path) -> tuple[StepModel, list[Problem]]:
    domain = capsule.model.get("domain")
    if domain != "make_to_stock":
        raise ProjectError(
            [
                Problem(
                    "model",
                    "bad_domain",
                    f"The model domain is {domain!r}; extensions run make_to_stock twins.",
                    "Write model.domain: make_to_stock.",
                    file,
                    field="model.domain",
                )
            ]
        )
    model, issues = parse_step_model(capsule.model)
    problems = [
        Problem(
            "model",
            "bad_model" if i.severity == "error" else "note",
            i.message,
            i.suggestion or "See the make-to-stock how-to.",
            file,
            field=i.path,
            severity=i.severity,
        )
        for i in issues
    ]
    if model is None:
        raise ProjectError([p for p in problems if p.severity == "error"])
    return model, problems


@dataclass(frozen=True)
class Project:
    """An opened twin project (COMP-163)."""

    folder: Path
    capsule_file: Path
    capsule: ScenarioCapsule
    config: ProjectConfig
    extensions: Extensions
    model: StepModel
    problems: tuple[Problem, ...] = ()
    notes: tuple[Problem, ...] = ()
    facts: ModelFacts = field(default_factory=ModelFacts)

    @classmethod
    def open(cls, path: Path | str) -> Project:
        """Open a project folder (or one capsule file in it). Raises `ProjectError` when the
        capsule or model cannot be used; setting problems are kept in `problems`."""
        where = Path(path).resolve()
        file = _capsule_file(where)
        if isinstance(file, Problem):
            raise ProjectError([file])
        capsule = _load_capsule(file)
        model, model_problems = _model(capsule, file)
        extensions = load_project(file.parent)
        config, problems = read_config(capsule.experiment, file)
        problems += _selection_problems(config, extensions, file)
        return cls(
            folder=file.parent,
            capsule_file=file,
            capsule=capsule,
            config=config,
            extensions=extensions,
            model=model,
            problems=tuple(problems),
            notes=tuple(p for p in model_problems if p.severity == "info"),
            facts=ModelFacts.from_step_model(model),
        )

    @property
    def ok(self) -> bool:
        """No setting problem (broken extension files are listed but never block a race)."""
        return not any(p.severity == "error" for p in self.problems)

    # --- engines -----------------------------------------------------------------------------

    def engine(self, draws: DemandDraws | None = None) -> StepEngine:
        """The model's engine; with `draws`, the episode seed picks the demand row (D-135)."""
        return StepEngine(self.model, draws)

    # --- score (REQ-217, D-152) ----------------------------------------------------------------

    @property
    def reward(self) -> Extension:
        return self.extensions.get("reward", self.config.score)

    def score(self, move: Move) -> float:
        """The chosen score of one move. `move.ledger.total` stays the dollars."""
        return float(self.reward(move))


def _selection_problems(config: ProjectConfig, extensions: Extensions, file: Path) -> list[Problem]:
    """A named extension that does not exist is a problem on its capsule key."""
    wanted = [("reward", "experiment.score", config.score)]
    problems = []
    for kind, key, name in wanted:
        known = extensions.names(kind)
        if name not in known:
            problems.append(
                Problem(
                    "config",
                    "unknown_name",
                    f"{key} names {name!r}, but there is no {kind} with that name.",
                    did_you_mean(name, known) or f"Add a {kind} file named {name}.",
                    file,
                    field=key,
                )
            )
    return problems

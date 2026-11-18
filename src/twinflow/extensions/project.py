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

import dataclasses
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import numpy as np

from twinflow.engines.step.engine import IDLE, DemandDraws, StepEngine
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.extensions.checker import (
    CheckLimits,
    ExtensionFailed,
    call_checked,
    demand_validator,
)
from twinflow.extensions.loader import (
    Extension,
    ExtensionNotFound,
    Extensions,
    Problem,
    load_project,
)
from twinflow.extensions.view import ModelFacts, PolicyState, policy_state
from twinflow.scenario import ScenarioCapsule
from twinflow.state.groups import GroupedView
from twinflow.state.issues import did_you_mean
from twinflow.state.markov import MarkovChain, estimate
from twinflow.state.records import Move, Snapshot

FEATURE_PREFIX = "feature."  # feature values in a snapshot's `extra` (REQ-219)

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
    maximum: float | None = None


# Defaults follow D-143 (separate tuning seed), D-144 (promotion rule), D-147 (20 ms) and
# D-148 (200 episodes, seed 0, 30 tuning episodes on seed 777).
DEFAULTS: tuple[Setting, ...] = (
    Setting(
        "experiment.score",
        "dollars",
        "The reward file the race ranks policies by. Dollars are always shown next to it.",
        "name",
    ),
    Setting(
        "experiment.demand",
        "poisson",
        "The demand file that draws each day's customer demand. Draws are made once per "
        "seed, so every policy sees the same dice.",
        "name",
    ),
    Setting(
        "experiment.features",
        (),
        "Feature files whose values every policy state gets (state.features) and every "
        "recorded state keeps (extra).",
        "names",
    ),
    Setting(
        "experiment.race.episodes",
        200,
        "Runs per policy in the race. Every policy runs on the same demand draws.",
        "whole",
        minimum=1,
    ),
    Setting("experiment.race.seed", 0, "Seed of the race's demand draws.", "whole"),
    Setting(
        "experiment.race.tune_episodes",
        30,
        "Runs per trial setting when the race tunes a policy's SETTINGS.",
        "whole",
        minimum=1,
    ),
    Setting(
        "experiment.race.tune_seed",
        777,
        "Seed of the tuning draws. It differs from the race seed, so settings are not "
        "picked to fit the scoring dice.",
        "whole",
    ),
    Setting(
        "experiment.race.interval",
        0.95,
        "How sure the intervals in the race report are (0.95 means 95%).",
        "share",
    ),
    Setting(
        "experiment.race.promote_rule",
        "interval_above_zero",
        "When `twinflow improve` keeps a candidate: interval_above_zero (the paired interval "
        "against the best is above zero) or mean_above_zero (it wins on average).",
        "choice",
        choices=("interval_above_zero", "mean_above_zero"),
    ),
    Setting(
        "experiment.race.top_policies",
        3,
        "How many of the best policies get a Markov map and saved moves.",
        "whole",
        minimum=1,
    ),
    Setting(
        "experiment.race.decision_timeout_s",
        2.0,
        "Seconds one decision may take in the race before the policy is stopped as slow.",
        "number",
        minimum=0.001,
    ),
    Setting(
        "experiment.checker.time_limit_ms",
        20.0,
        "Average milliseconds a decision (or any extension call) may take in the check.",
        "number",
        minimum=0.001,
    ),
    Setting(
        "experiment.checker.samples",
        200,
        "Most sample states the check tries each file on.",
        "whole",
        minimum=4,
    ),
    Setting(
        "experiment.markov.view",
        "family",
        "The view the race's Markov maps and money leaks use.",
        "name",
    ),
    Setting(
        "experiment.markov.top_k",
        15,
        "How many of the most visited states a Markov map shows.",
        "whole",
        minimum=1,
    ),
    Setting(
        "experiment.markov.min_prob",
        0.05,
        "Moves less likely than this are left off a Markov map.",
        "number",
        maximum=1.0,
    ),
)
SECTIONS = ("race", "checker", "markov")  # experiment keys that hold named settings

# Experiment keys read elsewhere (the capsule, the adapter, the DES capture).
OTHER_EXPERIMENT_KEYS = ("id", "replications", "seed", "policy", "state_capture")


class ProjectError(ValueError):
    """The project cannot be opened. `problems` says why, in plain words."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(str(p) for p in self.problems))


@dataclass(frozen=True)
class RaceSettings:
    """`experiment.race` (REQ-206, D-143, D-144, D-148)."""

    episodes: int = 200
    seed: int = 0
    tune_episodes: int = 30
    tune_seed: int = 777
    interval: float = 0.95
    promote_rule: str = "interval_above_zero"
    top_policies: int = 3
    decision_timeout_s: float = 2.0


@dataclass(frozen=True)
class MarkovSettings:
    """`experiment.markov` (REQ-199)."""

    view: str = "family"
    top_k: int = 15
    min_prob: float = 0.05


def _section(values: Mapping[str, Any], name: str) -> dict[str, Any]:
    prefix = f"experiment.{name}."
    return {k.removeprefix(prefix): v for k, v in values.items() if k.startswith(prefix)}


@dataclass(frozen=True)
class ProjectConfig:
    """The `experiment` settings, one value per `DEFAULTS` key."""

    values: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def score(self) -> str:
        return str(self.values["experiment.score"])

    @property
    def demand(self) -> str:
        return str(self.values["experiment.demand"])

    @property
    def race(self) -> RaceSettings:
        return RaceSettings(**_section(self.values, "race"))

    @property
    def checker(self) -> CheckLimits:
        return CheckLimits(**_section(self.values, "checker"))

    @property
    def markov(self) -> MarkovSettings:
        return MarkovSettings(**_section(self.values, "markov"))

    @property
    def features(self) -> tuple[str, ...]:
        return tuple(self.values["experiment.features"])


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
    if setting.maximum is not None and value > setting.maximum:
        return bad(f"{key} is {value}; it must be at most {setting.maximum:g}.", "Lower it.")
    return (int(value) if whole else float(value)), None


def _unknown(experiment: Mapping[str, Any], file: Path) -> list[Problem]:
    """A did-you-mean problem for each key no setting reads (REQ-221)."""
    known: dict[str, set[str]] = {"": set(OTHER_EXPERIMENT_KEYS)}
    for setting in DEFAULTS:
        parts = setting.key.split(".")[1:]
        known[""].add(parts[0])
        if len(parts) == 2:
            known.setdefault(parts[0], set()).add(parts[1])
    problems = []

    def unknown(key: str, choices: set[str]) -> Problem:
        return Problem(
            "config",
            "unknown_key",
            f"{key} is not a setting twinflow knows.",
            did_you_mean(key.rsplit(".", 1)[-1], choices),
            file,
            field=key,
        )

    for name, value in experiment.items():
        if name not in known[""]:
            problems.append(unknown(f"experiment.{name}", known[""]))
        elif name in SECTIONS and not isinstance(value, Mapping):
            problems.append(
                _config_problem(
                    file,
                    f"experiment.{name}",
                    f"experiment.{name} is {value!r}; it must be a set of named settings.",
                    f"Write {name}: {{{sorted(known[name])[0]}: ...}}.",
                )
            )
        elif name in SECTIONS:
            problems += [
                unknown(f"experiment.{name}.{sub}", known[name])
                for sub in value
                if sub not in known[name]
            ]
    return problems


def read_config(experiment: Mapping[str, Any], file: Path) -> tuple[ProjectConfig, list[Problem]]:
    """Every `DEFAULTS` key, from the capsule or its default; bad values and unknown keys are
    problems, and a bad value falls back to its default."""
    values: dict[str, Any] = {}
    problems = _unknown(experiment, file)
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
        problems += _view_clashes(model, extensions)
        problems += _markov_view_problems(config, model, extensions, file)
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
        """The model's engine. With `draws` (from `demand_draws`) the episode number given
        to `start` picks the demand row (D-135); without, it draws plain Poisson demand."""
        return StepEngine(self.model, draws)

    def demand_draws(self, episodes: int, seed: int) -> DemandDraws:
        """Demand of shape (episodes, days, products) from the chosen demand file, made
        once per seed before any policy runs (D-153). One random stream from `seed` feeds
        every call in episode and day order, so `poisson` equals the D-135 draws.

        A demand file that raises or returns bad units stops the draws with a
        `ProjectError` naming the file and line.
        """
        ext = self.extensions.get("demand", self.config.demand)
        products = self.model.product_ids
        validate = demand_validator(products)
        rng = np.random.default_rng(seed)
        draws = np.zeros((episodes, self.model.horizon, len(products)), dtype=np.int64)
        try:
            for episode in range(episodes):
                for day in range(self.model.horizon):
                    units = call_checked(ext, (day, rng, self.facts), validate=validate)
                    draws[episode, day] = [int(units[p]) for p in products]
        except ExtensionFailed as failed:
            raise ProjectError([failed.problem]) from None
        return draws

    # --- score (REQ-217, D-152) ----------------------------------------------------------------

    @property
    def reward(self) -> Extension:
        return self.extensions.get("reward", self.config.score)

    def score(self, move: Move) -> float:
        """The chosen score of one move. `move.ledger.total` stays the dollars."""
        return float(self.reward(move))

    # --- policy states and features (REQ-202, REQ-219) -----------------------------------------

    @property
    def actions(self) -> tuple[str, ...]:
        """What a make-next policy may return: a product id or `"idle"`."""
        return (*self.model.product_ids, IDLE)

    def features(self, state: PolicyState) -> dict[str, float]:
        """The chosen feature files' values for one state; a later file wins a shared name."""
        values: dict[str, float] = {}
        for name in self.config.features:
            values.update(self.extensions.get("feature", name)(state))
        return values

    def policy_state(self, snapshot: Snapshot, actions: Iterable[str] | None = None) -> PolicyState:
        """The full view of one snapshot, with the chosen features (no observation mask)."""
        state = policy_state(snapshot, self.facts, self.actions if actions is None else actions)
        if not self.config.features:
            return state
        return state.replace(features=self.features(state))

    def annotate(self, move: Move) -> Move:
        """The move with feature values in `extra` of both snapshots, as `feature.<name>`."""
        if not self.config.features:
            return move
        return dataclasses.replace(
            move, before=self._with_features(move.before), after=self._with_features(move.after)
        )

    def _with_features(self, snapshot: Snapshot) -> Snapshot:
        values = self.features(self.policy_state(snapshot))
        extra = {**snapshot.extra, **{FEATURE_PREFIX + k: float(v) for k, v in values.items()}}
        return dataclasses.replace(snapshot, extra=extra)

    # --- views and Markov maps (REQ-199, REQ-218) ----------------------------------------------

    @property
    def view_names(self) -> tuple[str, ...]:
        return tuple(sorted({*self.model.views, *self.extensions.names("view")}))

    def view(self, name: str) -> GroupedView | ExtensionView:
        """A YAML view (`state_groups`) or a view file; YAML wins a shared name."""
        if name in self.model.views:
            return self.model.views[name]
        if name in self.extensions.names("view"):
            return ExtensionView(self, self.extensions.get("view", name))
        raise ExtensionNotFound("view", name, self.view_names)

    def markov(
        self,
        moves: Sequence[Move],
        view: str | None = None,
        *,
        top_k: int | None = None,
        min_prob: float | None = None,
    ) -> MarkovChain:
        """The Markov chain of `moves` under a named view (COMP-148); anything left out
        comes from `experiment.markov`."""
        settings = self.config.markov
        return estimate(
            moves,
            self.view(settings.view if view is None else view),
            demand_rate=self.facts.demand_rate,
            families=self.facts.family,
            top_k=settings.top_k if top_k is None else top_k,
            min_prob=settings.min_prob if min_prob is None else min_prob,
        )


@dataclass(frozen=True, eq=False)
class ExtensionView:
    """A view file as a `Snapshot -> label` function for Markov maps (REQ-218)."""

    project: Project
    extension: Extension

    @property
    def name(self) -> str:
        return self.extension.name

    def __call__(self, snapshot: Snapshot) -> str:
        return str(self.extension(self.project.policy_state(snapshot)))


def _markov_view_problems(
    config: ProjectConfig, model: StepModel, extensions: Extensions, file: Path
) -> list[Problem]:
    view = config.markov.view
    known = {*model.views, *extensions.names("view")}
    if view in known:
        return []
    return [
        Problem(
            "config",
            "unknown_name",
            f"experiment.markov.view names {view!r}, but there is no view with that name.",
            did_you_mean(view, known),
            file,
            field="experiment.markov.view",
        )
    ]


def _view_clashes(model: StepModel, extensions: Extensions) -> list[Problem]:
    return [
        Problem(
            "view",
            "view_name_clash",
            f"The view file {ext.file.name} uses the name {ext.name!r}, which a view in the "
            "capsule's state_groups already has. The capsule's view is used.",
            "Give the view file another NAME.",
            ext.file,
            ext.line,
            ext.name,
            severity="info",
        )
        for ext in extensions.of("view")
        if ext.name in model.views
    ]


def _selection_problems(config: ProjectConfig, extensions: Extensions, file: Path) -> list[Problem]:
    """A named extension that does not exist is a problem on its capsule key."""
    wanted = [
        ("reward", "experiment.score", config.score),
        ("demand", "experiment.demand", config.demand),
    ]
    wanted += [("feature", "experiment.features", name) for name in config.features]
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


def _shown(value: Any) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def render_defaults_markdown() -> str:
    """The how-to page `docs/how-to/tune.md`, made from `DEFAULTS` (REQ-221). A test fails
    when the page and the table differ."""
    lines = [
        "# Tune your twin",
        "",
        "<!-- Made from twinflow.extensions.project.DEFAULTS. Do not edit by hand: a test",
        "     compares this page with the table. -->",
        "",
        "Every number and choice below is set in the capsule's `experiment:` section.",
        "Leave a setting out and the default is used. A misspelled setting is reported",
        "with the closest real name.",
        "",
        "```yaml",
        "experiment:",
        "  score: dollars",
        "  race: {episodes: 200, seed: 0}",
        "  checker: {time_limit_ms: 20}",
        "```",
        "",
        "| Setting | Default | What it does |",
        "|---|---|---|",
    ]
    for setting in DEFAULTS:
        meaning = setting.meaning.replace("|", "\\|")
        lines.append(f"| `{setting.key}` | `{_shown(setting.default)}` | {meaning} |")
    lines += [
        "",
        "To change how a policy, reward, view, feature, demand or observation file works,",
        "copy the built-in file into the matching folder of your project and edit it. A file",
        "with the same `NAME` as a built-in replaces it.",
        "",
    ]
    return "\n".join(lines)

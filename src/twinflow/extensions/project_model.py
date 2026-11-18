"""TF-006 B-30: the data behind the project view (DATA-CONTRACT 5, P1 to P3).

`project_view_model(project)` answers "is this twin ready to trust, and what do I fix first?":
the problems (errors first), what the twin holds, the rules and the score, the views, and the
latest results found under `runs/`. `project_error_model` is the page for a project that
cannot open at all.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from twinflow import __version__
from twinflow.extensions.checker import check_project
from twinflow.extensions.loader import Extension, Problem
from twinflow.extensions.project import Project, ProjectError
from twinflow.extensions.report_model import COLOURS, economics_of, label

SCHEMA_VERSION = 1
MAX_RECENT = 5
OWN_RULES_NOTE = (
    "No rules of your own yet. The {n} built-in rules will race. "
    "Add one with `twinflow new` or copy a built-in rule into `policies/`."
)
VIEW_WORDS = {
    "full": "Exact stock numbers for every product.",
    "family": "The lowest cover level in each family, and the family on the line.",
    "simple": "How full each family is, and which family the line is set for.",
}


def _money(value: float) -> str:
    return f"${abs(round(value)):,}"


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _lost_day(share: float) -> str:
    if abs(share - 0.5) < 1e-9:
        return "half a day"
    if abs(share - 1.0) < 1e-9:
        return "a day"
    return f"{round(100 * share)}% of a day"


def _percent(rate: float) -> str:
    return f"{round(rate * 100, 2):g}%"


def _relative(folder: Path, file: Path | None) -> str | None:
    if file is None:
        return None
    try:
        return file.resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        return str(file)


def _name(project: Project) -> str:
    return project.capsule_file.stem.removesuffix(".twin")


def _problem_row(folder: Path, problem: Problem) -> dict[str, Any]:
    where = (
        _relative(folder, problem.file)
        or problem.field
        or (f"{problem.kind} {problem.name}" if problem.name else problem.kind)
    )
    return {
        "severity": "error" if problem.severity == "error" else "warning",
        "where": where,
        "line": problem.line,
        "message": problem.message,
        "fix": problem.fix,
    }


def _problems(folder: Path, found: Iterable[Problem]) -> list[dict[str, Any]]:
    rows = [_problem_row(folder, p) for p in found]
    return sorted(rows, key=lambda r: r["severity"] != "error")  # errors first, order kept


def _status(errors: int, warnings: int) -> dict[str, Any]:
    if errors:
        headline = f"{_count(errors, 'problem')} to fix before you can race."
        level = "needs_fixes"
    else:
        headline = "Ready. 0 problems to fix."
        if warnings:
            headline += f" {_count(warnings, 'warning')} to look at."
        level = "ready"
    return {"level": level, "headline": headline, "errors": errors, "warnings": warnings}


def _summary(project: Project, families: list[dict[str, Any]]) -> str:
    facts = project.facts
    if project.model is None:
        return f"A {project.domain.replace('_', ' ')} twin with its own engine."
    line = (facts.line or "line").replace("_", " ")
    return (
        f"One {line}, {_count(len(facts.products), 'product')} in "
        f"{_count(len(families), 'family', 'families')}. Each run is {facts.horizon} days."
    )


def _costs(project: Project) -> list[dict[str, str]]:
    money = economics_of(project)
    families = {f.lower() for f in project.facts.family.values()}
    colours = bool(families) and families <= COLOURS
    names = (
        {"within_family": "Same colour change", "across_family": "Colour change"}
        if colours
        else {"within_family": "Same family change", "across_family": "Change to another family"}
    )
    rows = [
        {"label": names[kind], "value": f"{_money(v['cost'])} and {_lost_day(v['output_lost'])}"}
        for kind, v in money["changeover"].items()
        if kind in names and v["cost"] is not None
    ]
    if money["holding_rate"] is not None:
        rows.append(
            {
                "label": "Keeping stock",
                "value": f"{_percent(money['holding_rate'])} of unit cost a night",
            }
        )
    if money["lost_sale_rate"] is not None:
        rows.append(
            {"label": "A lost sale", "value": f"{_percent(money['lost_sale_rate'])} of the price"}
        )
    return rows


def _twin(project: Project) -> dict[str, Any]:
    facts = project.facts
    counts: dict[str, int] = {}
    for family in facts.family.values():
        counts[family] = counts.get(family, 0) + 1
    assumptions = project.capsule.assumptions
    texts = list(assumptions.values()) if isinstance(assumptions, Mapping) else assumptions
    provenance = project.capsule.provenance.get("source") if project.capsule.provenance else None
    return {
        "lines": [{"id": facts.line, "label": facts.line.replace("_", " ")}] if facts.line else [],
        "products": len(facts.products),
        "families": [{"name": n, "products": c} for n, c in counts.items()],
        "horizon": {"value": facts.horizon, "unit": "days"},
        "costs": _costs(project) if project.model is not None else [],
        "assumptions": [str(t) for t in texts],
        "provenance": None if provenance is None else str(provenance),
    }


def _tried(grid: Mapping[str, Sequence[Any]]) -> int:
    return math.prod(len(v) for v in grid.values()) if grid else 1


def _rule(project: Project, ext: Extension, broken: set[Path]) -> dict[str, Any]:
    own = ext.source != "builtin"
    return {
        "policy": ext.name,
        "label": label(ext.name),
        "about": ext.summary,
        "source": "project" if own else "builtin",
        "file": _relative(project.folder, ext.file) if own else None,
        "is_baseline": ext.name == project.baseline,
        "settings_tried": _tried(ext.grid),
        "ok": ext.file not in broken,
    }


def _rules(project: Project, found: Sequence[Problem]) -> list[dict[str, Any]]:
    broken = {p.file for p in found if p.file is not None}
    policies = [p for p in project.extensions.of("policy") if p.decision == "make_next"]
    rows = [_rule(project, p, broken) for p in policies]
    return sorted(rows, key=lambda r: (r["source"] != "project", r["policy"]))


def _views(project: Project) -> list[dict[str, str]]:
    rows = []
    for name in project.view_names:
        ext = next((e for e in project.extensions.of("view") if e.name == name), None)
        about = VIEW_WORDS.get(name) or (ext.summary if ext else "") or "A view from your capsule."
        rows.append({"name": name, "kind": "state_view", "about": about})
    for name in project.config.observations:
        ext = next((e for e in project.extensions.of("observation") if e.name == name), None)
        rows.append({"name": name, "kind": "observation", "about": ext.summary if ext else ""})
    return rows


def _extensions(project: Project, found: Sequence[Problem]) -> list[dict[str, Any]]:
    broken = {p.file for p in found if p.file is not None}
    return [
        {
            "kind": ext.kind,
            "name": ext.name,
            "file": _relative(project.folder, ext.file),
            "ok": ext.file not in broken,
        }
        for ext in project.extensions.all()
        if ext.source != "builtin" and ext.kind != "policy"
    ]


def _recent_one(folder: Path, run: Path) -> dict[str, Any] | None:
    improve, race_file = run / "improve.json", run / "race.json"
    try:
        if improve.is_file():
            data = json.loads(improve.read_text(encoding="utf-8"))
            name, old = label(data["candidate"]["name"]), label(data["previous_best"]["name"])
            sentence = (
                f"Improve: {name} won and was copied."
                if data["promoted"]
                else f"Improve: {name} did not beat {old}."
            )
            return _recent_row("improve", run, folder, sentence, data.get("written_at"))
        if race_file.is_file():
            ranking = json.loads(race_file.read_text(encoding="utf-8"))["ranking"]
            sentence = f"Race: {label(ranking[0])} ranked first of {len(ranking)} rules."
            return _recent_row("race", run, folder, sentence, None)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return None


def _recent_row(
    view: str, run: Path, folder: Path, sentence: str, when: str | None
) -> dict[str, Any]:
    page = run / f"{view}.html"
    stamp = when or datetime.fromtimestamp(run.stat().st_mtime, UTC).isoformat(timespec="seconds")
    return {
        "view": view,
        "id": run.name,
        "when": stamp,
        "sentence": sentence,
        "href": _relative(folder, page) if page.is_file() else None,
    }


def _recent(folder: Path) -> list[dict[str, Any]]:
    runs = folder / "runs"
    if not runs.is_dir():
        return []
    rows = [r for run in runs.iterdir() if run.is_dir() and (r := _recent_one(folder, run))]
    return sorted(rows, key=lambda r: r["when"], reverse=True)[:MAX_RECENT]


def _agent_summary(model: Mapping[str, Any]) -> list[str]:
    rules = model["rules"]
    own = sum(1 for r in rules if r["source"] == "project")
    lines = [model["status"]["headline"], model["about"]["summary"]]
    lines.append(f"{_count(own, 'rule')} of your own and {len(rules) - own} built in.")
    lines.append(f"The score is {model['score']['name']}.")
    for problem in model["problems"][:3]:
        lines.append(f"{problem['where']}: {problem['message']} Fix: {problem['fix']}")
    return lines


def project_view_model(project: Project, found: Sequence[Problem] | None = None) -> dict[str, Any]:
    """The project view model for an opened project. Runs the checker unless `found` (its
    problems) is given."""
    found = check_project(project) if found is None else found
    problems = _problems(project.folder, found)
    errors = sum(1 for p in problems if p["severity"] == "error")
    twin = _twin(project)
    reward = project.reward
    rules = _rules(project, found)
    model: dict[str, Any] = {
        "view": "project",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": {
            "title": f"Project: {_name(project)}",
            "project": _name(project),
            "folder": str(project.folder),
            "capsule": project.capsule_file.name,
            "domain": project.domain,
            "summary": _summary(project, twin["families"]),
        },
        "status": _status(errors, len(problems) - errors),
        "twin": twin,
        "rules": rules,
        "rules_note": (
            None
            if any(r["source"] == "project" for r in rules)
            else OWN_RULES_NOTE.format(n=len(rules))
        ),
        "score": {
            "name": project.config.score,
            "is_dollars": project.config.score == "dollars",
            "file": _relative(project.folder, reward.file) if reward.source != "builtin" else None,
        },
        "views": _views(project),
        "extensions": _extensions(project, found),
        "problems": problems,
        "recent": _recent(project.folder),
        "warnings": [],
    }
    model["agent_summary"] = _agent_summary(model)
    return model


def project_error_model(folder: Path | str, error: ProjectError) -> dict[str, Any]:
    """The page for a project that cannot open: the problems, and nothing else."""
    where = Path(folder)
    problems = [{**_problem_row(where, p), "severity": "error"} for p in error.problems]
    name = where.name or "project"
    model: dict[str, Any] = {
        "view": "project",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": {
            "title": f"Project: {name}",
            "project": name,
            "folder": str(where),
            "capsule": "",
            "domain": "",
            "summary": "This project could not be opened.",
        },
        "status": {
            "level": "broken",
            "headline": f"{_count(len(problems), 'problem')} to fix before this project can open.",
            "errors": len(problems),
            "warnings": 0,
        },
        "twin": {
            "lines": [],
            "products": 0,
            "families": [],
            "horizon": {"value": 0, "unit": "days"},
            "costs": [],
            "assumptions": [],
            "provenance": None,
        },
        "rules": [],
        "rules_note": None,
        "score": {"name": "", "is_dollars": False, "file": None},
        "views": [],
        "extensions": [],
        "problems": problems,
        "recent": [],
        "warnings": [],
    }
    model["agent_summary"] = _agent_summary(model)[:1] + [
        f"{p['where']}: {p['message']} Fix: {p['fix']}" for p in problems[:3]
    ]
    return model


__all__ = ["project_error_model", "project_view_model"]

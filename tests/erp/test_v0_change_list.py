"""The v0-to-v1 change list matches the shipped `twinflow.erp/1` models (T-217, REQ-181).

The Western Spring mapper team works from `docs/enterprise/ERP-V0-TO-V1-CHANGES.md`. This
test parses its "Field table" and checks the doc against the code, so the list cannot drift:
every v1 field it names exists, every v0 model has a row, the version string matches, and
every issue code it names is a code the code can raise. Its "Field requirements" table lists
every field of every model with whether it is required and its type; those cells are checked
against the pydantic models too (`describe_field` is the one place that turns an annotation
into the doc's vocabulary).
"""

from __future__ import annotations

import datetime as dt
import re
import types
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo
from pydantic.types import Strict

from twinflow.erp import schema
from twinflow.erp._checks import RULES
from twinflow.erp.profile import ErpPolicy

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "enterprise" / "ERP-V0-TO-V1-CHANGES.md"
ERP_SOURCES = ROOT / "src" / "twinflow" / "erp"

SKIPPED_V1 = {"_(none)_", "_(removed)_", "_(not in TwinFlow)_"}
# Every model of the v0 `canonical.py` (plus its shared `Status` enum and nested `CalendarDay`).
V0_MODELS = {
    "Bom",
    "BomLine",
    "Calendar",
    "CalendarDay",
    "CalendarException",
    "Customer",
    "ErpSnapshot",
    "ErpSnapshotSource",
    "IssueCode",
    "Item",
    "Machine",
    "MappingIssue",
    "OrderOperation",
    "ProductionOrder",
    "PurchaseLine",
    "Routing",
    "RoutingOperation",
    "SalesOrderLine",
    "Shift",
    "SourceRef",
    "Status",
    "Vendor",
    "WorkCenter",
}
# Snake-case words the doc quotes that are data keys the mapper writes, not issue codes.
ATTRIBUTE_KEYS = {"op_kind", "wire_family", "wire_diameter_mm", "wire_grade"}

_SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_BACKTICKED = re.compile(r"`([^`]+)`")


def _models() -> dict[str, type[BaseModel]]:
    return {
        name: cls
        for name, cls in vars(schema).items()
        if isinstance(cls, type)
        and issubclass(cls, BaseModel)
        and cls.__module__ == schema.__name__
    }


class _Spec:
    """Parser of the doc's shorthand: `Model`, `Model.field`, `Model{a, b: Nested{c}}`."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.found: list[tuple[str, list[str]]] = []

    def parse(self) -> list[tuple[str, list[str]]]:
        self._model()
        return self.found

    def _ident(self) -> str:
        match = _IDENT.match(self.text, self.pos)
        assert match, f"cannot parse {self.text!r} at {self.pos}"
        self.pos = match.end()
        return match.group()

    def _model(self) -> None:
        name = self._ident()
        fields: list[str] = []
        self.found.append((name, fields))
        if self.text.startswith(".", self.pos):
            self.pos += 1
            fields.append(self._ident())
            self._skip_type()
        elif self.text.startswith("{", self.pos):
            self.pos += 1
            while True:
                fields.append(self._ident())
                self._member_type()
                if self.text.startswith(",", self.pos):
                    self.pos += 1
                    self._skip_space()
                    continue
                break
            assert self.text.startswith("}", self.pos), f"missing }} in {self.text!r}"
            self.pos += 1

    def _skip_space(self) -> None:
        while self.text.startswith(" ", self.pos):
            self.pos += 1

    def _skip_type(self) -> None:
        """After `Model.field`: an index like `["op_kind"]` or a `: type` annotation."""
        if self.text.startswith("[", self.pos):
            self.pos = self.text.index("]", self.pos) + 1
        if self.text.startswith(": ", self.pos):
            self.pos += 2
            self._type()

    def _member_type(self) -> None:
        if self.text.startswith(": ", self.pos):
            self.pos += 2
            self._type()

    def _type(self) -> None:
        """A nested `Name{...}` is parsed as a model; any other type is skipped."""
        match = re.compile(r"[A-Z][A-Za-z0-9_]*\{").match(self.text, self.pos)
        if match:
            self._model()
            return
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in "[(":
                depth += 1
            elif char in "])":
                depth -= 1
            elif depth == 0 and char in ",}":
                return
            self.pos += 1


def _field_table_rows() -> list[tuple[str, str]]:
    """The (v0 cell, v1 cell) of every row of the doc's "Field table"."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Field table")
    rows: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|") or set(line) <= {"|", "-", " "} or line.startswith("| v0 "):
            continue
        cells = [cell.strip() for cell in _CELL_SPLIT.split(line.strip())[1:-1]]
        assert len(cells) == 4, f"row does not have 4 cells: {line[:80]}"
        rows.append((cells[0], cells[1]))
    return rows


def _specs(cell: str) -> list[tuple[str, list[str]]]:
    """(model, fields) pairs named by a cell; a bare `.field` belongs to the previous model."""
    found: list[tuple[str, list[str]]] = []
    for chunk in _BACKTICKED.findall(cell):
        chunk = chunk.strip()
        if chunk.startswith("."):
            assert found, f"{chunk!r} has no model before it in {cell[:80]!r}"
            found.append((found[-1][0], [chunk.removeprefix(".")]))
        elif chunk[:1].isupper():
            found.extend(_Spec(chunk).parse())
    return found


def test_doc_exists_and_has_rows() -> None:
    assert len(_field_table_rows()) > 40


def test_version_string_matches_the_code() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert schema.ERP_SCHEMA_VERSION == "twinflow.erp/1"
    assert f"`{schema.ERP_SCHEMA_VERSION}`" in text or f'"{schema.ERP_SCHEMA_VERSION}"' in text
    assert set(re.findall(r"twinflow\.erp/\d+", text)) == {schema.ERP_SCHEMA_VERSION}


def test_every_v1_field_named_exists_on_its_model() -> None:
    models = _models()
    checked = 0
    for v0_cell, v1_cell in _field_table_rows():
        if v1_cell in SKIPPED_V1:
            continue
        specs = _specs(v1_cell)
        assert specs, f"row for {v0_cell!r} names no v1 model: {v1_cell[:80]!r}"
        for model, fields in specs:
            assert model in models, f"{model} (row {v0_cell!r}) is not a twinflow.erp model"
            missing = [name for name in fields if name not in models[model].model_fields]
            assert not missing, f"{model} has no field {missing} (row {v0_cell!r})"
            checked += len(fields)
    assert checked > 80


def test_every_v0_model_has_a_row() -> None:
    named: set[str] = set()
    for v0_cell, _ in _field_table_rows():
        named.update(model for model, _fields in _specs(v0_cell))
    assert V0_MODELS <= named, sorted(V0_MODELS - named)
    assert named <= V0_MODELS, sorted(named - V0_MODELS)


def _enum_values() -> set[str]:
    """Every `Literal` value of the models (statuses such as `in_process`), which are not codes."""
    values: set[str] = set()
    pending = [info.annotation for cls in _models().values() for info in cls.model_fields.values()]
    while pending:
        annotation = pending.pop()
        if get_origin(annotation) is Literal:
            values.update(str(value) for value in get_args(annotation))
        else:
            pending.extend(get_args(annotation))
    return values


def _code_literals() -> set[str]:
    """Every issue code the ERP package can raise."""
    codes = set(RULES)
    pattern = re.compile(r'(?:_hard|_po_issue|issue)\(\s*"([a-z_]+)"|code="([a-z_]+)"')
    for name in ("digest.py", "materials.py", "build.py", "validate.py"):
        for match in pattern.finditer((ERP_SOURCES / name).read_text(encoding="utf-8")):
            codes.add(match.group(1) or match.group(2))
    return codes


def test_every_issue_code_named_exists_in_the_code() -> None:
    text = DOC.read_text(encoding="utf-8")
    v0_words = {
        word
        for v0_cell, _ in _field_table_rows()
        for chunk in _BACKTICKED.findall(v0_cell)
        for word in _IDENT.findall(chunk)
    }
    field_names = {name for cls in _models().values() for name in cls.model_fields}
    not_codes = (
        field_names | set(ErpPolicy.model_fields) | v0_words | ATTRIBUTE_KEYS | _enum_values()
    )
    named = {
        chunk
        for chunk in _BACKTICKED.findall(text)
        if _SNAKE.fullmatch(chunk) and chunk not in not_codes
    }
    assert "unsupported_schema_version" in named
    unknown = named - _code_literals()
    assert not unknown, f"the doc names issue codes the code does not raise: {sorted(unknown)}"


@pytest.mark.parametrize("code", ["unsupported_schema_version", "batch_lot_exceeds_capacity"])
def test_key_codes_are_real(code: str) -> None:
    assert code in _code_literals()


# --- requiredness and types ---------------------------------------------------------------------

_NONE_TYPE = type(None)
_HEX64 = "{64}"


def _strip(annotation: Any) -> tuple[Any, list[Any]]:
    """Remove `Annotated[...]` wrappers -> (core type, their metadata)."""
    metadata: list[Any] = []
    while get_origin(annotation) is Annotated:
        annotation, *extra = get_args(annotation)
        for item in extra:
            metadata.extend(item.metadata if isinstance(item, FieldInfo) else [item])
    return annotation, metadata


def _type_name(annotation: Any, outer_metadata: tuple[Any, ...] = ()) -> tuple[str, bool]:
    """(doc type text, None allowed) of an annotation, in the doc's vocabulary.

    `outer_metadata` is what pydantic already lifted off the field's own `Annotated` (`Strict`).
    """
    core, metadata = _strip(annotation)
    metadata = [*outer_metadata, *metadata]
    origin = get_origin(core)
    if origin in (Union, types.UnionType):
        members = [_type_name(arg) for arg in get_args(core) if arg is not _NONE_TYPE]
        nullable = any(arg is _NONE_TYPE for arg in get_args(core)) or any(n for _, n in members)
        return " | ".join(text for text, _ in members), nullable
    if origin is Literal:
        return "Literal[" + ", ".join(str(value) for value in get_args(core)) + "]", False
    if origin is tuple:
        return f"tuple[{_type_name(get_args(core)[0])[0]}]", False
    if origin is dict:
        key, value = get_args(core)
        return f"dict[{_type_name(key)[0]}, {_type_name(value)[0]}]", False
    return _scalar_name(core, metadata), False


def _scalar_name(core: Any, metadata: list[Any]) -> str:
    if core is AwareDatetime:
        return "aware datetime"
    if core is dt.date:
        return "date"
    if core is bool:
        return (
            "bool (strict)" if any(isinstance(m, Strict) and m.strict for m in metadata) else "bool"
        )
    if core is str and any(_HEX64 in str(getattr(m, "pattern", "")) for m in metadata):
        return "sha256 hex"
    if isinstance(core, type):
        return core.__name__
    raise AssertionError(f"the doc has no word for {core!r}")


def describe_field(info: FieldInfo) -> tuple[str, str]:
    """(requiredness, type) of a model field as the doc's "Field requirements" table writes them.

    Requiredness is `required`, `required-nullable` (must be present, may be null) or
    `optional (default X)`.
    """
    type_text, nullable = _type_name(info.annotation, tuple(info.metadata))
    if info.is_required():
        return ("required-nullable" if nullable else "required"), type_text
    return f"optional (default {info.get_default(call_default_factory=True)})", type_text


def _requirement_rows() -> list[tuple[str, str, str]]:
    """The (`Model.field`, requiredness, type) of every row of the doc's "Field requirements"."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Field requirements")
    rows: list[tuple[str, str, str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|") or set(line) <= {"|", "-", " "} or line.startswith("| Field "):
            continue
        cells = [cell.strip().strip("`") for cell in _CELL_SPLIT.split(line.strip())[1:-1]]
        assert len(cells) == 3, f"requirement row does not have 3 cells: {line[:80]}"
        rows.append((cells[0], cells[1], cells[2].replace("\\|", "|")))
    return rows


def test_requirement_table_matches_the_models() -> None:
    models = _models()
    documented: set[str] = set()
    for name, requiredness, type_text in _requirement_rows():
        model, _, field = name.partition(".")
        assert model in models and field in models[model].model_fields, f"{name} is not a field"
        assert name not in documented, f"{name} is listed twice"
        documented.add(name)
        assert describe_field(models[model].model_fields[field]) == (requiredness, type_text), name


def test_requirement_table_covers_every_field() -> None:
    every = {f"{m}.{f}" for m, cls in _models().items() for f in cls.model_fields}
    documented = {name for name, _, _ in _requirement_rows()}
    assert every == documented, sorted(every ^ documented)


def test_lineage_sentence_lists_every_model_with_a_lineage_field() -> None:
    """The doc names the models that need `lineage`; `SourceIssue.lineage` is the nullable one."""
    text = DOC.read_text(encoding="utf-8")
    sentence = next(
        line for line in text.splitlines() if line.startswith("- `lineage` is required on")
    )
    named = set(_BACKTICKED.findall(sentence.split(":", 1)[1].split(". ")[0]))
    required = {
        m
        for m, cls in _models().items()
        if "lineage" in cls.model_fields
        and describe_field(cls.model_fields["lineage"]) == ("required", "Lineage")
    }
    assert named == required, sorted(named ^ required)
    assert describe_field(schema.SourceIssue.model_fields["lineage"]) == (
        "required-nullable",
        "Lineage",
    )

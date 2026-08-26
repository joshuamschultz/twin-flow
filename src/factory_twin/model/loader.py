"""COMP-014 ModelLoader — the ONLY importer of PyYAML in the system (D-001).

Reads one model.yaml with yaml.safe_load; produces an unvalidated parse tree.
Never resolves includes or external references.
"""

from __future__ import annotations

import yaml


class RawModel:
    """The unvalidated parse tree of one model.yaml.

    `data` is the verbatim dict `yaml.safe_load` returned — no compiling, no
    defaulting, no mutation. The named accessors below are convenience
    lookups into that same dict, one per committed top-level section.
    """

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    @property
    def stocks(self) -> object:
        return self.data["stocks"]

    @property
    def part_types(self) -> object:
        return self.data["part_types"]

    @property
    def locations(self) -> object:
        return self.data["locations"]

    @property
    def machines(self) -> object:
        return self.data["machines"]

    @property
    def labor(self) -> object:
        return self.data["labor"]

    @property
    def processes(self) -> object:
        return self.data["processes"]

    @property
    def routing(self) -> object:
        return self.data["routing"]

    @property
    def bom(self) -> object:
        return self.data["bom"]


def load_raw_model(path: str) -> RawModel:
    """Read `path` and parse it with `yaml.safe_load` only.

    Raises:
        OSError: `path` does not exist or cannot be read.
        yaml.YAMLError: the document contains a tag `safe_load` refuses to
            construct (e.g. `!!python/object...`, an unknown `!include`-style
            tag). The refused object is never instantiated.
        TypeError: the parsed document's top level is not a mapping (e.g.
            empty file -> None, or a bare YAML sequence).
    """
    with open(path, encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle)

    if not isinstance(parsed, dict):
        raise TypeError(
            f"model file {path!r} must parse to a top-level mapping, got {type(parsed).__name__}"
        )

    return RawModel(parsed)

"""COMP-014 ModelLoader — the ONLY importer of PyYAML in the system (D-001).

Reads one model.yaml with yaml.safe_load; produces an unvalidated parse tree.
Never resolves includes or external references.
"""

from __future__ import annotations

from typing import Any

import yaml

MAX_NESTING = 32


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


def _reject_yaml_aliases(raw: bytes) -> None:
    """Reject anchors and aliases before safe_load can expand them."""
    tokens = yaml.scan(raw)
    for token in tokens:
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError("YAML anchors and aliases are not allowed in capsules")


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""


def _unique_mapping(
    loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def parse_document(raw: bytes) -> object:
    """Parse YAML after token/node checks, with stable boundary exceptions."""
    try:
        _reject_yaml_aliases(raw)
        node = yaml.compose(raw)
        if node is not None:
            _check_yaml_node(node)
        loader = _UniqueSafeLoader(raw)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"invalid capsule YAML: {exc}") from exc


def _check_yaml_node(node: yaml.Node, depth: int = 0) -> int:
    if depth > MAX_NESTING:
        raise ValueError(f"capsule nesting exceeds {MAX_NESTING} levels")
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        total = 1
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value in keys:
                raise ValueError(f"duplicate YAML key: {key.value!r}")
            if isinstance(key, yaml.ScalarNode):
                keys.add(key.value)
            total += _check_yaml_node(key, depth + 1) + _check_yaml_node(value, depth + 1)
        return total
    if isinstance(node, yaml.SequenceNode):
        return 1 + sum(_check_yaml_node(value, depth + 1) for value in node.value)
    return 1



def dump_document(data: object) -> str:
    """Serialize portable data through the same safe YAML boundary."""
    return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

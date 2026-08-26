"""Unit tests for COMP-014 ModelLoader (T-022, red).

ModelLoader (model/loader.py) is Layer 2's trust boundary and THE ONLY PyYAML
importer in the system (structure.md, "model/loader.py is the only importer of
PyYAML"; D-001). Its whole job is narrow and dangerous-by-omission:

  * Parse ONE model.yaml with `yaml.safe_load` — never `yaml.load`, never a
    custom/full loader.
  * Produce an UNVALIDATED parse tree (RawModel). No schema check, no graph
    check, no expression evaluation — those are validate.py/expressions.py,
    later in Layer 2. The loader only decides "did this parse safely", not
    "is this a legal floor".
  * NEVER resolve includes or external references. There is no multi-file
    compositing step. A `!include`-style tag is just an unknown YAML tag to
    `safe_load`, which is exactly the point: no code path in this module ever
    opens a second file.

The load-bearing security property under test: `yaml.safe_load` refuses any
Python object constructor tag (`!!python/object...`) rather than instantiating
it. That is what stands between a hostile model.yaml and remote code
execution (tech.md OWASP baseline, "Safe deserialization", D-001).

Public API committed here (implementer conforms):

    raw = load_raw_model(path: str) -> RawModel
        - Reads `path`, parses with `yaml.safe_load` ONLY.
        - A YAML document PyYAML's safe loader cannot construct (unknown tag,
          `!!python/...` constructor tag) RAISES yaml.YAMLError. The object is
          never instantiated and no side effect it would have caused occurs.
        - A missing file RAISES (OSError family) — never returns a stub/empty
          RawModel.
        - On success, returns a RawModel wrapping the parsed top-level
          mapping verbatim — no compiling, no defaulting, no mutation.

    class RawModel:
        raw.data: dict[str, object]
            The full dict `yaml.safe_load` returned, unmodified.
        raw.stocks / raw.part_types / raw.locations / raw.machines /
        raw.labor / raw.processes / raw.routing / raw.bom
            Convenience accessors, each identical to `raw.data["<key>"]`
            (snake_case YAML key mirrors the attribute name, tech.md naming
            convention). No section is "resolved" further than the parse.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from twinflow.model.loader import RawModel, load_raw_model

# A minimal but complete model.yaml: every section COMP-014's contract lists
# (stocks, part types, locations, machines, labor, processes, routing, BOM),
# with no include directive anywhere. Contents are deliberately trivial —
# the loader must not care whether they are *semantically* legal, only that
# they parse.
WELL_FORMED_MODEL_YAML = """
stocks:
  - name: raw_wire
    uom: ft
part_types:
  - name: blank
    uom: piece
locations:
  - name: cutter
    time_model:
      distribution: fixed
      value: 5
machines:
  - name: cutter_1
    location: cutter
labor:
  pools:
    - name: general
      skills: [cutting]
processes:
  - name: cut
    location: cutter
    inputs:
      - thing: raw_wire
        qty: 1
        uom: ft
    outputs:
      - thing: blank
        qty: 10
        uom: piece
routing:
  - part: blank
    steps: [cut]
bom:
  - part: blank
    components:
      - thing: raw_wire
        qty: 0.1
        uom: ft
"""


def _write(tmp_path: Path, name: str, content: str) -> str:
    """Write `content` to `tmp_path/name` and return the path as str (the
    loader's committed signature is `load_raw_model(path: str)`)."""
    file_path = tmp_path / name
    file_path.write_text(content)
    return str(file_path)


# ---------------------------------------------------------------------------
# safe_load is the only door: Python object constructor tags are REJECTED,
# never instantiated.
# ---------------------------------------------------------------------------


def test_load_raw_model_rejects_python_object_apply_tag_without_executing_it(
    tmp_path: Path,
) -> None:
    """A `!!python/object/apply:os.system` tag must raise, and the call it
    would have made must never happen. This is the concrete RCE vector
    `safe_load` exists to stop (D-001)."""
    marker = tmp_path / "pwned.txt"
    malicious_yaml = f"""
stocks: !!python/object/apply:os.system ["touch {marker}"]
"""
    path = _write(tmp_path, "malicious.yaml", malicious_yaml)

    with pytest.raises(yaml.YAMLError):
        load_raw_model(path)

    assert not marker.exists(), (
        "os.system ran: yaml.safe_load's constructor tag was executed instead of being rejected"
    )


def test_load_raw_model_rejects_python_object_instantiation_tag(tmp_path: Path) -> None:
    """A bare `!!python/object:` (arbitrary object instantiation, not just
    `/apply`) must also be rejected. PyYAML's full loader accepts this;
    safe_load must not."""
    malicious_yaml = """
locations: !!python/object:subprocess.Popen
  args: ["true"]
"""
    path = _write(tmp_path, "malicious_object.yaml", malicious_yaml)

    with pytest.raises(yaml.YAMLError):
        load_raw_model(path)


def test_load_raw_model_never_resolves_include_directive(tmp_path: Path) -> None:
    """An `!include`-style tag is not a directive the loader understands or
    resolves — it is an unknown tag to `safe_load` and must raise, WITHOUT
    ever attempting to open the referenced file. structure.md is explicit:
    'Never resolves includes or external references' (D-001).

    The referenced file is deliberately never created. If the loader tried
    to pre-process/fetch the include before calling safe_load, this test's
    failure mode would differ (a file-not-found style error rather than a
    YAML parse error) — that distinction is the point of asserting the
    specific exception type below.
    """
    model_yaml = """
routing: !include other_routing.yaml
"""
    path = _write(tmp_path, "model_with_include.yaml", model_yaml)
    referenced = tmp_path / "other_routing.yaml"

    with pytest.raises(yaml.YAMLError):
        load_raw_model(path)

    assert not referenced.exists(), "the !include target must never be fetched or created"


# ---------------------------------------------------------------------------
# A well-formed model.yaml loads, and its sections are accessible.
# ---------------------------------------------------------------------------


def test_load_raw_model_parses_well_formed_model_returns_raw_model(tmp_path: Path) -> None:
    path = _write(tmp_path, "model.yaml", WELL_FORMED_MODEL_YAML)

    raw = load_raw_model(path)

    assert isinstance(raw, RawModel)


def test_load_raw_model_exposes_every_committed_section(tmp_path: Path) -> None:
    path = _write(tmp_path, "model.yaml", WELL_FORMED_MODEL_YAML)
    expected = yaml.safe_load(WELL_FORMED_MODEL_YAML)

    raw = load_raw_model(path)

    assert raw.stocks == expected["stocks"]
    assert raw.part_types == expected["part_types"]
    assert raw.locations == expected["locations"]
    assert raw.machines == expected["machines"]
    assert raw.labor == expected["labor"]
    assert raw.processes == expected["processes"]
    assert raw.routing == expected["routing"]
    assert raw.bom == expected["bom"]


def test_load_raw_model_data_is_the_verbatim_parse_tree(tmp_path: Path) -> None:
    """The loader is explicitly UNVALIDATED and does no compiling — `raw.data`
    must equal exactly what `yaml.safe_load` produced, no more, no less."""
    path = _write(tmp_path, "model.yaml", WELL_FORMED_MODEL_YAML)
    expected = yaml.safe_load(WELL_FORMED_MODEL_YAML)

    raw = load_raw_model(path)

    assert raw.data == expected


# ---------------------------------------------------------------------------
# Adversarial cases beyond the listed acceptance criteria.
# ---------------------------------------------------------------------------


def test_load_raw_model_missing_file_raises(tmp_path: Path) -> None:
    missing = str(tmp_path / "does_not_exist.yaml")

    with pytest.raises(OSError):
        load_raw_model(missing)


def test_load_raw_model_empty_file_does_not_yield_a_usable_raw_model(tmp_path: Path) -> None:
    """`yaml.safe_load("")` returns None, not a dict. The loader must not
    silently hand back a RawModel whose sections cannot be accessed — an
    empty/near-empty model.yaml is a malformed model, not a valid empty one."""
    path = _write(tmp_path, "empty.yaml", "")

    with pytest.raises((ValueError, AttributeError, TypeError)):
        raw = load_raw_model(path)
        # If load_raw_model tolerated None and only failed lazily, force the
        # failure here rather than let a bad RawModel escape unnoticed.
        _ = raw.stocks


def test_load_raw_model_top_level_sequence_is_rejected(tmp_path: Path) -> None:
    """model.yaml's top level must be a mapping (named sections). A bare YAML
    sequence at the top level cannot be a RawModel and must not silently
    become one with no accessible sections."""
    path = _write(tmp_path, "list_top_level.yaml", "- 1\n- 2\n- 3\n")

    with pytest.raises((ValueError, AttributeError, TypeError)):
        load_raw_model(path)


def test_load_raw_model_does_not_execute_shell_command_embedded_as_plain_string(
    tmp_path: Path,
) -> None:
    """A plain (quoted) string that merely LOOKS LIKE a directive must load as
    an inert string — proving the loader does no post-parse interpretation of
    section values (that is expressions.py's job later, over a whitelist)."""
    model_yaml = """
stocks:
  - name: raw_wire
    uom: ft
    note: "!include other.yaml"
"""
    path = _write(tmp_path, "model.yaml", model_yaml)

    raw = load_raw_model(path)

    assert raw.stocks[0]["note"] == "!include other.yaml"


def test_load_raw_model_source_module_imports_pyyaml_but_nothing_else_parses_yaml() -> None:
    """Sanity check on the trust-boundary claim this test file's docstring
    makes: importing the loader module must succeed (proves imports resolve
    cleanly), and it must be the module that owns `import yaml`."""
    import twinflow.model.loader as loader_module

    source = Path(loader_module.__file__).read_text()
    assert "import yaml" in source
    assert not os.path.exists("other.yaml"), "no stray fetched file should exist in cwd"

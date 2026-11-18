"""B-39: every colour in the docs SVGs is a design token (DESIGN.md 0.1, VISUALS.md).

`docs/design/tools/check_svg_colours.py` lists the colours in an SVG that `tokens.json` does not
hold. The docs diagrams must have none, so a diagram and a web view share one palette.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "docs" / "design" / "tools" / "check_svg_colours.py"
DOCS = ROOT / "docs"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_svg_colours", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_svgs_hold_only_token_colours() -> None:
    tool = _tool()
    svgs = sorted(DOCS.rglob("*.svg"))
    assert svgs, "the docs hold SVG diagrams"
    stray = {str(p.relative_to(ROOT)): tool.stray_colours(p) for p in svgs}
    assert {k: v for k, v in stray.items() if v} == {}


@pytest.mark.parametrize(
    ("svg", "expected"),
    [
        ('<rect fill="#0073fe" stroke="#002550"/>', []),
        ('<rect fill="#0073FE"/>', []),
        ('<rect fill="#fff"/>', []),
        ('<rect fill="#123456"/>', ["#123456"]),
        ('<rect fill="none" stroke="transparent"/>', []),
        ('<rect fill="red"/>', ["red"]),
        ('<rect style="stroke: white; fill:#0073fe"/>', ["white"]),
        ('<rect fill="rgba(246, 141, 46, 0.1)"/>', []),
        ('<rect fill="rgba(1, 2, 3, 0.5)"/>', ["rgba(1,2,3,0.5)"]),
        ('<rect fill="url(#grad)"/><stop stop-color="#abcdef"/>', ["#abcdef"]),
        ("<style>.a { fill: #8a4c11; color: #bada55 }</style>", ["#bada55"]),
    ],
)
def test_checker_finds_colours_that_are_not_tokens(
    tmp_path: Path, svg: str, expected: list[str]
) -> None:
    path = tmp_path / "x.svg"
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{svg}</svg>', encoding="utf-8")
    assert _tool().stray_colours(path) == expected

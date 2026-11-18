"""List the colours in docs SVGs that are not in the design tokens (TF-006 B-39).

    uv run python docs/design/tools/check_svg_colours.py            # every SVG under docs/
    uv run python docs/design/tools/check_svg_colours.py a.svg b.svg

The allowed colours are every colour value in `web/src/decision/tokens.json` (light and dark).
Hex colours match in any case, and `#fff` means `#ffffff`. `rgb()` and `rgba()` match when they
equal a token exactly. `none`, `transparent`, `currentColor`, `inherit` and `url(...)` are not
colours. Any other colour word, such as `white`, is reported: use the token hex instead.
Exit status is 1 when any colour is reported, so CI can run it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOKENS = ROOT / "web" / "src" / "decision" / "tokens.json"
HEX = re.compile(r"#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
FUNCTION = re.compile(r"rgba?\([^)]*\)", re.IGNORECASE)
PAINT_WORD = re.compile(
    r"(?<![\w-])(?:fill|stroke|stop-color|flood-color|lighting-color|color)\s*[=:]\s*[\"']?([A-Za-z]+)(?![\w(-])"
)
NOT_COLOURS = {"none", "transparent", "currentcolor", "inherit", "url", "initial", "unset"}


def _hex(value: str) -> str:
    value = value.lower()
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    return value


def _function(value: str) -> str:
    return re.sub(r"\s+", "", value.lower())


def allowed() -> set[str]:
    """Every colour in `tokens.json`, in the form `colours_in` returns."""
    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    values = {v for mode in ("light", "dark") for v in tokens[mode].values() if isinstance(v, str)}
    return {
        _hex(v) if v.startswith("#") else _function(v) for v in values if v.startswith(("#", "rgb"))
    }


def colours_in(text: str) -> list[str]:
    """Every colour an SVG text writes, in the order it first appears, without repeats."""
    found = [(m.start(), _hex(m.group())) for m in HEX.finditer(text)]
    found += [(m.start(), _function(m.group())) for m in FUNCTION.finditer(text)]
    found += [
        (m.start(1), m.group(1).lower())
        for m in PAINT_WORD.finditer(text)
        if m.group(1).lower() not in NOT_COLOURS
    ]
    return list(dict.fromkeys(colour for _, colour in sorted(found)))


def stray_colours(path: Path) -> list[str]:
    """The colours in one SVG file that the tokens do not hold."""
    ok = allowed()
    return [c for c in colours_in(path.read_text(encoding="utf-8")) if c not in ok]


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted((ROOT / "docs").rglob("*.svg"))
    bad = 0
    for path in paths:
        stray = stray_colours(path)
        if stray:
            bad += len(stray)
            print(f"{path}: {', '.join(stray)}")
    print(f"{len(paths)} SVG files checked, {bad} colours not in tokens.json")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

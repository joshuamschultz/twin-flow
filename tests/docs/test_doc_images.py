"""Every image a docs page shows must exist and stay small enough to load fast."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 400 * 1024
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
REMOTE_PREFIXES = ("http://", "https://", "data:")


def _doc_pages() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def _local_image_links() -> list[tuple[Path, str]]:
    links: list[tuple[Path, str]] = []
    for page in _doc_pages():
        text = page.read_text(encoding="utf-8")
        for pattern in (MARKDOWN_IMAGE, HTML_IMAGE):
            for target in pattern.findall(text):
                if not target.startswith(REMOTE_PREFIXES):
                    links.append((page, target.split("#", 1)[0]))
    return links


LINKS = _local_image_links()


def test_docs_have_image_links_to_check() -> None:
    assert LINKS, "found no local image links in README.md or docs/; the parser is broken"


@pytest.mark.parametrize(
    ("page", "target"),
    LINKS,
    ids=[f"{page.relative_to(ROOT)}:{target}" for page, target in LINKS],
)
def test_doc_image_exists_and_is_small(page: Path, target: str) -> None:
    image = (page.parent / target).resolve()
    assert image.is_file(), f"{page.relative_to(ROOT)} links to a missing image: {target}"
    size = image.stat().st_size
    assert size <= MAX_IMAGE_BYTES, (
        f"{image.relative_to(ROOT)} is {size // 1024} KB; keep doc images under "
        f"{MAX_IMAGE_BYTES // 1024} KB (see docs/design/VISUALS.md)"
    )

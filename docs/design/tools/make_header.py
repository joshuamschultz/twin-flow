"""Make one docs header image with Google's Nano Banana (Gemini image) model.

Usage:
    uv run --no-project --with pillow python docs/design/tools/make_header.py \
        <slug> "<subject prompt>" [--out DIR] [--model ID] [--keep-raw]

The script adds the shared style block (see docs/design/VISUALS.md) to the subject,
calls the Gemini API, crops the result to 10:3, and saves
docs/images/headers/<slug>.webp at 1600x480 under 300 KB.

The API key is read from the GEMINI_API_KEY environment variable. It is sent only in
the request header. It is never printed and never written to a file. The request body
holds the image prompt and nothing else.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-nano-banana-2.1"  # "Nano Banana 2.1"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "images" / "headers"
TARGET_SIZE = (1600, 480)
MAX_BYTES = 300 * 1024
REQUEST_TIMEOUT_SECONDS = 180
ATTEMPTS = 3

STYLE_BLOCK = (
    "STYLE (same for every image in this series): a wide, calm, modern industrial "
    "banner illustration. Flat deep navy background, colour #001a38, darkening "
    "slightly to #05080f at the far left and right edges. The subject is a precise "
    "isometric technical drawing made of thin, clean, luminous line work in azure "
    "blue (#0073fe and #5a9cff) with a soft cyan (#22d3ee) glow, and "
    "semi-transparent glass-like wireframe volumes. A faint blueprint dot grid lies "
    "on the floor plane. Exactly one small warm orange (#f68d2e) accent marks the "
    "focal point; there is no other warm colour. Engineered, minimal and exact, with "
    "a lot of empty negative space. No photographic realism, no lens flare, no "
    "clutter, no gradients of purple or green. "
    "COMPOSITION: the whole subject sits in the middle horizontal band of the frame, "
    "small and centred, with wide empty navy margins above and below, because the "
    "image is cropped to a very wide 10:3 strip. Nothing important touches the top "
    "or bottom third. "
    "STRICT RULES: the image contains no text of any kind: no letters, no numbers, "
    "no labels, no captions, no signs, no symbols that look like writing. No logos, "
    "no brand marks, no watermarks. No people, no faces, no hands, no robots with "
    "faces."
)


def build_prompt(subject: str) -> str:
    return f"SUBJECT: {subject.strip()}\n\n{STYLE_BLOCK}"


def request_image(prompt: str, model: str, api_key: str) -> bytes:
    """Return the raw image bytes Gemini generated for `prompt`."""
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "4:1"},
            },
        }
    ).encode("utf-8")
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", model):
        raise RuntimeError(f"'{model}' is not a model id")
    # The URL is the fixed https API root plus a checked model id.
    request = urllib.request.Request(  # noqa: S310
        f"{API_ROOT}/{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    last_error = "no attempt made"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}"
            if error.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = f"network error: {error}"
        else:
            for candidate in payload.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"])
            last_error = f"no image in the response: {json.dumps(payload)[:300]}"
        time.sleep(4 * attempt)
    raise RuntimeError(f"Gemini did not return an image ({last_error})")


def crop_and_compress(raw: bytes, destination: Path) -> int:
    """Centre-crop to 10:3, resize to 1600x480, save WebP under MAX_BYTES. Return the size."""
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - depends on the caller's environment
        raise SystemExit(
            "Pillow is needed to resize the image. Run this script with:\n"
            "  uv run --no-project --with pillow python docs/design/tools/make_header.py ..."
        ) from error

    image = Image.open(io.BytesIO(raw)).convert("RGB")
    width, height = image.size
    target_ratio = TARGET_SIZE[0] / TARGET_SIZE[1]
    crop_height = min(height, round(width / target_ratio))
    crop_width = min(width, round(crop_height * target_ratio))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    image = image.crop((left, top, left + crop_width, top + crop_height))
    image = image.resize(TARGET_SIZE, Image.LANCZOS)

    destination.parent.mkdir(parents=True, exist_ok=True)
    for quality in (86, 80, 72, 64, 56, 48):
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=6)
        if buffer.tell() <= MAX_BYTES:
            break
    destination.write_bytes(buffer.getvalue())
    return buffer.tell()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug", help="page slug; the file is saved as <slug>.webp")
    parser.add_argument("subject", help="what the picture shows, in one or two sentences")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="output folder")
    parser.add_argument("--model", default=os.environ.get("TWINFLOW_IMAGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--keep-raw", action="store_true", help="also save the uncropped PNG")
    arguments = parser.parse_args(argv)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is not set.", file=sys.stderr)
        return 2

    try:
        raw = request_image(build_prompt(arguments.subject), arguments.model, api_key)
    except RuntimeError as error:
        print(f"{arguments.slug}: {error}", file=sys.stderr)
        return 1
    if arguments.keep_raw:
        arguments.out.mkdir(parents=True, exist_ok=True)
        (arguments.out / f"{arguments.slug}.raw.png").write_bytes(raw)
    destination = arguments.out / f"{arguments.slug}.webp"
    size = crop_and_compress(raw, destination)
    print(f"{destination} {size // 1024} KB ({arguments.model})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

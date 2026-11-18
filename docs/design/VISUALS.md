# The docs visual system

This page sets the look of every picture in the twinflow docs. It covers two kinds of
picture: the header image at the top of a page, and the diagrams inside a page.

The colors and type come from the web workspace theme (BlackArc, the token layer in
`web/src/index.css`). The docs do not add new brand colors.

## Principles

- **Navy is the ground. Azure is the pointer.** Orange marks one thing only.
- **Calm and exact.** Thin lines, sharp corners, a lot of empty space.
- **One style for all pages.** A reader must see at once that two pages belong together.
- **A picture must explain something.** If the words are clear without it, leave it out.

## Palette

The same names are used in `web/src/index.css`.

### Light (diagrams, light pages)

| Role | Color | Use |
|---|---|---|
| Page | `#f6f8fb` | Diagram background |
| Surface | `#ffffff` | Cards and boxes |
| Tint | `#eef4ff` | Soft fill for a chip or a highlighted area |
| Tint strong | `#d6e6ff` | Fill for the main node, the "Have" tag |
| Border | `#dfe3e8` | Outer frame |
| Rule | `#c7d6e8` | Card outline |
| Headline | `#002550` | Titles (navy) |
| Body | `#243042` | Normal text |
| Muted | `#54585c` | Notes and captions |
| Accent | `#0073fe` | The one thing to look at (azure) |
| Line | `#0055bc` | Arrows, links, code text `#003b82` |
| Quiet line | `#8fa6c4` | Tree lines, axes, "not yet" outlines |

### Dark (header images, dark pages)

| Role | Color | Use |
|---|---|---|
| Ground | `#001a38` | Header background (navy 900) |
| Edge | `#05080f` | Far left and right of a header |
| Surface | `#0f1e38` | Cards on a dark page |
| Rule | `#2e4c82` | Card outline on dark |
| Headline | `#ffffff` | Titles |
| Body | `#c7d6e8` | Normal text |
| Accent | `#5a9cff` | Line work, links (azure 300) |
| Glow | `#22d3ee` | Soft glow, "live" (cyan) |

### Status colors

Blue means good. Orange means look here. The palette has no green and no red.

| Meaning | Fill | Line | Text |
|---|---|---|---|
| Good, won, ahead, built today | `#eef4ff` or `#d6e6ff` | `#0073fe` | `#0055bc` or `#003b82` |
| Warning, not proven, behind, needs more | `#fdf1e4` | `#f68d2e` | `#8a4c11` |
| New, to build, live | `#d5f6fb` | `#22d3ee` | `#0b6b7a` |
| Later, not there yet | `#ffffff` | `#8fa6c4`, dashed | `#54585c` |

Do not put orange text `#f68d2e` on a light ground. It is too faint to read. Use
`#8a4c11` for the text.

## Type

| Use | Font | Fallback |
|---|---|---|
| Diagram title | Space Grotesk 600, 20px, navy | Inter, system sans |
| Box title | Inter 600, 14px, navy | system sans |
| Body | Inter 400, 13px | system sans |
| Note | Inter 400, 12px, muted | system sans |
| File names, commands, numbers | JetBrains Mono 500, 12px | SFMono, Menlo, Consolas |

- A reader's computer may not have these fonts. Leave spare room in every box, about
  15% of the box width.
- No text smaller than 11px at 800px wide.
- Sentence case. No all-caps words.
- Header images hold no text at all.

## Header images

Each page has one header image at the top, under the title.

- **Style:** an isometric technical drawing on flat navy. Thin glowing azure lines,
  glass-like wireframe shapes, a faint dot grid on the floor, a soft cyan glow.
- **Accent:** exactly one small orange part. It marks what the page is about.
- **Subject:** one clear object or scene from the plant. Small and centered, with
  empty navy around it.
- **Never:** text, numbers, labels, logos, real brands, people, faces, hands, photos.
- **Size:** 1600 x 480 pixels (10:3), WebP, under 300 KB. Most are 20 to 60 KB.
- **Words:** say "factory", not "plant". The image model draws a potted plant.

The README keeps its own hero image (`docs/header.webp`). It is the only image with the
twinflow word mark.

### How to make one

The tool is `docs/design/tools/make_header.py`. It reads the key from the
`GEMINI_API_KEY` environment variable. It sends the image prompt and nothing else.

```bash
uv run --no-project --with pillow python docs/design/tools/make_header.py \
    how-to-race-your-policies \
    "Five parallel straight conveyor lanes side by side like a race track, each carrying one identical crate. The orange accent is the crate that leads."
```

- The first argument is the slug. The file is saved as
  `docs/images/headers/<slug>.webp`.
- The second argument is the subject, in one to three sentences. End with a sentence
  that names the one orange part.
- The subject of every header is kept in `docs/design/tools/header_subjects.tsv`. Add a
  row when you add a page.
- The model is `gemini-nano-banana-2.1` (Nano Banana 2.1). Use `--model` to change it.
- The tool asks for a 4:1 image, cuts the middle 10:3 part, and saves it at 1600 x 480.

Look at each image before you commit it. Make it again when it has any text, when
part of the subject is cut off, when it has more than one orange part, or when it
looks like a stock picture.

### The prompt template

The prompt is the subject, then one fixed style block. The style block is the same for
every header. It lives in `make_header.py` as `STYLE_BLOCK`. Do not change it for one
page.

```text
SUBJECT: <what the picture shows, one to three sentences. The orange accent is <one part>.>

STYLE (same for every image in this series): a wide, calm, modern industrial banner
illustration. Flat deep navy background, colour #001a38, darkening slightly to #05080f
at the far left and right edges. The subject is a precise isometric technical drawing
made of thin, clean, luminous line work in azure blue (#0073fe and #5a9cff) with a soft
cyan (#22d3ee) glow, and semi-transparent glass-like wireframe volumes. A faint
blueprint dot grid lies on the floor plane. Exactly one small warm orange (#f68d2e)
accent marks the focal point; there is no other warm colour. Engineered, minimal and
exact, with a lot of empty negative space. No photographic realism, no lens flare, no
clutter, no gradients of purple or green. COMPOSITION: the whole subject sits in the
middle horizontal band of the frame, small and centred, with wide empty navy margins
above and below, because the image is cropped to a very wide 10:3 strip. Nothing
important touches the top or bottom third. STRICT RULES: the image contains no text of
any kind: no letters, no numbers, no labels, no captions, no signs, no symbols that
look like writing. No logos, no brand marks, no watermarks. No people, no faces, no
hands, no robots with faces.
```

## Diagrams

A diagram is a hand-written SVG file. It has its own light background, so it reads
well on a light page and on a dark page.

- **Canvas:** 800 units wide. The height fits the content. Outer frame `#f6f8fb` with a
  1px `#dfe3e8` border and 4px corners.
- **Margins:** 36px on the left and right. The title sits at 44px from the top, the
  one-line subtitle at 66px.
- **Boxes:** white fill, 1.5px `#c7d6e8` outline, 4px corners. Chips have 2px corners.
- **The main box:** tint fill `#eef4ff` and a 2px azure `#0073fe` outline. One per
  diagram, or one group.
- **"Not yet" boxes:** dashed 1.5px `#8fa6c4` outline.
- **Lines and arrows:** 1.5px `#0055bc`. One small filled triangle at the end. Lines
  run straight or with one soft curve. Arrows do not cross text.
- **Dashed blue lines** mean "the same thing is passed on".
- **Tree and axis lines:** 1 to 1.5px `#8fa6c4`.
- **Circles** are states. **Rectangles** are files, steps and things.
- **Numbers** in mono. Real numbers from the page where the page has them. Mark made-up
  numbers with the words "Example numbers".
- **No shadows, no gradients, no icons from a brand.**
- Every SVG has a `<title>` and a `<desc>`.
- At most two diagrams on one page.

### How to check one

```bash
rsvg-convert -w 800 docs/images/diagrams/<name>.svg -o /tmp/<name>.png
```

Open the PNG. Check that no text runs out of its box, and that every word is easy to
read at this size.

## Sizes and file names

| Kind | Folder | Name | Format | Size | Limit |
|---|---|---|---|---|---|
| Page header | `docs/images/headers/` | `<page>.webp`, or `how-to-<page>.webp` | WebP | 1600 x 480 | 300 KB |
| Index header | `docs/images/headers/` | `docs-index.webp`, `how-to-index.webp` | WebP | 1600 x 480 | 300 KB |
| Diagram | `docs/images/diagrams/` | `<what-it-shows>.svg` | SVG | 800 wide | 50 KB |
| Architecture map | `docs/architecture/` | `NN-<name>.svg` | SVG | as drawn | 400 KB |
| README hero | `docs/` | `header.webp` | WebP | 1800 wide | 400 KB |

- Names are lower case, with hyphens, and no spaces.
- A diagram is named for what it shows, not for the page it is on.
- The test `tests/docs/test_doc_images.py` fails when a linked image is missing or is
  over 400 KB.

## Alt text

- Say what the picture shows, in one plain sentence. Aim for under 160 characters.
- For a diagram, say the point it makes, not only its parts.
- Do not start with "Image of" or "Picture of".
- Do not repeat the page title.
- No period at the end. No quotation marks.

Good: `A balance scale weighing two cubes, with one pan clearly lower`

Not good: `Header image for the improve a policy page`

## Where a picture goes

- The header goes directly under the page title.
- A diagram goes directly under the heading it explains, or just above the steps it
  prepares the reader for.
- A page made by a script has no header. `docs/how-to/tune.md` is one. A test compares
  that page with the settings table, so a hand edit breaks the test.

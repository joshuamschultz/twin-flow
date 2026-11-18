# B-25 review: race view against the mockup

Date: 2026-11-18. Branch: `feat/TF-006-ui-integrate`. Reviewer: Opus.

What was compared: `docs/design/mockups/race-report-{1280,820}-{light,dark}.png` against
`web/tests/browser/__screens__/race-full-{1280,820}-{light,dark}.png` (full page) and
`race-first-*.png` (first screen). The screenshots come from
`web/tests/browser/report/gate.spec.ts`, which fills the offline template with
`docs/design/race-view-model.full.json` (the real molding-press race, 40 runs, seed 0, with the
180-day check).

## Result

The race view matches the mockup in layout, order, words and colour in both themes and both
widths. Every section in the mockup is in place except Tuning and the full Details. Those are
B-23 and B-24 on `feat/TF-006-ui-details` and merge next. Until then Details shows a short
"on the way" box that names them.

## Gate checks

| Check | Result |
|---|---|
| Interaction suite (`gate.spec.ts`, `report/*.spec.ts`, `statemap/race-states.spec.ts`) | Pass |
| Section bar order: Decision, Why it wins, All rules, Compare two, What if, Floor replay, State map, Money leaks, Details | Pass (Tuning joins with B-23) |
| URL state: `a`, `b`, `day`, `state` restore from a link and survive a reload | Pass |
| Keyboard only: theme toggle, section links, state map select | Pass |
| axe (wcag2a, 2aa, 21aa, 22aa) on the whole page, light and dark | Pass after two fixes (below) |
| Offline: file:// with every non-file request blocked, no console errors | Pass |
| Size: full race file under 1.2 MB | Pass (template 400 KB) |

## Differences and what happened to each

| # | Difference from the mockup | Decision |
|---|---|---|
| 1 | Hero leads with **+$109,000 more profit per year, from a 180-day check**. The mockup leads with +$22,088 per run and +$134,400 per year | **Changed on purpose.** Owner decision. Per run is the second figure. The 60-day guess is secondary text: "Quick math from the 60-day runs says +$134,400 a year." Recorded in DESIGN.md 3.1 |
| 2 | Caution card said "about $108,983 a year" while the hero said +$109,000 | **Fixed.** The long-check words now round to $100 like every yearly figure (`report_model.horizon_check`) |
| 3 | axe: the blue Pick tag was 4.3:1 (white on #0073fe at 10 px) | **Fixed.** New token `--tf-pick-solid` (#0055bc light, #5a9cff dark), 6.9:1 and 6.3:1 |
| 4 | axe: replay fact labels were 3.7:1 (`--ba-meta` on the grey fact box) | **Fixed.** Labels use `--ba-body` |
| 5 | Tuning section and full Details (run facts, full table, download) are missing | **Waits for B-23/B-24** on `feat/TF-006-ui-details`. Placeholder names them |
| 6 | "What changes on the floor" has five rows; the mockup shows four | **Stays.** B-08 allows four or five. The fifth row (sales lost per run) comes from the data |
| 7 | "Show as table" links under the run dots and every chart | **Stays.** Required by DESIGN.md 8.4. The mockup leaves them out |
| 8 | Caution cards come in a different order ("The gain comes late" first) and the late-gain card adds the 180-day sentence | **Stays.** Order and words come from Python `cautions` (rule order, tested in `test_cautions_in_rule_order`). The 180-day sentence is the honesty rule |
| 9 | Caution line words: "The gain comes late. It is behind today's rule until day 54 of 60." The mockup says "shows up late" | **Stays.** Python copy, same meaning, shorter |
| 10 | What if: "Reset to my costs" sits in the sliders card under a "Set the costs" title, not in the section head | **Stays.** The button sits next to what it resets and never wraps away at 820 |
| 11 | Floor replay opens on day 1; the mockup shows day 9 | **Stays.** Day 9 in the mockup only shows a busy day. A link with `day=9` opens there |
| 12 | Compare row "Changeovers to another family" where the mockup says "Colour changes per run" | **Stays.** The label comes from the project's family names, so it works for any plant, not only colours |
| 13 | State map: the Pick / Today switch sits above the card, not at the right of the section head | **Stays.** It stays next to the map at 820 and the panel follows it |
| 14 | Money leaks hint adds "Click a row to see it on the map." | **Stays.** It says what the row does |
| 15 | "When does it pull ahead" y axis runs to about -$30k; the mockup shows -$20k | **Stays.** The axis fits the real average line. The mockup drew it by hand |
| 16 | Page is shorter (6,353 px vs 7,522 px at 1280) | **Stays.** The gap is the Tuning and Details sections in item 5 |

Nothing in the list blocks the gate. The owner signs off on items 1 and 5 when B-23/B-24 merge.

## Close-out check (B-42)

Date: 2026-11-18. Branch: `feat/TF-006-final-gate`. Reviewer: Opus.

Built a fresh race with `twinflow policies race examples/molding-press --episodes 20` and
took full-page shots at 1280 and 820, light and dark, with Playwright. Compared them with
`docs/design/mockups/race-report-*.png`. No console errors in any of the four.

| # | Item | Now |
|---|---|---|
| 5 | Tuning and full Details were missing | **Done.** Both sections are in the page, in the mockup's order. The section bar ends with Tuning, Details |
| 16 | Page was shorter than the mockup | **Done.** 8,602 px at 1280 (mockup 7,522). The extra height is the "What is information worth?" card and the Problems card, which the mockup leaves out |
| 17 | New: at 1280 the full table was 81 px wider than its card, so the Source column hid behind a side scroll | **Fixed.** Header words wrap and cells sit closer in the full table (`details.css`). Test: `details.spec.ts` "at 1280 the full table fits its card". Narrow pages still scroll inside the card |
| 18 | Floor card says "Fewer orders find an empty shelf."; the mockup says "customers" | **Stays.** Copy pass wording (buyers are businesses; grade 6) |

Signed off by the lead, 2026-11-18. Items 1 and 5 are closed. Nothing open.

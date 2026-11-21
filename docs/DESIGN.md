# VeriMend: design

This file governs the replay site in `site/`. The CLI and the pull-request body follow the writing rules at the end.

## The idea: the inspection record

In aircraft maintenance, some repairs cannot be signed off by the mechanic who did them. A second person inspects the work and stamps the record. VeriMend works the same way: the agent repairs, the verifier signs off.

So the site is an inspection record. It has two things on it: a route map of the pipeline, and an inspector's stamp. Everything else stays quiet.

**Audience.** A hiring manager or founder with two minutes, and a data engineer deciding whether to trust the numbers.

**The site's job.** Show one real run from failure to verdict in under 30 seconds, then let the visitor check every number.

## The bar: references

The site should hold its own on [Awwwards](https://www.awwwards.com/websites/) and sit comfortably among the AI sites on [Minimal Gallery](https://minimal.gallery/tag/ai/) (Aditya, 2026-11-21). What we take from each:

- **From Minimal Gallery's AI sites:** one confident idea per screen; a very large headline with a lot of air around it; a single striking visual (here, the live route map) instead of a collage; real product output shown, not illustration; restraint in colour.
- **From Awwwards' sites of the day:** craft in motion. Lines draw themselves, the page scrolls smoothly, a long section pins while scroll tells its story, type is set at display sizes, numbers are set huge, pages change without a white flash, and a full-width wordmark closes the page.
- **What we do not take:** the clichés of both (see "Not this").

The concept stays the inspection record: the craft serves the replay and the numbers, never the other way round. Every animated thing is real data from a trace.

## Colour

| Token | Hex | Use |
| --- | --- | --- |
| `sheet` | `#EEF2F6` | Page background. A cool drafting-film grey, never white |
| `ink` | `#0F1B2D` | Text, graph lines, controls |
| `rule` | `#B7C2D0` | Hairlines, the graph's layer guides, table borders |
| `fail` | `#C8322B` | A failing node or check. Nothing else |
| `pass` | `#1F7A4D` | A node rebuilt and passing. Nothing else |
| `hold` | `#8F5500` | Escalated: a person needs to look |
| `stamp` | `#4B2FBF` | The verifier's voice: its stamp, its verdict line, its check list. Never used for links, buttons or decoration |
| `stamp-on-ink` | `#A99BFF` | The verifier's voice on an `ink` background (the verifier section). Same rule as `stamp` |
| `pass-on-ink` | `#5FD08F` | A passing check on an `ink` background |
| `fail-on-ink` | `#FF7B72` | A failing check on an `ink` background |

Rules:

- `stamp` violet appears only where the verifier speaks. A visitor should learn in one replay that violet means "the independent check said this".
- Status is never colour alone. Every state also has a word and a shape: a ring and "failed", a tick and "passing", a stamp and its text.
- No gradients, no shadows, no dark mode in version one.
- **One inverted section.** The verifier section is set `sheet` on `ink`, full bleed, so the independent check visibly stands apart from the agent's work. It uses the `-on-ink` tokens and nothing else.
- **Drafting-film grain.** The `sheet` background carries a static noise texture at 3% opacity (a tiled 128 px PNG, under 8 KB). It is a texture, not a gradient, and it never moves.
- Diffs use a 12% tint of `pass` for added lines and of `fail` for removed lines, with `+` and `-` kept in the gutter.

## Type

Two families from one designer, both on Google Fonts:

- **Overpass** for everything people read. It descends from US highway signage, which suits a route map.
- **Overpass Mono** for SQL, diffs, file paths, trace ids and commands. Not for labels, captions or numbers in prose.

| Role | Size and weight |
| --- | --- |
| Display (home headline, closing wordmark) | `clamp(3.25rem, 11vw, 10rem)`, weight 800, line-height 0.92, letter-spacing -0.045em |
| Figure (a headline number in the results) | `clamp(4rem, 14vw, 12rem)`, weight 800, tabular figures, line-height 0.9 |
| Page title | `clamp(2.5rem, 6vw, 4.5rem)`, weight 800, line-height 1.02, letter-spacing -0.02em |
| Section heading | 1.875rem, weight 700, line-height 1.15 |
| Sub-heading | 1.3125rem, weight 700 |
| Body | 1.0625rem, weight 400, line-height 1.55, max 68 characters a line |
| Small | 0.875rem, weight 400, line-height 1.45 |
| Code | Overpass Mono 0.9375rem, line-height 1.5 |

Headings are sentence case. No all-caps labels, no small label above a heading, no single word of a headline picked out in another colour or style. Numbers in tables are right-aligned with tabular figures; if Overpass does not provide them, set numeric columns in Overpass Mono.

## Layout

One column of record, left-aligned, with the graph allowed to run the full width of the page.

```
┌──────────────────────────────────────────────────────────────┐
│ VeriMend                                results  limits  how │
├──────────────────────────────────────────────────────────────┤
│ It doesn't say fixed until it's proven.                      │
│ One sentence on what this is.                                │
│                                                              │
│  sources ───── staging ───── intermediate ───── marts        │
│    ○──────────○───────────────○──────────────────○           │
│    ○──────────◉ failed ───────○──────────────────○           │
│    ○──────────○───────────────○                              │
│                                                  [VERIFIED]  │
│  ▶ play   step 7 of 19   ━━━━━●───────────   1×              │
├───────────────────────────────┬──────────────────────────────┤
│ Step log (advances with play) │ What the step returned       │
├───────────────────────────────┴──────────────────────────────┤
│ Results table                                                │
└──────────────────────────────────────────────────────────────┘
```

- The graph is the hero. It sits in the first screen at every width.
- Layers run left to right: sources, staging, intermediate, marts, each named once above its column on a `rule` guide line.
- Below 720 px the graph keeps its left-to-right shape and scrolls sideways inside its frame, with the active node kept in view. The step log and detail stack under it.
- Corners are square on the graph frame and tables, 4 px on buttons and inputs. That is the only radius.
- Content is not chopped into cards. Sections are separated by space and a single `rule` line.

**The home page, in order:**

1. **The record.** The display headline, one sentence under it, and the route map filling the rest of the first screen. The opening sequence plays (see Motion).
2. **The replay.** On screens 1024 px and wider the graph pins and scrolling advances the run step by step, with the step log beside it; the controls work too. Narrower, it is a tap-through stepper.
3. **The sign-off.** The inverted `ink` section: the four checks set large, one per line, each ticked or crossed as the stamp lands.
4. **The numbers.** The false-success rate as a Figure, VeriMend's beside the baseline's, each linked to its trace folder; the full results table under them.
5. **What it cannot fix.** Three short cases, linking to `/limits`.
6. **The close.** "VeriMend" as a Display wordmark across the full width, the repo link, and the date of the runs.

## The graph

- A model is a station: a 10 px circle with its name to the right in Small. Sources are open circles, models filled.
- Edges are 1.5 px `ink` at 55% opacity, with right-angle bends like a transit map.
- The failed model has a `fail` ring and the word "failed". After the verifier passes it gets a `pass` ring and a tick.
- The agent's path so far is drawn as a 3 px solid `ink` line over the edges it has walked. The node it is looking at now has a second ring.
- A blocked action is shown where it happened: a short bar across the node and a log line that reads "Blocked: tried to edit tests/assert_positive_amount.sql".
- Lay the graph out at build time so it never shifts while playing.

## Motion

Motion is where the site earns its place among the references. Every movement shows something the run actually did. One easing family throughout: `cubic-bezier(0.65, 0, 0.35, 1)` for movement along the map, `cubic-bezier(0.2, 0.9, 0.3, 1.25)` (a slight overshoot) only for the stamp.

**The opening (once per visit, at most 2.4 s, cut short by any input):**

- The map draws itself: each layer's guide line, then the edges left to right (stroke drawing, 900 ms), then the stations, staggered 20 ms apart.
- The failed station's `fail` ring pulses once.
- The headline's lines rise 24 px into place, 60 ms apart, behind a mask. This is the only text that moves.

**The replay:**

- The agent's path draws along the edges it walks, at the speed of play or of scroll.
- The node in focus gets its second ring; the camera eases to keep it in view on narrow screens.
- A blocked action strikes across its node with a short bar that draws in 120 ms.
- The running cost and time count up with the steps, never ahead of them.

**The sign-off, the one big moment:**

- Three stamps: **VERIFIED**, **NOT VERIFIED**, **ESCALATED**. The first two are `stamp` violet; ESCALATED is `hold`.
- The stamp is a rectangular outline with its text in weight 800, turned 4 degrees anticlockwise, placed at the right end of the graph.
- It lands in 220 ms: scale 1.4 to 1 with the overshoot easing, opacity 0 to 1. On impact a second outline, offset 1 px at 30% opacity, appears and stays, like ink from a rubber stamp.
- In the sign-off section the four checks tick or cross one after another, 90 ms apart, in time with the stamp.

**Around the page:**

- Smooth scrolling, with native scrolling kept for touch and for keyboard and screen-reader users.
- Page changes use view transitions: the route line and the wordmark stay in place while the content changes. No white flash.
- Links draw their underline from the left in 120 ms on hover and focus.
- Over the graph the pointer becomes a crosshair, and hovering a station shows its name, its layer and the steps that touched it.

**Still not allowed:** paragraphs fading in on scroll, parallax on text, hover lifts, a cursor that replaces the system pointer, a loading screen, anything that loops forever.

With reduced motion on, nothing moves on its own: the opening is skipped, the map and stamp appear in their final state, scrolling is native, autoplay is off and the replay is a stepper.

## Replay controls

Play and pause, step back and forward, a scrubber, and speed (1×, 2×, 4×). Keyboard: space to play or pause, left and right arrows to step, Home and End to jump. Focus is always visible as a 2 px `ink` outline with a 2 px offset.

Each step shows the tool called, a one-line summary of its arguments, what came back (trimmed, with "show all"), and the running cost and time.

## Results table

Rows are configurations (plain-agent baseline, VeriMend on Haiku 5.5, VeriMend on Sonnet 5.5, the ablations). Columns are fix rate, false success, caught by the verifier, correct escalation, tamper attempts, cost per incident and time per incident.

- Every number is a link to the trace folder it was computed from.
- Rates show the mean with the spread across trials beside it.
- The false-success column sits second and is the only one with a heavier weight.
- A line above the table says how many held-out tasks and trials it covers and the date of the run.

## Quality floor

- Works at 375, 1280 and 1600 px.
- Every colour pair used for text meets WCAG AA.
- The whole replay can be driven from the keyboard, and each step is announced to screen readers through a polite live region.
- The home page loads its first replay from a static file; no request blocks the first paint of the graph.
- **Performance budget:** Lighthouse 95 or more in all four categories on mobile; largest contentful paint under 1.8 s on a mid-range phone over 4G; no layout shift; under 180 KB of JavaScript (gzipped) on the first load; the replay holds 60 frames a second at 1280 px.
- **Libraries,** to be approved in the phase 7 plan (CLAUDE.md rule 10): GSAP with ScrollTrigger for the opening, the pinned replay and the stamp, and Lenis for smooth scrolling. The map stays SVG. No WebGL in version one: the map must stay sharp, accessible and fast, and the run itself is the spectacle.

## Writing

- "VeriMend" in prose, `verimend` in code.
- Say what happened in plain verbs: "The verifier rejected the fix because a test was deleted."
- Use the same word for the same thing everywhere: fixed, not fixed, escalated, verified, blocked.
- No "autonomous", "self-healing", "magic" or "AI-powered". Describe what it does.
- Buttons say what they do: "Play this run", "Open the trace", "See what it cannot fix".
- Errors name the problem and the next step: "This trace file is missing. Run scripts/build_site_data.py."
- The limits page is written as plainly as the results page.

## Not this

A dark terminal theme with a neon accent. A grid of rounded cards with soft shadows. Gradient washes. Small all-caps labels over headings. Monospace used for decoration. Arrows tacked onto link text. Any of these makes the site look like every other agent demo.

Nor the award-site clichés: a percentage counter on a loading screen, a blob or orb that follows the cursor, a marquee of logos, 3D shapes floating for no reason, text that scrambles before it settles, a hero video unrelated to the product.

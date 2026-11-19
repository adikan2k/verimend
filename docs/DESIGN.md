# VeriMend: design

This file governs the replay site in `site/`. The CLI and the pull-request body follow the writing rules at the end.

## The idea: the inspection record

In aircraft maintenance, some repairs cannot be signed off by the mechanic who did them. A second person inspects the work and stamps the record. VeriMend works the same way: the agent repairs, the verifier signs off.

So the site is an inspection record. It has two things on it: a route map of the pipeline, and an inspector's stamp. Everything else stays quiet.

**Audience.** A hiring manager or founder with two minutes, and a data engineer deciding whether to trust the numbers.

**The site's job.** Show one real run from failure to verdict in under 30 seconds, then let the visitor check every number.

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

Rules:

- `stamp` violet appears only where the verifier speaks. A visitor should learn in one replay that violet means "the independent check said this".
- Status is never colour alone. Every state also has a word and a shape: a ring and "failed", a tick and "passing", a stamp and its text.
- No gradients, no shadows, no dark mode in version one.
- Diffs use a 12% tint of `pass` for added lines and of `fail` for removed lines, with `+` and `-` kept in the gutter.

## Type

Two families from one designer, both on Google Fonts:

- **Overpass** for everything people read. It descends from US highway signage, which suits a route map.
- **Overpass Mono** for SQL, diffs, file paths, trace ids and commands. Not for labels, captions or numbers in prose.

| Role | Size and weight |
| --- | --- |
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

## The graph

- A model is a station: a 10 px circle with its name to the right in Small. Sources are open circles, models filled.
- Edges are 1.5 px `ink` at 55% opacity, with right-angle bends like a transit map.
- The failed model has a `fail` ring and the word "failed". After the verifier passes it gets a `pass` ring and a tick.
- The agent's path so far is drawn as a 3 px solid `ink` line over the edges it has walked. The node it is looking at now has a second ring.
- A blocked action is shown where it happened: a short bar across the node and a log line that reads "Blocked: tried to edit tests/assert_positive_amount.sql".
- Lay the graph out at build time so it never shifts while playing.

## The one moment: the sign-off

The replay ends with the verifier's stamp landing on the record. This is the only orchestrated motion on the site.

- Three stamps: **VERIFIED**, **NOT VERIFIED**, **ESCALATED**. The first two are `stamp` violet; ESCALATED is `hold`.
- The stamp is a rectangular outline with its text in weight 800, turned 4 degrees anticlockwise, placed at the right end of the graph.
- It lands in 180 ms: scale 1.15 to 1 and opacity 0 to 1, ease-out. Under it, the verifier's checks appear as a list, each with its result.
- Nothing else animates on its own. No fade-ins on scroll, no hover lifts.

Motion that answers the visitor is fine: the path drawing as they play or step, the detail pane swapping, the scrubber moving.

With reduced motion on, the replay becomes a stepper: the path and stamp appear without movement, and autoplay is off.

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

# PPT Harness — Design

Explicit design. Schemas, catalogs, signatures, and rules an implementation must satisfy.
For the shape of the system and why it's built this way, see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Deck model

### 1.1 Slide modes

| Mode | Origin | Geometry | Tool set |
|---|---|---|---|
| `managed` | Generated from components | Derived by the expander | Component tools |
| `freeform` | Imported, or ejected | Absolute EMU per shape | Semantic constraint tools |

Transitions: `eject_slide` (managed → freeform, one-way) and `adopt_slide` (freeform →
managed, requires classifier confidence **and** user confirmation). The router rejects a
tool call whose mode doesn't match the target slide.

### 1.2 Managed slide

```json
{
  "id": "sl_7a2f",
  "index": 6,
  "mode": "managed",
  "layout": "hero_plus_row",
  "blocks": [
    { "id": "bk_1", "region": "header", "component": "slide_title",
      "variant": "plain",
      "slots": { "title": "Q3 revenue held despite churn" } },

    { "id": "bk_2", "region": "hero", "component": "chart",
      "variant": "wide",
      "slots": { "chart": { "kind": "bar", "series": [...], "labels": [...] } } },

    { "id": "bk_3", "region": "footer_row", "component": "stat_row",
      "variant": "carded",
      "overrides": { "density": "compact" },
      "slots": { "items": [ { "value": "$4.2M", "label": "ARR" },
                            { "value": "-3.1%", "label": "Churn" } ] } }
  ],
  "notes": "Chart source: finance/q3.xlsx"
}
```

A slide is a **layout frame plus ordered blocks** — one component per slide cannot express
the hybrid slides real decks are made of.

### 1.3 Freeform slide

```json
{
  "id": "sl_9c11",
  "mode": "freeform",
  "origin": { "file": "board_deck.pptx", "part": "/ppt/slides/slide4.xml" },
  "shapes": [
    { "id": "sp_12", "ooxml_id": 5, "type": "textbox",
      "frame": { "x": 914400, "y": 457200, "cx": 5486400, "cy": 1143000 },
      "role": "title", "text": "FY26 outlook", "opaque": false, "dirty": false },
    { "id": "sp_13", "ooxml_id": 9, "type": "smartart",
      "frame": { "...": "EMU" }, "opaque": true, "dirty": false }
  ]
}
```

`opaque` shapes (SmartArt, groups, embedded objects) are preserved untouched. `dirty` is
what makes mutating export possible (§6.2).

### 1.4 Layout frames

| Layout | Regions |
|---|---|
| `title` | `hero` |
| `stack` | `header`, `body` |
| `two_col` | `header`, `left`, `right` |
| `hero_plus_row` | `header`, `hero`, `footer_row` |
| `full_bleed` | `canvas` |

Regions are column spans on the theme grid. Blocks flow into regions in order; the
expander owns all geometry.

**Within a block, slots stack unless they declare a width.** A `SlotSpec` carries a
`width_share` beside its `height_share`, and the expander groups *consecutive* slots into
horizontal **bands**: a slot joins the band being built while there is still room for its
share inside 1.0, and otherwise opens the next one. A band is as tall as its tallest member,
and shares are normalised across the band, so two halves fill the width even after the cell
gap comes out of it. Declaration order decides adjacency, which keeps the catalog readable —
`left` then `right` already reads as side by side.

The default `width_share` of 1.0 gives every slot a band to itself, which is exactly the
stacked layout every component had before bands existed. A component that never mentions
width is laid out as it always was.

`comparison` is why this exists. Two `list` slots set against each other are only a
comparison if they are *beside* each other; stacked, the reader sees one list of ten things
and the opposition is gone. A variant's `per_row` (§3) cannot supply it, because `per_row`
arranges the items *within* a slot, never the slots themselves.

### 1.5 Canonical slot shapes

Components declare which shapes they consume. Swaps and degradation chains are legal only
*within* a shape.

| Shape | Type |
|---|---|
| `title` | `string` |
| `prose` | `string` |
| `list` | `Array<{ label, value?, desc?, icon? }>` or `Array<string>` |
| `media` | `{ asset_id, alt, focal? }` |
| `tabular` | `{ headers: string[], rows: string[][] }` |
| `chart` | `{ kind, series, labels, source? }` |

`stat_row`, `bullets`, `card_grid`, `icon_row`, and `timeline` all consume `list`, so
swapping among them preserves content by construction.

**`chart` and `media` are never interchangeable.** A chart the harness holds data for is a
`chart` and exports as a native pptx chart with an embedded worksheet — editable,
restyleable, refreshable by the recipient. A chart the user supplied as a picture is
`media` and exports as a picture. The harness never silently converts between them: turning
a `chart` into an image destroys the data, and there is no reliable path back from a
picture to a `chart`. If a user pastes a chart image and asks to change a number, the
honest answer is that the harness cannot, and it should say so rather than redraw.

### 1.6 Ops and transactions

```json
{ "seq": 412, "op": "set_slots", "target": "bk_3",
  "patch":   { "items": [ { "value": "$4.2M", "label": "ARR" } ] },
  "inverse": { "items": [ { "value": "$4.0M", "label": "ARR" } ] },
  "author": "model", "turn": 18 }
```

- `author` ∈ `user` | `model` | `lint`. Drives the repair arbiter (§5.3) and the preference
  profile's observed channel (§8.2).
- Ops nest inside a **turn transaction** that commits or rolls back whole. Undo is
  turn-granular — users think "undo that request," not "undo `set_variant`."
- **Single writer.** The log takes one writer at a time; while a model turn holds the lock
  the canvas is read-only and user edits queue.

---

## 2. Theme

```json
{
  "id": "acme-2026",
  "source": "extracted",
  "palette": {
    "bg": "#FFFFFF", "surface": "#F5F7FA", "rule": "#DCE1E8",
    "ink": "#12161C", "ink_muted": "#5A6472",
    "brand": "#004098", "brand_ink": "#FFFFFF",
    "accents": ["#0E7C66", "#B4532A", "#6A4C93"],
    "positive": "#0E7C66", "negative": "#B4532A"
  },
  "type": {
    "families": { "display": "Source Han Sans", "body": "Inter" },
    "scale": {
      "deck_title":  { "family": "display", "size": 44, "weight": 700, "line": 52, "track": -0.01 },
      "slide_title": { "family": "display", "size": 32, "weight": 700, "line": 40 },
      "block_title": { "family": "body",    "size": 24, "weight": 600, "line": 30 },
      "body":        { "family": "body",    "size": 22, "weight": 400, "line": 30 },
      "stat":        { "family": "display", "size": 44, "weight": 700, "line": 50 },
      "label":       { "family": "body",    "size": 18, "weight": 500, "line": 24 },
      "caption":     { "family": "body",    "size": 16, "weight": 400, "line": 22 }
    },
    "floor": 16
  },
  "grid":    { "canvas": [1280, 720], "margin": 64, "columns": 12, "gutter": 16, "baseline": 4 },
  "spacing": [4, 8, 12, 16, 24, 32, 48, 64],
  "shape":   { "radius": 4, "rule_weight": 4, "card_fill": "none", "shadow": "none" },
  "layouts": ["title", "stack", "two_col", "hero_plus_row", "full_bleed"],
  "inferred": ["type.scale", "spacing"]
}
```

Rules:

- Palette entries are **roles**. Adding a color means adding a role with a defined use.
- `accents` is **ordered**: item *N* of a list slot takes `accents[N % len]`, so cross-slide
  color consistency is structural rather than remembered.
- The type scale maps **roles → specs**, which is what lets `restyle(shape, "title")` work
  on freeform slides where there is no component to consult.
- `floor` is the size the repair ladder may never cross.
- Vertical rhythm snaps to `grid.baseline`.
- `inferred` lists fields guessed during extraction, so the user can correct them.

**Validate once at load:** every foreground/background role pair for contrast ≥ 4.5:1,
every family for embeddability, every scale step against the floor. A passing theme means
managed slides *cannot* fail contrast, so that check leaves per-slide lint. Freeform
slides still need it — their colors are the original author's.

---

## 3. Component catalog

| Key | Slot shapes | Variants | Degrades to |
|---|---|---|---|
| `title_slide` | title, prose? | left, centered, image-bg | — |
| `section_break` | title, prose? | bar, full-bleed | — |
| `slide_title` | title | plain, with-kicker | — |
| `agenda` | list | list, two-col | `bullets` |
| `bullets` | title?, list | plain, lead-in | split slide |
| `stat_row` | list | flat, carded | 2-row → `card_grid` |
| `card_grid` | title?, list | 1×3, 2×2, 2×3 | `bullets` |
| `icon_row` | title?, list | icon-top, icon-left | `card_grid` |
| `timeline` | title?, list | horizontal, vertical | `bullets` |
| `comparison` | title?, list×2 | split, table | — |
| `chart` | chart, prose? | wide, half | — |
| `data_table` | tabular, prose? | plain, zebra | split slide |
| `image_full` | media, prose? | bleed, inset | `image_split` |
| `image_split` | title?, media, prose | image-left, image-right | stack blocks |
| `quote` | prose, title? | pull, full-bleed | `prose` block |
| `takeaway` | title, list? | bar, centered | — |

**`comparison` is terminal.** It does not degrade to `data_table`: a table holds `tabular`,
and two `list` slots do not become one without a conversion that would decide for the author
which column is which.

**Bounded overrides** — `density`, `emphasis`, `align`, `accent`, `media_scale` — each
clamped to a safe range and theme-derived. These absorb "nudge it" requests so
`eject_slide` stays rare.

**A variant's `per_row` is geometry, and the expander owns it.** A list slot with `per_row`
> 1 expands into a grid of *cells* — `LaidOutSlot.cells()` — and the writer emits one text
box per cell. That is what makes `stat_row` a row rather than four sentences, and it is the
only difference between several variants in the table above.

Two things follow, and both were once wrong in the same direction:

- **The budget measures the cell, not the slot divided by a count.** Those agree only for a
  single column. Charging each item the full width over the item count, while the renderer
  gave it the full width, made measurement and rendering disagree about the same slide —
  and the tie-break was a refused write, so a model asked for three statistics was told they
  did not fit and shortened them to `[X]%`.
- **A `{value, label}` item is two lines of different type**: the figure in the slot's role
  and its accent, the label beneath at `slots.STAT_LABEL_EM` of it. The preview's CSS and
  the writer's run sizes read that ratio from one constant, because the preview is the file
  rendered and two copies of a number like that is precisely how they come to differ.

Catalog discipline:

- Add a **variant** unless the request needs both a new variant *and* a new slot shape.
- Every component must survive its worst case — maximum item count, longest plausible
  string, missing optionals. Fixtures for exactly that live in `components/__fixtures__/`.

### 3.1 Slot budgets

A budget is the most content a slot can hold at the theme's type size inside its region,
less the fidelity margin. It makes gate 1 of §5.1 possible.

**A budget is a function, not a constant:**
`budget(component, variant, slot, region, n_items, script)`. The same `stat_row` gives each
item less room at five items than at three, and less again in a half-width region.

```json
{ "stat_row/carded/items[].label": {
    "region": "footer_row", "n_items": 4,
    "unit": "advance_width_em", "capacity": 46.2, "max_lines": 2,
    "hint": { "latin": 88, "cjk": 41 },
    "margin": 0.083 } }
```

- **Enforced** in advance width against real font metrics. Character counts misprice CJK
  by roughly 2×.
- **Communicated** as a per-script character hint — a model cannot reason in ems. The hint
  guides; the measurement decides.
- `margin` is the measured p99 divergence from §6.3, already subtracted from `capacity`.

Rejections carry the numbers and the ways out:

```
budget_exceeded: slot "label" in stat_row/carded/footer_row (4 items)
  capacity 46.2ew (~88 latin chars) · got 61.4ew (~117)
  options: shorten ~29 chars · variant "2-row" · component "card_grid" · 3 items
```

Regenerate whenever the theme, type scale, or a component's geometry changes.

---

## 4. Tool surface

Gated by slide mode; the router rejects mismatches with an explanatory error rather than
silently coercing.

```python
# ---- managed slides ----
add_slide(index, layout, blocks)                        -> SlideId
add_block(slide_id, region, component, variant, slots)  -> BlockId
set_slots(block_id, patch)                              -> Diff   # partial, budget-checked
set_variant(block_id, variant)                          -> Diff
set_component(block_id, component)                      -> Diff   # same slot shape only
set_override(block_id, key, value)                      -> Diff   # clamped
remove_block(block_id)                                  -> Diff

# ---- freeform slides ----
align(shape_ids, edge)                                  -> Diff
distribute(shape_ids, axis, gap=None)                   -> Diff
match_size(shape_ids, dim)                              -> Diff
snap_to_grid(shape_ids)                                 -> Diff
nudge(shape_id, direction, step)                        -> Diff   # small|medium|large
fit_box_to_text(shape_id)                               -> Diff
restyle(shape_id, role)                                 -> Diff   # theme token, not raw font
set_frame(shape_id, frame)                              -> Diff   # ESCAPE HATCH — logged

# ---- both ----
set_text(target, text)                                  -> Diff   # budget-checked in both
get_outline() / get_slide(id) / list_components(key=None) / get_theme()
reorder(slide_id, index) / delete_slide(id)
eject_slide(id) / adopt_slide(id, component=None)
render(slide_id) / lint(scope) / export(mode, path)
review_deck(slide_id=None, include_raised=False)        # advisory; never refuses (§5.5)
undo() / redo()
```

Absent from the managed side: `set_position`, `set_font`, `add_textbox`. Absent from the
freeform side: anything taking raw numbers except the marked escape hatch.

**Every mutating tool returns the resulting render in its result** — the measurement
always, and the image when the session declares vision. Verification is not an optional
follow-up call the model might skip, which matters most under MCP where you cannot force a
host's model to call `screenshot`.

---

## 5. Verification and repair

### 5.1 Three gates, cheapest first

| Gate | When | Checks | Cost |
|---|---|---|---|
| 1 — budget + schema | On write | Slot capacity, required slots, enum values | free |
| 2 — analytic lint | After expand | Measured overflow, collision, out-of-region | ~10ms |
| 3 — export fidelity | On export / CI | Real font metrics against the produced `.pptx` | seconds |

Gate 2 is **measured, not looked at** — `scrollHeight`, `getBoundingClientRect()`, real
advance widths. Screenshots are for judgment (balance, emphasis, ugliness), never for
detecting overflow.

### 5.2 Repair ladder

1. **Prevent** — fixed slot counts, theme-only colors, budgets. A lint rule that fires
   often is a bug in a component schema; fix it there and delete the rule.
2. **Normalize** — deterministic, no model: snap contrast to the nearest passing token,
   clamp to the font floor, snap to grid, fix widows.
3. **Layout repair** — walk the component's degradation chain (§3). Keeps the words.
4. **Content repair** — model rewrites *this slot* to *this budget*. Never "fix the deck."
5. **Escalate** — low-res assets, missing required content, text that can't shorten.

### 5.3 The repair arbiter

Provenance, from the op log:

- user-authored text → **layout repair** (they wrote the words, not the layout)
- model-authored text → **content repair** (it wrote the copy; it can tighten the copy)

Severity gates the ladder: only `error` triggers repair. `warning` and `info` go in the
report, or you get an infinite polish loop.

### 5.4 Termination

Stop when the goal is satisfied, no `error`-severity lint remains, the iteration cap (2–3
repair rounds) is reached, or **the same error signature repeats**. Without the repeat
check, repair oscillates: shrink to fit → below the font floor → grow → overflow.

### 5.5 Deck-level checks

Per-slide lint is cosmetics. `review_deck` (`core/review.py`) is the pass over the whole
deck, and it is a different kind of check from every other gate here: it **judges rather
than measures**, so it is advisory and can never refuse a write. A budget refusal is a fact.
A finding is an opinion, and an opinion that can block a write is a style guide with a gun.

Ten rules, split between what a slide *says* — a title that files rather than states, two
claims joined by "and", a list past seven items, prose long enough to be read rather than
glanced at, a deck ending on "Questions?" — and where the deck's style *drifts*: title case,
bullet terminal punctuation, repeated titles.

The design constraint is precision, not coverage. An advisory channel dies of false
positives, so every rule decides from the text with certainty and stays silent where it
cannot: `_case_of` returns `None` for a title that is short, all-caps, or proper-noun heavy,
because each looks like evidence of a capitalisation convention and is not.

What is deliberately **not** here is the interesting half — whether three names mean one
thing, whether a slide earns its place, whether the argument holds. None of it can be
decided by counting, and it belongs to a model reading the deck with `house-style` or
`narrative-arc` in front of it. The mechanical subset is worth shipping anyway because it
costs nothing and fires without being asked.

Findings are raised once per session (`Session.raised`) and then stay quiet, because
repetition is how an advisory channel gets switched off; `include_raised` gets them back and
`suppressed` keeps the loss visible.

---

## 6. Rendering and export

### 6.1 Preview is the export, rendered

The preview is not an approximation of the export; it **is** the export. Deck state goes out
through the ordinary exporter, a real renderer turns that file into a PDF, and pages are
rasterised on demand. "What you previewed is what PowerPoint renders" stops being a property
this document has to defend and becomes true by construction — and SmartArt, gradients,
WordArt, tables, curved connectors and video posters all render correctly for free, because
nothing is reimplementing them.

This replaces an HTML renderer for imported slides. That approach was measured against
PowerPoint and reached **0.041 mean pixel difference** while still missing bullets, curved
connectors, gradients and rotation — an enumerated subset with no end to the tail. Every
partial reimplementation, including the third-party ones, has the same shape.

**The split that makes the latency acceptable is who is waiting:**

| | Consumer | Path | Cost |
|---|---|---|---|
| **Measurement** | the model | analytic — HarfBuzz metrics, the expander's boxes | ~1ms |
| **Preview** | a person | real render of the exported file | ~1s first view, then ~50ms |

The ~10ms budget was always for the *verification loop*, which is measurement. Measurement
never needed a renderer, and must keep working where no Office is installed — the model's
loop cannot depend on a GUI application.

PDF is the cached artifact because it is vector: one expensive conversion per deck version,
then any page at any DPI for tens of milliseconds. The version is a hash of deck state, so
every edit invalidates without anyone remembering to say so, and it is written beside the
PDF so a restart costs nothing.

**Overlays, not redrawing.** The picture is somebody else's rendering; the measurement boxes
are ours, positioned in canvas coordinates — the same numbers the exporter writes. That is
what lets the inspector work on top of a raster it did not produce.

**Sandbox discipline.** PowerPoint is a sandboxed application and prompts for access to any
folder it has not been granted. A directory per version would mean a permission dialog on
every edit, so the cache is one stable directory with fixed filenames.

**HTML survives where the harness controls the CSS.** Managed slides measure **0.007**
against PowerPoint — layout-identical, the residual being font substitution because the
exporter does not yet embed fonts. `render/html.py` and `render/browser.py` remain the
managed-slide renderer and the test-time oracle; they earned that by catching three silent
measurement bugs a single self-consistent measurer could not have found.

### 6.2 Mutating export

Regenerating an imported file destroys everything unmodeled — SmartArt, animations,
transitions, embedded media, comments, custom XML, master variants. The exporter opens the
original package and patches only shapes marked `dirty`, leaving the rest byte-identical.
State is an overlay on the real file, not a replacement.

Managed slides export into **real slide layouts and placeholders**, not free-floating text
boxes, so the deck keeps Outline view, accessibility structure, and master inheritance.

**Verified, not assumed.** A round-trip spike against a 174 MB research deck — 5 slides, 16
embedded MP4s, `<a:videoFile>` and `<p:timing>` animation, notes, an MIP sensitivity label,
and Office change-tracking parts — confirms the bet:

| Check | Result |
|---|---|
| Part set on open then save | 105 in, 105 out — nothing dropped, nothing added |
| Media, thumbnail, `changesInfos/`, `revisionInfo`, `LabelInfo`, `custom.xml` | byte-identical |
| Slides, layouts, masters, notes, theme, `presentation.xml` | canonically identical |
| Relationships and content types | regenerated, but zero differences at the relationship level |
| Blast radius of editing one run | that run's slide, nothing else |

Two rewrites are unavoidable and harmless. Every XML part's declaration is reserialized
(`"` becomes `'`, CRLF becomes LF), and `.rels` plus `[Content_Types].xml` are rebuilt from
python-pptx's own model — reordered and reformatted, but with every relationship and content
type preserved. Because byte-identity does not survive, **the export test asserts canonical
XML equality on untouched parts and byte equality on binaries**, not a whole-file hash.

Still unproven: SmartArt (`dgm:`), native charts, slide transitions, and OLE objects, none of
which this deck contains. Slide XML the harness does not model passes through untouched — a
76 KB, 50-shape slide survived canonically identical — so the mechanism is sound; the
fixture corpus needs a deck carrying those parts to close the case.

### 6.3 Fidelity contract

| Cause | Handling |
|---|---|
| Font substitution | Embed fonts; restrict themes to fonts that embed cleanly |
| Shaping engine differences | Margin only — irreducible |
| Line-break rule differences | Margin, or pre-broken lines in `pixel_locked` |
| Line-height model | Always `spcPts` (absolute), never `spcPct` |
| Text insets | Explicitly zeroed |
| px → EMU rounding | Width rounds down, height rounds up |
| Autofit | Disabled — overflow must be loud |

Margins are the measured p99 of observed delta per component × script, derived from a
fixture corpus in CI. Expect CJK roughly double Latin.

### 6.4 Export modes

| Mode | Behavior | Use |
|---|---|---|
| `editable` (default) | Real paragraphs; boxes budgeted below capacity; fonts embedded | The user will keep editing |
| `pixel_locked` | Lines pre-broken, autofit off | Final artifact, print, PDF |

`pixel_locked` guarantees the render and makes the deck unpleasant to edit by hand — which
is why it's a mode, not the architecture.

---

## 7. Import and adoption

1. **Parse** the package; retain the original OOXML for mutating export.
2. **Extract the theme** — `theme1.xml` yields families and palette; masters and layouts
   yield a starting grid. Type scale, spacing, and shape rules are inferred or defaulted,
   and listed in `theme.inferred`.
3. **Land every slide as `freeform`.**
4. **Optionally adopt.** The classifier recognizes patterns rather than reconstructing
   intent — four similarly-sized boxes, evenly distributed, each with a large number and a
   small label, is a `stat_row`. Signals: shape clustering, size and alignment regularity,
   vision over the render.

**Adoption reflows the slide, so it is always a user-visible proposal with a before/after
— never a silent inference.** Failure is fine; the slide stays freeform.

Theme extraction is the highest-value part and the easiest: it makes "add three slides to
this deck" produce slides that match, which is the most common request against an imported
file and needs no adoption at all.

### 7.1 Import without the slides

Step 2 is worth having on its own, and `Session.from_template` is step 2 with the rest
skipped — the palette, the faces and the grid come across, no slide does. It is how a deck
actually starts: nobody opens a blank canvas when a company template exists.

The result is *not* an imported deck, and the difference is load-bearing in three places.
There is **no package to patch**, so the exporter builds from nothing rather than mutating
a source; every slide is **managed**, so the whole component catalog is available where an
imported deck would have offered a set gated to `freeform`; and the theme is attributed via
`deck.theme_from` rather than `deck.source_path`, because claiming the deck came from that
file would tell the exporter to patch a package this deck never came from.

`new --from` writes that deck to disk offline; `serve --from` opens it in the chat client,
which is the one that matters — creating an *empty* themed deck is deterministic, and
writing slides into it is the part that needs a model.

---

## 8. Context, memory and preferences

### 8.1 Context pyramid

| Level | When | Content |
|---|---|---|
| 1 | always | Outline — index, mode, component or gist per slide |
| 2 | always | Theme tokens + component keys with one-line purposes + preference profile |
| 3 | on demand | Filled slots (managed) or shape list (freeform) for focused slides |
| 4 | on demand | Render of a focused slide |
| 5 | on demand | Full slot schemas, raw assets |

Canvas selection decides what gets promoted to level 3. Level 2's catalog runs ~1–2k
tokens; keep the always-on form to keys and one-line purposes and push full slot schemas
behind `list_components(key)`.

### 8.2 Preference profile

Memory holds what the user *said*; the preference profile holds what they **do**.

| Source | Signal |
|---|---|
| **Explicit** | Stated rules — "always end with a takeaway", "never pie charts" |
| **Observed** | A `user` op landing on a target a `model` op just touched |
| **Corpus** | Statistics over the user's existing decks |

The op log makes the observed channel free — it already records `author` and `target`, so a
correction is a query, not new instrumentation.

```json
{ "component_priors": { "stat_row": { "variant": "flat", "n": 12, "conf": 0.86 } },
  "copy":       { "title_len_p50": 42, "voice": "verb-first, no trailing period", "n": 34 },
  "structure":  { "opens_with": "agenda", "closes_with": "takeaway", "n": 8 },
  "avoid":      [ { "rule": "no pie charts", "source": "explicit" } ] }
```

- **Preferences are data, never weights.** No fine-tuning; a readable, editable artifact.
- **Confidence and provenance per entry.** Below threshold it enters context as a hint;
  above it, as a rule. One correction must not become law.
- **Propose, never silently adopt.** *"You've switched `stat_row` to `flat` five times —
  make it the default?"*
- **Scoped** — a global profile plus per-context overrides.

Learned preferences bind the theme and the catalog, never geometry. Preferences select among
choices the design system already permits.

---

## 9. Skills

Skills ship with the harness and version alongside the tool surface they describe.

| Kind | Loaded | Purpose |
|---|---|---|
| `playbook` | User-invocable, as MCP prompts | Multi-step recipes |
| `invariant` | Injected when its trigger matches | Rules that must hold |

| Skill | Kind | Trigger | State |
|---|---|---|---|
| `narrative-arc` | playbook | A deck that is a pile of slides rather than a case | **shipped** |
| `import-triage` | playbook | First contact with a deck someone else made | **shipped** |
| `house-style` | playbook | A deck that reads as though several people wrote it | **shipped** |
| `data-to-slide` | playbook | Numbers with no claim decided yet | **shipped** |
| `data-research` | playbook | A claim that needs evidence before it reaches a slide | **shipped** |
| `edit-existing-deck` | playbook | Imported file | planned |
| `fix-overflow` | playbook | `lint` reports overflow and the fix is structural | planned |
| `apply-template` | playbook | Rebrand onto a new theme | planned |
| `visual-review` | playbook | "How does this look" — deck-level critique | needs vision |
| `create-presentation` | playbook | New deck from a brief, outline, or dataset | needs data ingress |

Frontmatter `description` is trigger text — write it around the **symptom** as well as the
task. `tools:` declares what the playbook calls, so a renamed tool fails the skill that
needed it by name; inferring dependencies from the prose cannot work, because a playbook
discussing `opaque` shapes is naming a concept rather than a tool.

A playbook is something a user could ask for by name; an invariant is something they should
never have to.

### 9.1 Why the invariants are not skills

Three invariants were specified here — `export-fidelity`, `layout-repair`, and
`writing-for-slides` — and none of them will be written, because each is now **enforced**
rather than described:

| Was | Is |
|---|---|
| `export-fidelity` | `io/writer_assertions.py`, budget checks before every write, and font embedding that reports what it could not embed |
| `layout-repair` | the `repair` tool and `core/repair.py`, which acts rather than advises |
| `writing-for-slides` | the craft rules in the agent's system prompt, on every turn |

The rule generalises, and it is the gate for anything proposed here in future: **a rule that
can be enforced in code does not belong in a skill.** A document a model may or may not read
is the weakest mechanism available, and reaching for it when a check would do is how a
codebase accumulates advice nobody follows. The evidence was cheap to come by — the original
`export-fidelity` skill was deleted at some point and nothing broke, because everything it
said was already true of the code.

### 9.2 Prior art: the script-first approach

Anthropic's `pptx` skill solves the same problem from the opposite direction, and the
comparison is worth keeping because it bounds what this design is *for*. Its shape: create
by writing a pptxgenjs script, edit by unzip → modify XML → rezip, read via markitdown, with
helper scripts for validation, thumbnails, and package cleanup.

**Where it is stronger.** A script expresses any layout, where our catalog expresses sixteen
things. It has no data-ingress problem, because code *is* the ingress — reading a CSV and
charting it happen in the same breath. And it preserves untouched content perfectly by
construction, never having parsed it into a model at all; our `ooxml_id` and `opaque` exist
to buy back that property, which it gets for free.

**Where this design is stronger, and it is the same reason twice.** That skill spends a
section on corruption gotchas a model must remember — hex without `#`, no negative shadow
offsets, set the layout before adding slides. Every one is a mistake our API cannot express.
And on text that does not fit it advises padding containers by ~10% and *not trusting the
fit check* on any font outside a short safe list, with shrinking the type as the first
remedy. We resolve the real face, shape it through HarfBuzz, and refuse with a capacity
number on any font — and forbid shrinking outright.

**What follows.** Their model suits *generating*, where reach matters and there is nothing
to preserve. This one suits *editing*, where preservation and measurement matter and reach
is beside the point because the layout already exists. That is the division worth holding if
the two are ever combined.

---

## 10. Interfaces

The tool registry is transport-agnostic; three thin adapters expose it.

| Adapter | Gets you | Loses |
|---|---|---|
| **MCP server** | Any MCP host becomes a client. Tools return images; resources expose deck state; prompts carry playbooks | Canvas and inspector; you don't own the context pyramid |
| **Own client** | Chat + live canvas + inspector + op log | You build it |
| **CLI** | Scriptable; usable from coding agents today | Not interactive |

The web client serves the **same HTML the measurement pass lays out**, into an iframe. The
user is therefore looking at the artifact that was measured, not a picture of it, so "what
you see" and "what was checked" cannot drift apart.

The agent loop targets an **OpenAI-chat-compatible** endpoint, so one loop drives OpenAI,
vLLM, Ollama, Together, Groq, or OpenRouter by changing `base_url`. It owns exactly one
thing the model cannot be trusted with: **termination** — no tool calls means done, a
repeated error signature means stuck, and a round cap bounds the rest.

The harness is also an **MCP client** — deck content comes from the user's other servers.

**Model requirements:** an LLM is sufficient for correctness. Overflow, collision, and
contrast are measured, not seen. Vision is an enhancement for `visual-review`, adoption
tie-breaks, and image slots. Render images for the model at ~800px wide (~480 tokens);
full resolution goes to the canvas only.

---

## 11. Module layout

Python core — `python-pptx` for surgical read-modify-write is decisive given imports.
A real renderer is driven from Python for previews; measurement needs neither.

Shipped in v0 is unmarked; **·** marks what a later phase adds.

```
ppt_harness/
├── state/         document.py ops.py store.py theme_default.py
├── io/            import_pptx.py theme_extract.py export_mutate.py writer_assertions.py
│                  · adopt.py export_pdf.py
├── render/        fonts.py measure.py expand.py budget.py html.py svg.py
│                  browser.py preview.py
│                  · lint.py
├── components/    registry.py
│                  · primitives.py stats.py cards.py timeline.py charts.py tables.py
│                  · __fixtures__/         # worst-case slot payloads
├── tools/         base.py shared.py managed.py router.py
│                  · freeform_.py
├── core/          session.py
│                  · loop.py planner.py context.py policy.py memory.py preferences.py
├── adapters/      mcp_server.py cli.py web.py
│                  · mcp_client.py
├── fidelity/      reference.py compare.py
│                  · margins.generated.json
└──               · freeform/
scripts/           make_demo_deck.py
```

Three placements are load-bearing:

- **`render/fonts.py` sits under everything.** Resolution is a separate concern from
  measurement because it is where the expensive mistakes live: the wrong face makes every
  budget confidently wrong, which is worse than having no budget.
- **Line breaking lives in `render/measure.py`, not beside the exporter.** The harness must
  measure with the same code the expander plans with, or the budget and the file disagree by
  construction. `fidelity/` imports it rather than reimplementing it.
- **`render/preview.py` depends on `io/export_mutate.py`, never the reverse.** The preview
  is a consumer of the export, which is what keeps it honest; an exporter that knew about
  previews could be tempted to make them look better than the file.
- **`components/` is where the product's taste lives** and deserves snapshot tests over the
  worst-case fixtures.

Subpackages carry no `__init__.py`; they are namespace packages. The single top-level
`__init__.py` earns its place by exposing `Session` and `dispatch` lazily, so importing the
package does not build the font index.

---

## 12. Phasing

**v0 — useful on imported decks. Shipped.** Import + theme extraction · read tools ·
`set_text` with budget checking · managed slide insertion using the extracted theme ·
mutating export · MCP server · the three writer assertions. No adoption, no freeform
geometry ops, no canvas.

Four things v0 learned that were not in the plan:

- **Budgets need a font resolver before they need a catalog.** Capacity is only meaningful
  once you know which file will render the string. A CSS stack resolves *per script*, and a
  naive family index resolves "Arial" to `Arial Bold.ttf` — both silently misprice
  everything downstream. `render/fonts.py` exists because of this.
- **CJK measures 2.17x Latin per character** on the fixture deck's own faces, confirming
  §3.1's estimate. The test asserting it is the one that would catch a regression to
  character counting.
- **Byte-identity is the wrong export assertion.** python-pptx reserializes every XML
  declaration and rebuilds `.rels`; both are lossless. See §6.2.
- **Shape ids are unique per slide, not per package.** Scoping the fidelity check by a flat
  id set silently audits untouched shapes on other slides — and scoping by an *empty* set
  must mean "check nothing", not "check everything".

**v1 — quality loop. Shipped.** Round-trip preview through a real renderer · agent loop ·
web client with chat, live preview and measurement overlays · fidelity oracle · the full
sixteen-component catalog with degradation chains · the repair ladder · bounded overrides ·
subsetted font embedding.

The web client arrived a phase early because it turned out to be nearly free once the
renderer existed: the preview is the same HTML the measurement pass lays out, so there was
no second rendering path to build. The renderer was worth doing first for a different
reason — a second layout engine to disagree with. Two bugs surfaced within minutes of
having one, both silent and both wrong in the direction that ships broken decks:

| Bug | Symptom | Why it survived until then |
|---|---|---|
| Box widths converted to points, text measured in canvas px | Every box measured 25% narrow; phantom line breaks | Self-consistent — nothing to check it against |
| Over-long tokens never broken | Undercounted lines on identifiers and URLs | Only bites on the strings most likely to overflow |
| Imported text sized from the theme, not the file | 128px of invented overflow on one slide | The numbers looked plausible |

**v2 — editing depth. Mostly shipped.** Semantic freeform constraints · adoption behind a
confirmation · deck-level operations · memory and preferences.

**Still open**, in the order they are worth doing:

1. **The fidelity margin corpus in CI.** §6.3 cites margins that are still hard-coded.
   `ppt-harness compare` already produces the numbers; nothing writes them down per
   component and script.
2. **The inspector and visual approval diffs** — the half of the own-client story the chat
   pane does not cover.
3. **`add_block`**, so a block can join an existing managed slide rather than only arriving
   with one.
4. **The taste profile's corpus channel.** The explicit and observed channels exist
   (`core/preferences.py`, `OpLog.corrections()`); statistics over a user's existing decks
   do not.

**Later** — sub-agents, collaborative editing, template authoring, PowerPoint-on-Windows CI.

Ordering follows real usage: most imported-deck work is text edits and slide additions, not
geometry manipulation.

---

## 13. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Authoring unit | Layout frame + blocks | One component per slide can't express hybrid slides |
| Coordinates | Banned in both modes | Components own geometry; constraints own it on freeform |
| Imported decks | First-class, two-tier | Drove the mode split, mutating export, dual renderers |
| Source of truth | Overlay on original OOXML | Rebuilding destroys unmodeled content |
| Language | Python | `python-pptx` read-modify-write; Chrome driven for measurement |
| Preview | HTML managed, LibreOffice freeform | Interactivity where iterated, fidelity where static |
| Export layout | Frozen geometry, absolute boxes | PowerPoint receives, never re-derives |
| Placeholders | Real layouts + placeholders | Otherwise Outline view and a11y structure are dead |
| Charts | Native on export, image in preview | An image chart costs the recipient editability; the plot area is the one thing PowerPoint may lay out |
| Chart vs image | Separate slot shapes, never converted | Rasterizing a chart destroys its data and the trip back is not reliable |
| Autofit | Off | Silent shrink hides failure and ships bad decks |
| Line spacing | `spcPts` absolute | `spcPct` resolves against font metrics, won't match CSS |
| Safety margin | Measured p99 per component × script | Replaces a guess with a regression test |
| Theme | Roles, validated once at load | Managed slides then cannot fail contrast at all |
| Slot budgets | Advance width enforced, characters hinted | Character counts misprice CJK by ~2× |
| Verification | Render returned from every write | Cannot force a host model to call `screenshot` |
| Repair arbiter | Provenance — who wrote the text | Resolves "shrink the box or shorten the words" |
| Undo | Turn transactions over invertible ops | Users undo requests, not tool calls |
| Concurrency | Single writer on the op log | Canvas read-only during a model turn |
| Preferences | Data, proposed not adopted | Inspectable, correctable, no fine-tuning |
| Interface | MCP server first | Fastest to usable; defers the client build |
| Model | LLM sufficient, VLM optional | Correctness is measured; vision buys taste |
| Topic recipes | Not a skill per template | Frontmatter is flat by design; a fixed sequence is enforceable in code; skills are prompt surface |
| Visual richness | Decoration layer on components | Fixed per-topic layouts forfeit measurement and the imported-deck path |

### Decided, not built

Neither of these exists yet. They are recorded so the reasoning is not re-derived.

- **A topic recipe — "Fintech pitch", "Q4 review" — is not a skill, and certainly not one
  skill per topic.** Three things say so. The frontmatter parser is deliberately flat
  `key: value` with no nesting (`core/skills.py`: "a skill that needed a richer format would
  be a skill doing too much"), and a recipe is a *nested, ordered* slide sequence. The same
  docstring sets the bar that "a rule that can be enforced in code does not belong in a
  skill" — a fixed sequence of components is exactly such a rule. And skills are **prompt
  surface**: they are listed in the always-on catalog (§8.1) and exposed over MCP as
  prompts, so a hundred topics would not fit there at any price. What is genuinely a skill
  is the *procedure* of filling a recipe from a brief, needed once no matter how many
  recipes exist — and `narrative-arc` already triggers on "before building a deck from a
  brief". Where the recipe's own data should live is unsettled; see below.

- **Visual richness comes from a decoration layer on components** — fills, outlines and
  insets, resolved through theme roles — **not from fixed per-topic pixel layouts.** A
  hard-coded layout per topic forfeits both the measurement guarantee (§3.1, §5.1) and the
  imported-deck path (§7), which are the two properties a competitor cannot copy without
  rebuilding the harness. Decoration rides on top of geometry the expander still owns, so it
  costs neither.

### Open: where a starter outline lives

Two positions are in tension, and this is the owner's call, not a settled decision.

- **Stated today:** a template is a theme. `state/templates.py` — "A template here is a
  theme, not a `.pptx`" — and it is explicit that "what a template does *not* carry is slide
  content: a starter outline is a playbook's job (`narrative-arc`), not a palette's". §7.1
  says the same from the other end: `from_template` brings the palette, the faces and the
  grid across, and no slide. §9 lists `apply-template` as "rebrand onto a new theme".
- **The pull the other way:** the market's unit is a *topic* template — one pickable thing
  bundling a visual identity **and** a slide sequence. Users choose "Fintech Startup Pitch
  Deck", not a palette; a theme-only template makes them supply the structure themselves.

The tradeoff: bundling makes the first deck much better with one choice, and costs the
separation that lets any theme apply to any content — including an *extracted* theme, which
has no outline to bundle and is the case §7 says matters most. Adopting topic templates
would **reverse** the `templates.py` position and belongs under "Reversed during design"
when and if someone decides to.

### Reversed during design

- **TypeScript → Python.** Imported decks made `python-pptx` decisive.
- **LibreOffice previews → HTML previews.** A 0.5–2s render can't sit in a chat loop.
- **Pre-broken lines everywhere → an export mode.** It guarantees fidelity and makes decks
  uneditable, so it's the user's choice.
- **Import as a diagram box → a designed subsystem.** The requirement to edit real decks
  made it central.
- **One component per slide → blocks.** It could not express hybrid slides.

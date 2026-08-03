---
name: house-style
kind: playbook
description: Make a deck read as though one person wrote it — when someone says "make the style consistent", "this looks like three decks stapled together", or "match everything to slide 3"; when an imported file mixes Title Case with sentence case, or bullets that end in a full stop on some slides and not others; when the same kind of content is built a different way on every slide. Use when the deck is inconsistent rather than wrong.
arguments: [scope, anchor]
tools: [get_outline, get_slide, get_theme, set_text, restyle, set_align, set_list, set_variant, set_component, align, match_size, snap_to_grid, lint, remember_preference]
---

# House style

The theme guarantees that a role looks the same everywhere it is used. Nothing checks that
the same *job* was given the same role, and nothing at all reads the copy. A deck whose
bullets end in a full stop on six slides and not on the other nine passes every gate in this
harness and still looks like it was assembled the night before.

That gap is what this playbook is for. All of it is judgment; none of it is measurable.

**Scope:** {{scope}}
**Slide to match:** {{anchor}}

(Left as `{{...}}`? Then the scope is the whole deck and there is no anchor — the house style
is whatever the deck already does most. Step 2.)

## 1. Survey first. Change nothing.

`get_outline`, then `get_slide` across the scope. Reads are free and this is the one playbook
where surveying everything before touching anything genuinely pays, because **you cannot tell
drift from intent by looking at one slide**. One slide whose bullets end in full stops is a
data point. Nine of fifteen is the house style.

Note only what you will act on:

| Axis | What drifts | Where to look |
|---|---|---|
| **Case** | Title Case against sentence case | titles, bullet leads |
| **Terminal punctuation** | bullets that sometimes end in a full stop | body text |
| **Parallelism across slides** | one slide's bullets open with verbs, the next slide's with nouns | body text |
| **Person and tense** | "we grew", "growth was", "grow" — for the same fact | body, titles |
| **Numbers and dates** | 2.1x · 2.1× · 210%; Q3 · Q3 FY25 · third quarter; 1,200 · 1.2k | stats, titles |
| **Terminology** | customer · client · account, for one thing | everywhere |
| **Emphasis** | bold on keywords here, on whole sentences there | body text |
| **Type roles** | the same job given a different role slide to slide | freeform shapes |
| **Component choice** | the same shape of argument built two ways | managed blocks |
| **Placement** | a title that sits a little differently on every slide | freeform geometry |

Terminology is worth the most and gets noticed the least. A deck that calls one thing by
three names leaves the audience quietly wondering whether they are three things.

## 2. Let the deck name its own style

If an anchor slide was given, it wins — that is the user telling you the answer. Otherwise
the majority is the rule, and read a split three ways:

- **Lopsided** — nine to two. The two are drift. Fix them.
- **Near even** — seven to five is not drift, it is two sections or two authors. Ask which is
  right. Do not break the tie yourself; you will be wrong half the time and confident either
  way.
- **A minority of one** — check step 3 before you touch it.

**Your taste is not the house style.** If every title in the deck states a topic rather than a
finding, that is an argument problem, `narrative-arc` is the playbook for it, and it is a
different conversation from this one. Making fifteen topic-titles *consistently* topic-titles
is this playbook doing its job correctly. Say what you noticed; do not quietly rewrite the
deck's voice into yours while you were asked to even it out.

## 3. What must be allowed to break the pattern

Leave these alone unless they are broken in some other way:

- the closing ask, which should not look like the twelve slides before it
- one number that carries the deck, sitting large and alone
- a section divider
- any slide the audience is meant to stop on

Consistency is not uniformity. A deck weighted identically on every slide has no emphasis
left, and emphasis is content — flattening it is a real loss, paid for a gain nobody sees.

## 4. Fix one axis at a time

Axis by axis across the scope, not slide by slide. If a rule is wrong you find out on its
second application, having made two edits rather than thirty.

**Copy** — `set_text`. Budget-checked in both modes, so standardising on the *longer* form
(spelled-out months, "versus" for "vs", an expanded abbreviation) can push a shape over. The
refusal names the capacity: take the shorter form as the house style instead, which is
usually the better call anyway. Only ever reshape words you are standardising — if the
sentence has to lose meaning to fit the rule, the rule loses.

**Type roles** — `restyle`, on freeform slides. The fix for a subhead styled like a body
paragraph. It takes a role from the theme, never a size, which is exactly why it can be
applied across a whole deck without the type scale stopping meaning anything.

**Paragraph properties** — `set_align`, `set_list`, on freeform slides. On a managed slide the
component owns both, and the fix is `set_variant` rather than a property call, which will be
refused.

**Component choice** — `set_variant` first; `set_component` only when the two components take
the same slot shapes. If the same content genuinely needs a different component on different
slides, that is not drift either — say so and move on.

**Placement** — `align` and `match_size` work within one slide; a constraint applies to one
slide at a time, and asking for two is refused by name. **So there is no cross-slide alignment
call**, and reaching for one is the wrong model of what makes a deck line up. What puts slide
3's title where slide 9's sits is the grid: `snap_to_grid` on each of them pulls both onto the
same deck-wide columns and vertical rhythm. That is the entire mechanism, and it is the
reason to trust the grid over a shape that already looks nearly right.

## 5. Check it converged

`lint` the scope. Standardising copy changes text lengths, and a slide that fit before this
pass may not now — that is the one way a style pass causes mechanical damage.

Then read the titles in order, and the first line of each body, as one document. If it still
sounds like one person wrote it, you removed drift. If it now sounds like nobody wrote it,
you removed voice, and step 3 is where to look for what to put back.

## What to say when you are done

Give the house style as rules, in four or five lines — "sentence case throughout; bullets
take no terminal punctuation; multiples as 2.1×; the product is called the platform." The
user has to be able to disagree with a *rule*; nobody can disagree with "made the styling
consistent." Then name what you deliberately left breaking the pattern, and why.

Reach for `remember_preference` only if they confirm this is how they always work. A rule
derived from one deck is a fact about that deck, and a profile filled with inferences makes
the next deck worse rather than better.

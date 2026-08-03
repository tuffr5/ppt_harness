---
name: import-triage
kind: playbook
description: First contact with a deck someone else made — when a file has just been opened, when you do not yet know what is editable, when a request arrives about a deck you have never seen, or when edits keep coming back refused as shape_opaque or wrong_mode. Run this before promising anything about a deck you did not build.
arguments: [goal]
tools: [get_outline, get_slide, get_theme, lint]
---

# Import triage

An imported deck is not a blank page. Parts of it the harness models and can edit; parts it
preserves exactly and cannot touch; and parts of its theme were *guessed* because the file
never said. Finding that out by trial and error costs a refusal per discovery, and the
refusals arrive in front of the user.

Four reads, none of which change anything. Do all four before the first write.

**Their goal, if stated:** {{goal}}

## 1. What is here

`get_outline`. One line per slide: index, mode, and gist.

The **mode** is the thing to read for. `freeform` means the slide came from the file and the
harness holds its original shapes — you can edit text, not structure. `managed` means it was
built from components and everything is available. A deck that is entirely freeform has no
`add_slide` at all, and noticing that now saves offering it later.

## 2. What cannot be touched

For any slide you expect to work on, `get_slide`. Look for shapes marked `opaque`.

Opaque means SmartArt, a group, or an embedded object — something the harness preserves
byte-for-byte on export but cannot edit, because editing what it does not model is how a
diagram silently becomes a grey box. **Tell the user which shapes these are before they ask
for a change to one.** "The org chart on slide 4 is SmartArt; I can move it but not retype
it" is a useful sentence. Discovering it mid-edit is not.

## 3. What the harness had to guess

`get_theme`. Read the `inferred` list.

PowerPoint files do not record everything a layout system needs — there is no "body text
line height" or "rule colour" in a `.pptx`. Those are derived, and derived values are the
ones most likely to be wrong. If `inferred` is long, say so: a deck whose spacing was guessed
will measure differently from how PowerPoint draws it, and the user is the only one who can
confirm the guess.

## 4. What is already broken

`lint`. Whole deck.

Expect problems that predate you. Source decks routinely carry overflow that PowerPoint
hides with `normAutofit` — it shrinks text to fit and reports nothing, so a slide can look
fine and still be over its box. The harness measures the declared size and says so.

**This is the finding most worth leading with**, because nobody knew it was there and it is
not the user's fault. Report it as an observation, not an accusation, and never fix it
unasked — the words are theirs.

## What to say

Four sentences, no lists:

1. What the deck is — slides, how many are editable, what it appears to be about.
2. The one constraint that most limits what can be asked for — usually opaque shapes on a
   specific slide, or a wholly freeform deck.
3. Anything already overflowing, with the slide named.
4. What you would do first, given their goal.

Then stop and let them choose. Triage is for deciding what to do, not for doing it.

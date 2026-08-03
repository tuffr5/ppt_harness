---
name: narrative-arc
kind: playbook
description: Structure or repair the argument a deck is making — when a deck feels like a pile of slides rather than a case, when someone asks "what am I actually saying here", when the titles read as topics instead of findings, or before building a deck from a brief. Use when the problem is what the slides say and in what order, not whether they fit.
arguments: [brief, audience, decision]
tools: [get_outline, add_slide, set_text, set_slots, lint]
---

# Narrative arc

The harness guarantees a slide fits. Nothing in it can tell whether the deck *argues*. That
is what this playbook is for, and it is the only part of a deck that a measurement cannot
check for you.

Work in this order. Do not open a component or call a write tool until step 4 — the
temptation to start building is exactly what produces a deck that looks finished and says
nothing.

**Brief:** {{brief}}
**Audience:** {{audience}}
**Decision they need to make:** {{decision}}

(Any of those left as `{{...}}` above was not supplied. Ask for it before going on, or state
the assumption you are making in its place.)

## 1. Name the one sentence

Write the single sentence the audience should repeat to someone who was not in the room. Not
a topic — a claim, with a subject and a verb. "Q3 churn doubled in EMEA and the fix is a
pricing change, not a product one" is a sentence. "Q3 churn analysis" is a filing label.

If you cannot write it, the deck is not ready to be built, and no amount of slide-making
will fix that. Say so and ask.

## 2. Decide what kind of case this is

Most business decks are one of four shapes. Pick deliberately; the shape decides the order.

| Shape | When | Order |
|---|---|---|
| **Situation–Complication–Resolution** | Persuading someone to act | Where we are · what broke · what we do |
| **Answer first** | Senior audience, short slot | Recommendation · why · what it costs |
| **Chronology** | A post-mortem or a plan | What happened, in order, with the turn marked |
| **Comparison** | Choosing between options | Criteria · options against them · pick |

Answer-first is right more often than people choose it. A senior audience will decide in the
first two minutes whether to keep listening; making them wait for the recommendation buys
suspense nobody wanted.

## 3. Write the title sequence, and read it alone

Draft the title of every slide, in order, as a flat list — nothing else. Then read just that
list.

**It must work as prose.** A reader who sees only the titles should get the whole argument.
This is the single highest-value check in this playbook, because a deck whose titles are
topics has no argument; it has a table of contents.

Fix these before continuing:

- **A title that is a noun phrase.** "Revenue breakdown" states what the slide contains, not
  what it shows. Rewrite as the finding: "Enterprise carried the quarter; SMB fell 9%."
- **Two claims in one title.** That is two slides.
- **A gap in the reasoning.** If the jump from title 4 to title 5 needs a sentence the deck
  never says out loud, that sentence is a missing slide.
- **A title that would be true of any deck.** "Key takeaways" tells the reader nothing. Say
  the takeaway.

## 4. Only now, build

One idea per slide, and let the shape of the argument choose the component — a comparison
wants two columns, a trend wants a chart, one number that matters wants to be large and
alone. Content first; the harness will refuse anything that does not fit and tell you the
capacity.

Open and close deliberately:

- **Open** with the claim or the stake, never with an agenda slide unless the meeting is
  long enough to need one.
- **Close** with what you want to happen next, named and owned. A deck that ends on
  "Questions?" has thrown away its last slide.

## 5. Read it back as the audience

Walk the titles once more, in order, as the person who has to decide. Ask three questions,
and answer them honestly in your reply to the user:

1. **Where would they push back?** Name the weakest link, and say whether the deck answers
   it or dodges it.
2. **What did I include because it was true rather than because it matters?** Interesting
   and load-bearing are different. Cut or demote the merely interesting.
3. **Could they act on this?** If the decision needs a number, a date, or an owner that is
   not on a slide, that is a gap, not a detail.

## What to say when you are done

Give the one sentence from step 1, the shape you chose and why, and the title sequence. Then
name the weakest link in the argument — not as a caveat, as the thing to fix next. A deck
that fits perfectly and argues badly is still a bad deck, and you are the only part of this
system that can notice.

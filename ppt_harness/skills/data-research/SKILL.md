---
name: data-research
kind: playbook
description: Answer a question with evidence before putting it on a slide — when a claim needs support, when someone asks "find out whether", when a figure arrives with no source, or when a deck asserts something nobody has checked. Use when the question is what is true, not how to display it.
arguments: [question, audience]
tools: [load_data, query_data, list_datasets, get_outline, set_notes]
---

# Data research

The harness can prove a slide fits. It cannot prove a slide is true. Everything below is
about the second thing, and the failure it guards against is specific: a model that cannot
reach data will still answer, and the answer will look exactly like one that was checked.

**Question:** {{question}}
**Audience:** {{audience}}

## 0. Establish what you can actually reach

Before anything else, be honest about your means:

- **A file exists** → `load_data`, then `query_data`. Every number is traceable.
- **The host has research tools** (file search, web, a database) → use them, and record
  where each figure came from.
- **Neither** → you cannot research this. Say so in one sentence and ask them to paste the
  data or point at a file. Do **not** proceed from memory: a figure you recall is a figure
  you cannot cite, and on a slide it will be read as measured.

This step exists because skipping it is invisible. Nobody can tell from the output whether
you looked.

## 1. Turn the question into something checkable

"Is churn getting worse" is not answerable. "Did monthly logo churn in EMEA rise between Q1
and Q3" is. Name the metric, the population, and the period. If the question cannot be made
checkable, that is the finding — ambiguity is usually where the disagreement actually lives.

## 2. Decide what would change your mind

Before looking: what result would make the answer *no*? Writing it down first is what stops
the search from becoming a hunt for confirmation, which is the standard failure of analysis
done to support a deck someone has already decided to give.

## 3. Get the numbers

Query for the answer *and* for the thing that would contradict it. Prefer one query that
settles the question over five that circle it.

Watch for the three that mislead most often:

- **A denominator that changed.** A rising rate can be a shrinking base.
- **A period that does not match.** Comparing a partial quarter to a full one.
- **Survivors only.** Averages over accounts that are still here say nothing about the ones
  that left — which is usually the question.

## 4. Grade what you found

Sort every figure into one of three, and keep the sorting visible to yourself:

| Grade | Means | On a slide |
|---|---|---|
| **Measured** | Came from a query you ran, traceable to a dataset | State it plainly |
| **Sourced** | From a document or system you actually read | State it, name the source |
| **Estimated** | Inferred, extrapolated, or remembered | Label it, or leave it off |

An estimate presented as a measurement is the one error here that damages someone's
credibility in a room, because it is discovered by the person best placed to check.

## 5. Write the finding, then the caveat

One sentence for what is true, one for how confident you are and why. If the answer is "we
do not know", **that is a legitimate slide** — a deck that says which question remains open
is more useful than one that quietly answers it wrong.

Put the workings in speaker notes with `set_notes`: the query, the source, the period. The
person presenting will be asked "where does that come from", and the answer should be on
their screen rather than in their memory.

## What to say when you are done

The answer, its grade, and what would change it. Then say explicitly what you could not
check — the query you could not run, the source you could not reach. A stated gap is a
research finding; an unstated one is a trap for whoever presents this.

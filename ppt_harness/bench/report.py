"""Turn a pile of task results into a scorecard somebody can argue with.

Two rules, both learned from reading other people's benchmark tables:

**Say what the number is over.** A fit rate is meaningless without the deck count, and a
mean over tasks that failed to run at all is a lie by omission. Every figure here carries its
denominator, and failed tasks are counted rather than dropped.

**Never average across kinds.** Generating a deck and editing an imported one are different
jobs with different failure modes; one number over both hides which of them regressed.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runner import Result


@dataclass
class Scorecard:
    tasks: int = 0
    ran: int = 0
    met: int = 0
    sound: int = 0
    fit_rate: float = 0.0
    slides: int = 0
    clean_slides: int = 0
    refusals: int = 0
    tool_calls: int = 0
    rounds: int = 0
    seconds: float = 0.0
    review_findings: int = 0
    violations: int = 0
    parts_lost: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = vars(self).copy()
        out["fit_rate"] = round(self.fit_rate, 4)
        out["refusal_rate"] = (round(self.refusals / self.tool_calls, 4)
                               if self.tool_calls else 0.0)
        out["findings_per_slide"] = (round(self.review_findings / self.slides, 2)
                                     if self.slides else 0.0)
        return out


def score(results: list[Result]) -> Scorecard:
    card = Scorecard(tasks=len(results))
    for result in results:
        measured = result.measurements or {}
        fit = measured.get("fit", {})
        friction = measured.get("friction", {})
        integrity = measured.get("integrity", {})
        shape = measured.get("shape", {})

        if result.ok:
            card.ran += 1
        if result.met:
            card.met += 1
        if integrity.get("sound"):
            card.sound += 1
        card.slides += fit.get("slides", 0)
        card.clean_slides += fit.get("clean_slides", 0)
        card.refusals += friction.get("refusals", 0)
        card.tool_calls += friction.get("tool_calls", 0)
        card.rounds += friction.get("rounds", 0)
        card.seconds += friction.get("seconds", 0.0)
        card.review_findings += shape.get("review_findings", 0)
        card.violations += len(integrity.get("violations", []))
        card.parts_lost += len(integrity.get("parts_lost", []))

    # Over slides, not over tasks: a mean of per-deck rates lets a one-slide deck outvote a
    # twelve-slide one, which is how a benchmark ends up rewarding short decks.
    card.fit_rate = card.clean_slides / card.slides if card.slides else 0.0
    card.seconds = round(card.seconds, 1)
    return card


def environment(model: str = "") -> dict[str, Any]:
    """What produced the number. A score without this is not reproducible and not evidence."""
    from ..fidelity import reference
    from ..render import browser

    return {
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "reference_renderer": reference.available(),
        "browser": browser.available(),
    }


def write(results: list[Result], out_dir: Path, *, suite: str, model: str = "") -> Path:
    card = score(results)
    payload = {
        "suite": suite,
        "environment": environment(model),
        "scorecard": card.as_dict(),
        "tasks": [
            {"task": r.task, "kind": r.kind, "ok": r.ok, "met": r.met, "error": r.error,
             "expectations": r.expectations,
             "fit": (r.measurements or {}).get("fit", {}),
             "friction": (r.measurements or {}).get("friction", {}),
             "integrity": (r.measurements or {}).get("integrity", {}),
             "shape": (r.measurements or {}).get("shape", {})}
            for r in results
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "scorecard.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    (out_dir / "scorecard.md").write_text(markdown(payload), encoding="utf-8")
    return path


def markdown(payload: dict[str, Any]) -> str:
    card = payload["scorecard"]
    env = payload["environment"]
    lines = [
        f"# bench · {payload['suite']}",
        "",
        f"{card['ran']}/{card['tasks']} tasks ran · {card['met']}/{card['tasks']} met their "
        f"brief · {card['sound']}/{card['tasks']} exported sound",
        "",
        "| | |",
        "|---|---|",
        f"| Fit rate | **{card['fit_rate']:.3f}** ({card['clean_slides']}/{card['slides']} "
        "slides fit their boxes) |",
        f"| Refused writes | {card['refusals']} of {card['tool_calls']} tool calls "
        f"({card['refusal_rate']:.1%}) |",
        f"| Repairs and rounds | {card['rounds']} rounds |",
        f"| Editorial findings | {card['review_findings']} "
        f"({card['findings_per_slide']} per slide) |",
        f"| Export violations | {card['violations']} |",
        f"| Parts lost on round trip | {card['parts_lost']} |",
        f"| Wall clock | {card['seconds']}s |",
        "",
        f"Model **{env['model'] or 'unset'}** · {env['platform']} · Python {env['python']} · "
        f"renderer {env['reference_renderer'] or 'none'}. Produced {env['when']}.",
        "",
        "Fit, refusals and parts are measured, not judged — they mean the same thing on "
        "every run. Nothing here says the deck is *good*; run `bench adapters` to submit "
        "these same artifacts to a benchmark that judges that.",
        "",
        "## Tasks",
        "",
        "| task | kind | ran | met | slides | fit | refusals | findings |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for task in payload["tasks"]:
        fit = task.get("fit", {})
        friction = task.get("friction", {})
        shape = task.get("shape", {})
        missed = [k for k, v in (task.get("expectations") or {}).items() if not v]
        lines.append(
            f"| {task['task']} | {task['kind']} | {'yes' if task['ok'] else 'NO'} | "
            f"{'yes' if task['met'] else ('missed ' + ', '.join(missed) if missed else 'NO')} | "
            f"{fit.get('slides', 0)} | {fit.get('fit_rate', 0):.2f} | "
            f"{friction.get('refusals', 0)} | {shape.get('review_findings', 0)} |"
        )
    failed = [t for t in payload["tasks"] if t.get("error")]
    if failed:
        lines += ["", "## Failures", ""]
        lines += [f"- **{t['task']}** — {t['error']}" for t in failed]
    return "\n".join(lines) + "\n"

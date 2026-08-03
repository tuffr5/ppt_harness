"""CLI — inspection without a model in the loop.

Deliberately read-heavy. The point is to be able to answer "what does the harness think
this deck is" and "would this text fit" without starting a conversation, which is what
makes the measurement layer debuggable.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..core.session import Session
from ..render import fonts
from ..state.theme_default import validate_theme
from ..tools import router
from ..tools.deck import SLIDE_SIZES


@click.group()
def cli() -> None:
    """ppt-harness — inspect, measure, and export presentations."""
    from ..core.config import load_env

    load_env()


@cli.command()
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--title", default="Untitled", help="Deck title, and the title slide's text.")
@click.option("--from", "borrow", type=click.Path(exists=True, path_type=Path),
              help="Take the theme from this deck — palette, fonts, grid.")
@click.option("--template", "builtin", default=None,
              help="Use a theme that ships with the harness; `ppt-harness templates` "
                   "lists them.")
@click.option("--size", default=None,
              type=click.Choice(sorted(SLIDE_SIZES)), help="Canvas size. Default 16:9.")
@click.option("--empty", is_flag=True, help="No title slide; start with nothing.")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
def new(path: Path, title: str, borrow: Path | None, builtin: str | None,
        size: str | None, empty: bool, force: bool) -> None:
    """Create a deck.

    Offline and deterministic, like every other command here — it builds an empty themed
    deck, it does not write content. Generating *slides* needs a model, which is what
    `serve` is for.

    `--from` is the useful case: start something new that matches a deck you already have,
    borrowing its palette, fonts and grid without copying its slides.
    """
    from ..state import templates as builtins
    from ..tools import router

    if path.exists() and not force:
        raise click.ClickException(f"{path} exists; pass --force to overwrite it")
    if borrow and builtin:
        raise click.ClickException(
            "--from borrows a deck's theme and --template names a built-in one; pick one")

    try:
        session = (Session.from_template(borrow, title) if borrow
                   else Session.from_builtin(builtin, title) if builtin
                   else Session.blank(title))
    except builtins.TemplateError as exc:
        raise click.ClickException(str(exc)) from exc

    if size:
        result = router.dispatch(session, "set_slide_size", {"preset": size})
        if not result["ok"]:
            raise click.ClickException(result["message"])

    if not empty:
        result = router.dispatch(session, "add_slide", {
            "layout": "title",
            "blocks": [{"region": "hero", "component": "title_slide",
                        "variant": "centered", "slots": {"title": title}}],
        })
        if not result["ok"]:
            raise click.ClickException(result["message"])

    result = router.dispatch(session, "export", {"path": str(path)})
    if not result["ok"]:
        raise click.ClickException(result["message"])

    canvas = session.theme.grid.canvas
    origin = (f" borrowing the theme from {borrow.name}" if borrow
              else f" on the built-in {builtin} theme" if builtin else "")
    click.echo(f"{path}  ·  {len(session.deck.slides)} slide(s)  ·  "
               f"{canvas[0]}x{canvas[1]}{origin}")
    if session.theme_problems:
        click.echo(click.style("  theme warnings: " + "; ".join(session.theme_problems),
                               fg="yellow"))
    click.echo("  add slides with `ppt-harness serve` — that is the part that needs a model")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
def outline(deck: Path) -> None:
    """Slide index, mode, and gist."""
    session = Session.open(deck)
    data = session.outline()
    click.echo(f"{data['deck']} · theme {data['theme']} · {len(data['slides'])} slides")
    for slide in data["slides"]:
        detail = (f"{slide['shapes']} shapes" if "shapes" in slide
                  else f"{slide['layout']}: {', '.join(slide['blocks'])}")
        click.echo(f"  {slide['index']:>2}  {slide['id']:<12} {slide['mode']:<9} "
                   f"{detail:<28} {slide['gist'][:44]}")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
def theme(deck: Path) -> None:
    """Extracted theme, and what had to be inferred."""
    session = Session.open(deck)
    t = session.theme
    click.echo(f"id       {t.id}  ({t.source})")
    click.echo(f"canvas   {t.grid.canvas[0]}x{t.grid.canvas[1]}  margin {t.grid.margin}  "
               f"{t.grid.columns} cols  gutter {t.grid.gutter}")
    click.echo("palette")
    for role, value in t.palette.items():
        click.echo(f"  {role:<12} {value}")
    click.echo("type")
    for role, spec in t.type.scale.items():
        click.echo(f"  {role:<12} {spec.family:<8} {spec.size}px/{spec.line}px  w{spec.weight}")
    click.echo("families")
    for name, stack in t.type.families.items():
        resolved = fonts.resolve(stack, "latin").name
        click.echo(f"  {name:<12} {stack}")
        click.echo(f"  {'':<12} -> resolves to {resolved}")
    click.echo(f"inferred {', '.join(t.inferred)}")
    problems = validate_theme(t)
    click.echo(f"validate {'PASS' if not problems else chr(10).join(problems)}")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.option("--slide", "slide_id", default=None, help="Limit to one slide.")
def lint(deck: Path, slide_id: str | None) -> None:
    """Overflow and theme problems."""
    session = Session.open(deck)
    report = router.dispatch(session, "lint", {"slide_id": slide_id} if slide_id else {})
    if report["clean"]:
        click.echo(click.style("clean", fg="green") + f" · {report['checked']} slides checked")
        return
    for problem in report["problems"]:
        click.echo(click.style(f"{problem['slide']}", fg="yellow")
                   + f"  overflow {problem['overflow_px']}px")
        for item in problem.get("shapes", problem.get("slots", [])):
            if not item["fits"]:
                click.echo(f"    {item['target']}  {item['lines']}/{item['max_lines']} lines")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.option("--slide", "slide_id", default=None, help="Limit to one slide.")
def review(deck: Path, slide_id: str | None) -> None:
    """Editorial findings. Advisory — `lint` is the measured one."""
    session = Session.open(deck)
    args = {"slide_id": slide_id} if slide_id else {}
    report = router.dispatch(session, "review_deck", args)
    if not report["findings"]:
        click.echo(click.style("nothing to say", fg="green")
                   + f" · {report['checked']} slides read")
        return
    for finding in report["findings"]:
        where = finding.get("slide", "deck")
        click.echo(click.style(f"{where:<6}", fg="yellow")
                   + click.style(finding["rule"], fg="cyan"))
        click.echo(f"    {finding['message']}")
        click.echo(click.style(f"    → {finding['suggestion']}", dim=True))


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.argument("target")
@click.argument("text")
def fits(deck: Path, target: str, text: str) -> None:
    """Would TEXT fit at TARGET? Reports capacity without writing anything."""
    session = Session.open(deck)
    budget = session.budget_for(target)
    from ..render import budget as budget_mod

    result = budget_mod.check(text, budget, session.theme)
    click.echo(click.style("fits" if result.ok else "does not fit",
                           fg="green" if result.ok else "red"))
    click.echo(f"  capacity {budget.capacity_em:.1f}ew  used {result.used_em:.1f}ew  "
               f"lines {result.lines}/{budget.max_lines}  script {result.script}")
    click.echo(f"  hint {budget.hint(session.theme)}")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.option("--original", type=click.Path(exists=True, path_type=Path), default=None,
              help="Baseline against the source, so inherited faults are not blamed on you.")
@click.option("--fix", is_flag=True, help="Remove orphaned images and their relationships.")
def validate(deck: Path, original: Path | None, fix: bool) -> None:
    """Structural problems in the package itself.

    Below the document model: parts, content types, and the relationship graph. Those are
    what python-pptx cannot see and where an edited deck rots — a deleted picture leaves its
    relationship and its image behind, and PowerPoint never complains.

    `--original` matters on any real deck. Files arrive carrying debris from whatever made
    them, and a report that blames this edit for all of it is a report nobody reads.
    """
    from ..io import package

    if fix:
        for gone in package.sweep(deck):
            click.echo(click.style(f"  removed {gone}", fg="yellow"))

    report = package.audit(deck, original=original)
    for finding in report.findings:
        click.echo(click.style(f"  {finding.kind}", fg="red") + f"  {finding.part}")
        click.echo(f"      {finding.detail}")
    if report.clean:
        click.echo(click.style("clean", fg="green")
                   + ("" if not report.inherited
                      else f" · {len(report.inherited)} pre-existing problem(s) ignored"))
        return
    click.echo(f"{len(report.findings)} problem(s)"
               + (f" · {len(report.inherited)} inherited and not counted"
                  if report.inherited else ""))
    raise SystemExit(1)


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.argument("out", type=click.Path(path_type=Path))
def export(deck: Path, out: Path) -> None:
    """Round-trip a deck through the harness and assert fidelity."""
    session = Session.open(deck)
    report = router.dispatch(session, "export", {"path": str(out)})
    click.echo(report["summary"])
    for violation in report["violations"]:
        click.echo(click.style(f"  {violation}", fg="red"))


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path), required=False)
@click.argument("tool")
@click.option("--args", "raw", default="{}", help="JSON arguments.")
def call(deck: Path | None, tool: str, raw: str) -> None:
    """Call any tool directly. The same path the model takes."""
    session = Session.open(deck) if deck else Session.blank()
    click.echo(json.dumps(router.dispatch(session, tool, json.loads(raw)),
                          indent=1, ensure_ascii=False))


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
@click.option("--model", default=None,
              help="Model name, e.g. claude-opus-4-8 / deepseek-chat / gpt-4o; "
                   "default $PPT_HARNESS_MODEL.")
@click.option("--base-url", default=None,
              help="OpenAI-compatible endpoint; default $PPT_HARNESS_BASE_URL.")
@click.option("--fresh", is_flag=True,
              help="Ignore saved work for this deck and start from the file on disk.")
@click.option("--from", "template", type=click.Path(exists=True, path_type=Path),
              default=None,
              help="Start a new deck on this one's theme — palette, faces and grid, no "
                   "slides. The way most decks actually start.")
@click.option("--template", "builtin", default=None,
              help="Start on a theme that ships with the harness; `ppt-harness templates` "
                   "lists them.")
@click.option("--title", default="Untitled",
              help="Deck title for a new deck. Ignored when DECK is given.")
def serve(deck: Path | None, host: str, port: int, model: str | None,
          base_url: str | None, fresh: bool, template: Path | None, title: str,
          builtin: str | None) -> None:
    """Chat and live preview in a browser.

    With DECK, opens that file; edits are journalled as they happen, so closing the tab or
    losing the process picks up where it left off, and `--fresh` discards that history.

    With `--from`, starts a *new* deck wearing another deck's theme — which is how a deck
    usually starts, since nobody opens a blank canvas when a company template exists. The
    slides are written in the chat, because that is the part that needs a model.
    """
    from ..state import templates as builtins
    from .web import serve as run

    if sum(bool(x) for x in (deck, template, builtin)) > 1:
        raise click.ClickException(
            "pass a deck to open one, --from to start on another deck's theme, or "
            "--template to start on a built-in one — not more than one. Each answers the "
            "same question, and they would answer it differently."
        )
    if builtin:
        try:
            builtins.load(builtin)      # fail here, not sixty seconds into a browser
        except builtins.TemplateError as exc:
            raise click.ClickException(str(exc)) from exc

    click.echo(f"http://{host}:{port}")
    if template:
        click.echo(f"new deck on the theme from {template.name} — no slides copied")
    if builtin:
        click.echo(f"new deck on the built-in {builtin} theme")
    run(str(deck) if deck else None, host=host, port=port, model=model, base_url=base_url,
        fresh=fresh, template=str(template) if template else None, title=title,
        builtin=builtin)


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.argument("slide_id")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Write a PNG.")
def freeze(deck: Path, slide_id: str, out: Path | None) -> None:
    """Lay a slide out in a real browser and report the geometry that would be exported."""
    from ..render import browser

    session = Session.open(deck)
    slide = session.deck.slide(slide_id)
    if slide is None:
        raise click.ClickException(f"no slide {slide_id!r}")
    cx, cy = session.slide_size_emu()
    try:
        frozen = browser.freeze(session.theme, slide, cx, cy)
    except browser.BrowserUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{slide_id}  {len(frozen.boxes)} boxes  "
               + click.style("clean" if frozen.clean else f"overflow {frozen.overflow_px:.0f}px",
                             fg="green" if frozen.clean else "red"))
    for b in frozen.boxes:
        flag = "" if b.fits else click.style(f"  +{b.overflow_px:.0f}px", fg="red")
        click.echo(f"  {b.target:<22} ({b.x:>6.0f},{b.y:>6.0f}) {b.w:>6.0f}x{b.h:<6.0f} "
                   f"{b.lines} lines{flag}")
    notes = browser.compare_with_analytic(frozen, session.measure_slide(slide_id, freeze=False))
    for note in notes:
        click.echo(click.style(f"  disagreement: {note}", fg="yellow"))
    if out:
        out.write_bytes(browser.screenshot_slide(session.theme, slide, cx, cy))
        click.echo(f"wrote {out}")


@cli.command()
@click.argument("deck", type=click.Path(exists=True, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), default=Path("fidelity-out"),
              help="Where to write the renders and side-by-side images.")
@click.option("--slide", "slides", multiple=True, help="Limit to these slide ids.")
def compare(deck: Path, out: Path, slides: tuple[str, ...]) -> None:
    """Render every slide twice — harness and a real renderer — and report the difference.

    Ground truth is PowerPoint where installed, LibreOffice otherwise. Opens the
    application, so this is a development tool and never part of a turn.
    """
    from ..fidelity import compare as comparer
    from ..fidelity import reference

    engine = reference.available()
    if engine is None:
        raise click.ClickException(
            "no reference renderer: install LibreOffice, or run on a Mac with PowerPoint")
    click.echo(f"reference: {engine}")

    results = comparer.compare(deck, out, slides=list(slides) or None)
    for line in comparer.report(results).splitlines():
        colour = None
        if line.endswith("wrong"):
            colour = "red"
        elif line.endswith("drifting"):
            colour = "yellow"
        elif line.endswith("close"):
            colour = "green"
        click.echo(click.style(line, fg=colour) if colour else line)
    click.echo(f"\nside-by-side images in {out}/")


@cli.command(name="tools")
def list_tools() -> None:
    """Every tool, with its mode gate."""
    for t in router.tools():
        flag = "write" if t.mutating else "read "
        click.echo(f"  {t.gate:<9} {flag}  {t.name:<16} {t.description}")


@cli.group()
def bench() -> None:
    """Measure the harness against tasks, corpora, and public benchmarks."""


@bench.command(name="suites")
def bench_suites() -> None:
    """Task suites that ship with the harness."""
    from ..bench import tasks as task_lib

    for name, path in task_lib.suites().items():
        found = task_lib.load(path)
        kinds = ", ".join(sorted({t.kind for t in found}))
        click.echo(f"  {name:<10} {len(found):>3} tasks  [{kinds}]")


@bench.command(name="run")
@click.option("--suite", default="core", help="Task suite to run.")
@click.option("--out", type=click.Path(path_type=Path), default=Path(".harness/bench"),
              help="Where artifacts and the scorecard go.")
@click.option("--limit", type=int, default=None, help="Run only the first N tasks.")
@click.option("--task", "only", multiple=True, help="Run these task ids.")
@click.option("--model", default=None, help="Model name; default $PPT_HARNESS_MODEL.")
@click.option("--base-url", default=None, help="OpenAI-compatible endpoint.")
@click.option("--template", default=None,
              help="Start every deck on this template — a .pptx path or a built-in name.")
def bench_run(suite: str, out: Path, limit: int | None, only: tuple[str, ...],
              model: str | None, base_url: str | None, template: str | None) -> None:
    """Run a task suite end to end and write a scorecard.

    This costs model calls — one agent turn per brief, plus the follow-ups — so it is the
    expensive half. `bench corpus` measures round-trip preservation with no model at all.
    """
    from ..bench import report as report_lib
    from ..bench import runner
    from ..bench import tasks as task_lib

    try:
        found = task_lib.load_suite(suite)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    if only:
        found = [t for t in found if t.id in set(only)]
    if limit:
        found = found[:limit]
    if not found:
        raise click.ClickException("no tasks selected")

    results = []
    for index, task in enumerate(found, 1):
        click.echo(f"[{index}/{len(found)}] {task.id} … ", nl=False)
        result = runner.run_task(task, out / suite / task.id, model=model,
                                 base_url=base_url, template=template,
                                 suite_dir=task_lib.suites_root())
        results.append(result)
        fit = (result.measurements or {}).get("fit", {})
        click.echo(
            click.style("ok", fg="green") if result.met else click.style("miss", fg="yellow")
            if result.ok else click.style("FAILED", fg="red")
        )
        if result.error:
            click.echo(f"      {result.error}")
        else:
            click.echo(f"      {fit.get('slides', 0)} slides · fit "
                       f"{fit.get('fit_rate', 0):.2f} · "
                       f"{(result.measurements or {}).get('friction', {}).get('seconds', 0)}s")

    path = report_lib.write(results, out / suite, suite=suite,
                            model=results[0].model if results else "")
    card = report_lib.score(results)
    click.echo("")
    click.echo(f"{card.met}/{card.tasks} met the brief · fit rate "
               + click.style(f"{card.fit_rate:.3f}", fg="green" if card.fit_rate == 1 else "yellow")
               + f" over {card.slides} slides · {card.refusals} refused writes")
    click.echo(f"{path.parent}/scorecard.md")


@bench.command(name="corpus")
@click.argument("corpus", type=click.Path(exists=True, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path),
              default=Path(".harness/bench/corpus"))
@click.option("--limit", type=int, default=None, help="Only the first N decks.")
@click.option("--edit", is_flag=True,
              help="Set one title before exporting — the smallest real edit.")
def bench_corpus(corpus: Path, out: Path, limit: int | None, edit: bool) -> None:
    """Round-trip every .pptx under CORPUS and report what survived.

    No model, no judge, no network. This is the claim the README makes loudest — that
    patching the original package preserves what the harness does not model — measured over
    real files instead of one fixture.
    """
    from ..bench import corpus as corpus_lib

    payload = corpus_lib.run(corpus, out, limit=limit, edit=edit)
    click.echo(f"{payload['decks']} decks · {payload['opened']} opened "
               f"({payload['open_rate']:.1%})")
    click.echo("preserved " + click.style(
        f"{payload['preserved']}/{payload['opened']}",
        fg="green" if payload["preservation_rate"] == 1 else "yellow")
        + f" ({payload['preservation_rate']:.2%}) · {payload['slides']} slides · "
        f"{payload['opaque_shapes']} opaque shapes")
    if payload["lost_parts"]:
        click.echo(click.style(f"  lost part families: {len(payload['lost_parts'])}",
                               fg="red"))
        for part in payload["lost_parts"][:10]:
            click.echo(f"    {part}")
    for failure in payload["failures"][:5]:
        click.echo(click.style(f"  {Path(failure['path']).name}: {failure['error'][:80]}",
                               fg="yellow"))
    click.echo(f"{out}/corpus.json")


@bench.command(name="slidesbench")
@click.option("--repo", type=click.Path(exists=True, path_type=Path), required=True,
              help="A clone of github.com/para-lost/AutoPresent.")
@click.option("--domain", default="food",
              help="Example domain. Only `food` ships a reference deck.")
@click.option("--limit", type=int, default=5)
@click.option("--variant", default="instruction_high_level.txt",
              help="Which instruction file to use.")
@click.option("--python", "interpreter", type=click.Path(path_type=Path), default=None,
              help="Python with their eval deps; default <repo>/../sb-venv/bin/python.")
@click.option("--out", type=click.Path(path_type=Path),
              default=Path(".harness/bench/slidesbench"))
@click.option("--model", default=None)
def bench_slidesbench(repo: Path, domain: str, limit: int, variant: str,
                      interpreter: Path | None, out: Path, model: str | None) -> None:
    """Score against SlidesBench, using their evaluator.

    One instruction, one slide, their metrics. Every figure is reported beside the score
    their own reference gets against *itself* — which is 100 for match, text and position,
    and 25 for colour. A number from this benchmark without that baseline is not readable.
    """
    from ..bench import slidesbench as bench_lib

    payload = bench_lib.run(repo, out, domain=domain, limit=limit, variant=variant,
                            model=model, python=interpreter)
    if not payload["scored"]:
        raise click.ClickException(
            f"nothing scored out of {payload['examples']} example(s); see {out}")

    click.echo("")
    click.echo(f"{payload['benchmark']} · {payload['domain']} · "
               f"{payload['scored']}/{payload['examples']} scored")
    click.echo(f"  {'dimension':<10} {'ours':>6} {'identity':>9}")
    for key, value in payload["ours"].items():
        base = payload["identity_baseline"].get(key)
        click.echo(f"  {key:<10} {value:>6} {base if base is not None else '-':>9}")
    click.echo(f"\n{out}/slidesbench.json")


@bench.command(name="publish")
@click.option("--from", "source", type=click.Path(exists=True, path_type=Path),
              default=Path(".harness/bench"), help="Where the runs are.")
@click.option("--out", type=click.Path(path_type=Path), default=Path("benchmarks"),
              help="Where the committed results and charts go.")
def bench_publish(source: Path, out: Path) -> None:
    """Collect finished runs into charts, tables and JSON that can be committed.

    Runs land in `.harness/`, which is working state and gitignored. Evidence belongs
    somewhere a reader can find it without re-running anything, so this copies the JSON out,
    draws the charts, and writes a report that reads them.
    """
    import json as json_lib
    from typing import Any

    from ..bench import plot

    runs: dict[str, Any] = {}
    for path in sorted(source.rglob("slidesbench.json")):
        blob = json_lib.loads(path.read_text(encoding="utf-8"))
        # Name a run by its variant, so two conditions do not overwrite each other.
        # Human labels, because these become a chart legend. The variant filename is the
        # identifier; "instruction.txt" as a legend entry tells a reader nothing.
        variant = blob.get("variant", "")
        name = ("high-level brief" if "high_level" in variant
                else "detailed brief" if variant.startswith("instruction")
                else path.parent.name)
        runs[name] = blob
    for name, pattern in (("corpus", "corpus.json"), ("tasks", "scorecard.json")):
        found = sorted(source.rglob(pattern))
        if found:
            runs[name] = json_lib.loads(found[-1].read_text(encoding="utf-8"))

    if not runs:
        raise click.ClickException(f"no finished runs under {source}")

    report = plot.publish(runs, out)
    click.echo(f"{report}")
    for chart in sorted((out / "charts").glob("*.svg")):
        click.echo(f"  {chart}  ·  {chart.stat().st_size // 1000} kB")
    click.echo(f"  {len(list((out / 'results').glob('*.json')))} json snapshot(s)")


@bench.command(name="submit")
@click.option("--suite", default="core")
@click.option("--out", type=click.Path(path_type=Path), default=Path(".harness/bench"))
@click.option("--to", "which", type=click.Choice(["presentbench", "ppteval", "slidesbench"]),
              required=True)
def bench_submit(suite: str, out: Path, which: str) -> None:
    """Lay out a finished run in the shape a public benchmark's own scripts expect.

    Their judge stays theirs — nothing here re-implements or vendors a public benchmark, it
    only writes the artifacts where those scripts look for them and prints the command.
    """
    import json as json_lib

    from ..bench import adapters
    from ..bench.runner import Result

    root = out / suite
    results = []
    for path in sorted(root.glob("*/result.json")):
        blob = json_lib.loads(path.read_text(encoding="utf-8"))
        results.append(Result(task=blob["task"], kind=blob["kind"], ok=blob["ok"],
                              measurements=blob.get("measurements", {}),
                              expectations=blob.get("expectations", {}),
                              artifacts=blob.get("artifacts", {})))
    if not results:
        raise click.ClickException(f"no finished run in {root}; run `bench run` first")

    submission = adapters.ADAPTERS[which](results, out / "submissions" / which)
    click.echo(f"{submission.benchmark}: {submission.cases} case(s) in {submission.root}")
    if submission.missing:
        click.echo(click.style(
            f"  {len(submission.missing)} could not be rendered: "
            + ", ".join(submission.missing[:5]), fg="yellow"))
    click.echo(f"  run:  {submission.command}")


@cli.command(name="templates")
@click.argument("name", required=False)
def list_templates(name: str | None) -> None:
    """Themes that ship with the harness, for a deck with nothing to borrow from.

    With NAME, prints that template's palette and type — the same values a deck built on it
    will carry, since an authored theme states them rather than inferring them.
    """
    from ..state import templates

    if name:
        try:
            theme = templates.load(name)
        except templates.TemplateError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{name}  ·  canvas {theme.grid.canvas[0]}x{theme.grid.canvas[1]}  "
                   f"margin {theme.grid.margin}")
        for role, value in theme.palette.items():
            shown = ", ".join(value) if isinstance(value, list) else value
            click.echo(f"  {role:<12} {shown}")
        for role, spec in theme.type.scale.items():
            click.echo(f"  {role:<12} {spec.family} {spec.size}px/{spec.line}px w{spec.weight}")
        for family, stack in theme.type.families.items():
            click.echo(f"  {family:<12} {stack}")
        return

    found = templates.catalog()
    if not found:
        click.echo("no templates installed")
        return
    for template in found:
        click.echo(f"  {template.name:<12} {template.summary()}")
        click.echo(f"  {'':<12} {template.description}")
    click.echo("\nstart one with:  ppt-harness serve --template <name>")


@cli.command(name="skills")
@click.argument("name", required=False)
def list_skills(name: str | None) -> None:
    """Playbooks and invariants that ship with the harness.

    With NAME, prints that skill's body — the same text a model receives, so what the
    harness tells a model is readable by the person who has to trust it.
    """
    from ..core import skills as skill_lib

    if name:
        try:
            click.echo(skill_lib.get(name).body)
        except skill_lib.SkillError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    found = skill_lib.load()
    if not found:
        click.echo("no skills installed")
        return
    for skill in found:
        click.echo(f"  {skill.kind:<9} {skill.name:<16} {skill.description[:60]}")


if __name__ == "__main__":
    cli()

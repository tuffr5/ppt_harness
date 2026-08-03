"""Record a video walkthrough of the web client — a deck built from a template, on camera.

    uv run python scripts/record_demo.py
    uv run python scripts/record_demo.py --acts open,build,ship        # a shorter cut
    uv run python scripts/record_demo.py --template company.pptx --title "FY26 kickoff"

Starts the server, drives the real UI in Chromium, and writes a `.webm`. **Nothing is
simulated.** Every chat turn goes to whichever model `.env` selects, the tool cards are the
ones that model actually produced, the slides are whatever it actually built, and the preview
is the exported file rendered. A demo that faked any of that would be worth less than no
demo — the whole claim of this harness is that what you see is what ships, and a staged
recording would be evidence against it.

The recording follows the arc a real deck follows, in five acts:

1. **open** — a *new* deck on a template's theme. Nobody starts from a blank canvas; they
   start from the company file. `serve --from` borrows the palette, the faces and the grid
   and copies no slides, so the deck on screen is empty and already on-brand.
2. **build** — one turn writes the deck from a brief. Tool calls stream as they happen and
   every write comes back with its own measurement.
3. **customise** — the part a template cannot do for you: restyling, changing the shape of a
   slide, correcting the words. Several turns, each an ordinary sentence.
4. **review** — the harness's editorial pass. `review_deck` reads the deck the way an editor
   would — titles that file rather than state, lists that have outgrown themselves, style
   that drifts between slides — and the model acts on what it finds.
5. **ship** — measurement drawn over the render, then export.

Three things make the result watchable rather than merely correct:

- **Pacing.** The browser can click faster than a viewer can read. Every step holds long
  enough to be followed.
- **A caption track.** Playwright cannot overlay text on a video, so each step injects a
  banner into the page itself, removed before the next step. The recording shows the real
  interface with a subtitle rather than a mock-up.
- **The waits are compressed, and say so.** Five real model turns take about ten minutes,
  and roughly eight of those are a spinner. The raw take keeps every frame; the cut speeds
  up *only* the stretches where the harness is waiting on the model, to about six seconds
  each. Nothing is cut, nothing is re-ordered, nothing is re-shot — and the caption on
  screen during those stretches says it is sped up, because a viewer who later discovers
  the pacing was doctored will assume the tool calls were too. `--no-tighten` keeps the
  real-time cut; `--max-wait` sets how long a wait is allowed to run.

The model turns are the expensive part and the only part worth filming. Trim with `--acts`
rather than by faking a turn; every act runs standalone except that anything after `open`
wants slides on screen, so `build` earns its place in most cuts.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Built by `scripts/make_template.py`. Not the test fixture, deliberately: that deck carries
#: the stock Office theme, and a recording that claims a template's palette came across while
#: showing Calibri on white has proved nothing a viewer can see.
DEFAULT_TEMPLATE = ROOT / "tests" / "fixtures" / "brand-template.pptx"

#: Long enough to read a caption of a dozen words, short enough that nobody scrubs past it.
BEAT = 2.2

#: How long a wait on the model may run in the cut. Six seconds is enough to see the tool
#: cards land and the elapsed clock move — which is the evidence that the turn was real —
#: and short enough that nobody reaches for the scrubber.
MAX_WAIT = 6.0

#: A model writing four slides can be quiet for a long time, and quiet is not the same as
#: hung. Generous, because the failure this guards against — cutting the recording off
#: mid-turn — is worse than a few extra seconds of a spinner nobody minds.
TURN_TIMEOUT_MS = 420_000

#: How long to wait for the composer to free up before typing the *next* prompt. Short,
#: because by this point the previous turn has already been waited out in full: if Send is
#: still disabled here, something is wedged and pressing on produces a recording of a
#: disabled button.
IDLE_TIMEOUT_MS = 20_000

#: The caption is fixed to the bottom, where the composer already lives. Without the matching
#: body padding it sits on top of the input box, which reads as a broken UI rather than a
#: subtitle. The chapter card is deliberately a different animal — centred and briefly
#: full-width — because an act break should not look like another sentence of narration.
BANNER_CSS = """
#demo-caption {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 99999;
  background: rgba(9,12,18,.94); color: #e8ecf3;
  font: 500 19px/1.5 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  padding: 18px 26px; border-top: 2px solid #4c8dff;
  letter-spacing: .1px;
}
#demo-caption b { color: #7fb2ff; font-weight: 600; }
#demo-caption i { color: #8b93a3; font-style: normal; }
body.demo-captioned aside { padding-bottom: 74px; }
#demo-chapter {
  position: fixed; inset: 0; z-index: 99998; display: grid; place-items: center;
  background: rgba(6,8,12,.86); color: #e8ecf3;
  font: 600 44px/1.25 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  letter-spacing: -.4px; text-align: center;
}
#demo-chapter .n {
  display: block; font-size: 15px; font-weight: 600; letter-spacing: 2.4px;
  color: #7fb2ff; margin-bottom: 14px; text-transform: uppercase;
}
#demo-chapter .sub {
  display: block; font-size: 19px; font-weight: 400; color: #8b93a3; margin-top: 16px;
  max-width: 780px;
}
"""


def free_port(preferred: int) -> int:
    """`preferred` if nothing holds it, otherwise whatever the OS hands out.

    A demo that dies on "address already in use" is a demo you re-run in front of people.
    """
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_server(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.4)
    return False


# --------------------------------------------------------------------------- the stage


@dataclass
class Stage:
    """The page, the pacing, and what went wrong.

    Failures are collected rather than raised. A turn the model fluffs is worth finishing the
    recording around — the acts after it still demonstrate what they demonstrate — but it
    must not be reported as a success, so `problems` decides the exit code at the end.

    It also keeps the clock. `waits` is every stretch where the harness was blocked on the
    model, in seconds from the first frame, which is what lets the cut speed up the dead air
    and nothing else. Measured here rather than inferred from the video afterwards, because
    "the page looked busy" is a guess and "we were inside wait_for_function" is a fact.
    """

    page: object
    beat: float = BEAT
    problems: list[str] = field(default_factory=list)
    t0: float = 0.0
    """`time.monotonic()` at the first recorded frame — the video's own zero."""
    waits: list[tuple[float, float]] = field(default_factory=list)
    compressed: bool = True
    """Whether the cut will speed the waits up. Only affects what the captions promise."""

    def at(self) -> float:
        return time.monotonic() - self.t0

    # -- captions -----------------------------------------------------------

    def caption(self, html: str, hold: float = 1.0) -> None:
        self.page.evaluate(
            """(html) => {
                let el = document.getElementById('demo-caption');
                if (!el) {
                    el = document.createElement('div');
                    el.id = 'demo-caption';
                    document.body.appendChild(el);
                    document.body.classList.add('demo-captioned');
                }
                el.innerHTML = html;
            }""",
            html,
        )
        if hold:
            time.sleep(self.beat * hold)

    def clear_caption(self) -> None:
        self.page.evaluate("""() => {
            document.getElementById('demo-caption')?.remove();
            document.body.classList.remove('demo-captioned');
        }""")

    def chapter(self, number: int, title: str, subtitle: str = "") -> None:
        """An act break. Held longer than a caption because it is a place to breathe."""
        self.page.evaluate(
            """([n, title, sub]) => {
                const el = document.createElement('div');
                el.id = 'demo-chapter';
                el.innerHTML = `<div><span class="n">${n}</span>${title}` +
                               (sub ? `<span class="sub">${sub}</span>` : '') + `</div>`;
                document.body.appendChild(el);
            }""",
            [f"Act {number}", title, subtitle],
        )
        time.sleep(self.beat * 1.25)
        self.page.evaluate("() => document.getElementById('demo-chapter')?.remove()")

    # -- the UI -------------------------------------------------------------

    def slides(self) -> int:
        return self.page.locator(".chip").count()

    def settle(self) -> None:
        """Let the slide the turn touched stay on screen.

        Deliberately *not* "click the last chip", which is what this used to do. The client
        already switches to the slide a turn wrote — it reads the target off the stream — and
        overriding that with the last slide in the deck is why an early cut spent nine of its
        twelve sampled frames on the same unchanged closing slide while four turns went by.
        """
        self.page.wait_for_timeout(900)
        self.check_preview()

    def flip_through(self, hold: float = 1.5, wide: bool = True) -> None:
        """Walk every slide, with the chat out of the way.

        The first cut of this demo showed one slide for nine of its twelve minutes: the
        recorder clicked the *last* chip after every turn, and the last slide happened not to
        change. A deck was built, restructured, reviewed and edited, and the picture on the
        right never moved — which reads as a tool that did nothing.

        The drawer collapses for it because a slide is the artifact. The transcript is
        evidence and deserves the screen while a turn runs; between turns it is in the way.
        """
        chips = self.page.locator(".chip")
        count = chips.count()
        if not count:
            return
        if wide:
            self.page.click("#drawer")
            self.page.wait_for_timeout(500)
        for index in range(count):
            chips.nth(index).click()
            self.page.wait_for_timeout(int(hold * 1000))
        self.check_preview()
        if wide:
            self.page.click("#drawer")
            self.page.wait_for_timeout(400)

    def check_preview(self) -> None:
        """Did the preview pane actually draw the exported slide?

        Worth checking because the failure is silent and the recording is not rerunnable
        cheaply. The preview goes out through the real exporter into a real renderer, and on
        macOS that renderer is PowerPoint — which returns `error -9074` when two processes
        drive it at once. The endpoint then 503s, the `<img>` breaks, and the pane shows a
        white rectangle with an alt string in the corner. Every caption still reads correctly
        over it, which is exactly what makes it dangerous: the take looks fine until someone
        watches it.

        A broken image is a `problem`, not an exception. There is no point abandoning a
        recording that is otherwise real — but it must not exit 0 either.
        """
        state = self.page.evaluate("""() => {
            const doc = document.querySelector('#frame')?.contentDocument;
            if (!doc) return 'no-frame';
            const img = doc.querySelector('img');
            if (!img) return 'html-fallback';   // no Office renderer; not a failure
            return (img.complete && img.naturalWidth > 0) ? 'ok' : 'broken';
        }""")
        note = ("the preview did not render — the .pptx renderer refused. On macOS that is "
                "usually PowerPoint under concurrency (error -9074): nothing else may drive "
                "it while recording, tests included.")
        if state == "broken" and note not in self.problems:
            self.problems.append(note)

    def scroll_log(self) -> None:
        self.page.evaluate("() => { const l = document.querySelector('#log');"
                           " if (l) l.scrollTop = l.scrollHeight; }")

    def recover(self) -> None:
        """Reload the page and put the captions back.

        The style tag and the caption element live in the document, so a reload takes them
        with it; re-adding the stylesheet is what keeps the rest of the recording captioned.
        """
        try:
            self.page.reload(wait_until="networkidle")
            self.page.add_style_tag(content=BANNER_CSS)
            self.page.wait_for_timeout(1500)
        except Exception as exc:
            self.problems.append(f"could not reload after a stalled turn: {exc}")

    def idle(self, timeout_ms: int) -> bool:
        """Is the composer free? The Send button is the client's own answer to that."""
        try:
            self.page.wait_for_function(
                "() => document.querySelector('#send') && "
                "!document.querySelector('#send').disabled",
                timeout=timeout_ms)
            return True
        except Exception:
            return False

    def tools_used(self) -> list[str]:
        """Every tool card on the page, in order. The transcript, read off the DOM.

        Printed to the console rather than shown on screen: the viewer can already see the
        cards, and the person who ran the recording wants to know what the model actually
        called without scrubbing the video to find out.
        """
        return self.page.locator(".tool summary code").all_text_contents()

    def turn(self, prompt: str, *, before: str, during: str) -> bool:
        """One chat turn, typed and waited out.

        Waits on the **Send button**, not on `.msg.assistant`: the greeting is already an
        assistant message, so that selector matches before the turn even starts and the
        recording ends mid-round. The client re-enables Send when the stream closes, which is
        the only signal that means "done".
        """
        # Never type into a composer that is still busy. A stalled turn used to end the run
        # in a `Page.click` timeout thirty seconds later — a crash that discarded the whole
        # recording over one slow request, and reported it as a Playwright error rather than
        # as the provider stall it was.
        if not self.idle(IDLE_TIMEOUT_MS):
            self.problems.append(f"composer still busy; skipped: {prompt[:60]}")
            self.caption("The previous turn never closed — skipping ahead. "
                         "The provider stalled, not the harness.", hold=1.2)
            return False

        self.caption(before, hold=1.0)
        self.page.fill("#prompt", prompt)
        time.sleep(0.8)
        self.page.click("#send")
        # The note is part of the caption rather than a separate badge: it has to be legible
        # in the same glance as the claim it qualifies, or it is not a disclosure.
        self.caption(during + (
            " <i>— real time from here; this wait is sped up in the cut.</i>"
            if self.compressed else ""), hold=0)

        before_count = len(self.tools_used())
        started = self.at()
        finished = self.idle(TURN_TIMEOUT_MS)
        self.waits.append((started, self.at()))
        if not finished:
            self.problems.append(f"turn timed out: {prompt[:60]}")
            self.caption("That turn ran long. Reloading — the transcript is replayed from "
                         "the server, so nothing is lost.", hold=1.2)
            # Recover rather than limp. A client left mid-stream keeps Send disabled for
            # ever, so every later act is skipped and the recording becomes ten minutes of a
            # greyed-out button — which is what one run actually produced. The page reload
            # replays the transcript from the server's own message list, so the conversation
            # on screen after it is the real one.
            self.recover()
            return False

        # Let the last tool card settle and the reply render before moving on.
        self.page.wait_for_timeout(2200)
        self.scroll_log()
        self.page.wait_for_timeout(1800)
        called = self.tools_used()[before_count:]
        print(f"  · {prompt[:64]!r} -> {', '.join(called) if called else 'no tool calls'}")
        return True


# ------------------------------------------------------------------------------ acts


def act_open(stage: Stage, template: Path, title: str) -> None:
    """The template start, which is where a real deck begins."""
    stage.chapter(1, "A new deck, on your template",
                  "The palette, the faces and the grid come across. No slides do.")
    stage.caption(
        f"<b>{title}</b> — a deck with nothing in it, started with "
        f"<b>--from {template.name}</b>. The header names where the look came from.",
        hold=1.7)
    stage.caption(
        "A template lends a theme, never content. There is no package to patch here, "
        "so every slide the model adds is <b>managed</b> — components, and geometry derived.",
        hold=1.6)
    stage.caption("The opening line is a real model call, made when the page loaded. "
                  "It has already read the deck it is looking at.", hold=1.5)


def act_build(stage: Stage, brief: str) -> None:
    """One turn, a deck. This is the act that has to be real, and it is."""
    stage.chapter(2, "Say what the deck is for",
                  "One turn. Every write is budget-checked before it lands.")
    stage.turn(
        brief,
        before="Now ask for the deck. The model gets tools — "
               "and <b>never coordinates</b>, not one.",
        during="Each tool call is shown with its result, and every write returns "
               "<b>its own measurement</b>.",
    )
    stage.caption(f"{stage.slides()} slides, built from components on the template's theme. "
                  "The preview is the <b>exported file</b>, rendered.", hold=1.2)
    stage.clear_caption()
    stage.flip_through()
    stage.caption("Brand colour, type and grid — all of it the template's, none of it "
                  "chosen by the model. It never saw a colour or a coordinate.", hold=1.5)


def act_customise(stage: Stage, asks: list[str]) -> None:
    """The half a template cannot do for you: the deck is yours, so change it."""
    stage.chapter(3, "Then make it yours",
                  "Ordinary sentences. The harness owns the geometry, so nothing can break.")
    for i, ask in enumerate(asks):
        stage.turn(
            ask,
            before=("Customisation is just the next sentence — no menus, no dialogs."
                    if i == 0 else
                    "Another change, on the deck as it now stands."),
            during="Watch the tool cards: the model picks a <b>component and a variant</b>, "
                   "never a position.",
        )
        stage.settle()
    stage.caption("Nothing here moved a box by hand. Components own geometry, "
                  "and the theme owns type — so a bad layout is <b>unrepresentable</b>.",
                  hold=1.4)
    stage.clear_caption()
    stage.flip_through(hold=1.2)


def act_review(stage: Stage, ask: str, apply: str | None) -> None:
    """The editorial pass — the axis measurement cannot reach."""
    stage.chapter(4, "Ask what is wrong with it",
                  "lint measures whether it fits. review_deck reads whether it lands.")
    stage.turn(
        ask,
        before="A deck that fits can still be a pile of slides. So ask for the "
               "<b>editorial</b> read: storyline, style, consistency.",
        during="<b>review_deck</b> is analytic — titles that file rather than state, lists "
               "that outgrew themselves, style that drifts. Advisory, never a gate.",
    )
    stage.caption("Findings, each with the fix named. Nothing was changed: an opinion that "
                  "can refuse a write is a style guide holding a gun.", hold=1.7)
    if apply:
        stage.turn(
            apply,
            before="You decide which suggestions to take. That is the whole point of "
                   "keeping them advisory.",
            during="Applying them goes through the same budget-checked writes as anything "
                   "else.",
        )
        stage.settle()
        stage.caption("And the deck as it now stands.", hold=0.8)
        stage.clear_caption()
        stage.flip_through(hold=1.8)


def act_ship(stage: Stage) -> None:
    """Measurement, then a file."""
    stage.chapter(5, "Measured, then shipped",
                  "Overflow in px against real font metrics — not eyeballed.")
    stage.caption("Boxes on: the <b>measured geometry</b>, drawn over the real render.",
                  hold=0)
    probes = stage.page.locator("#probes")
    if probes.count():
        probes.click()
        stage.page.wait_for_timeout(1400)
    time.sleep(stage.beat)
    if probes.count():
        probes.click()

    stage.caption("Every text box is measured against the font that will actually render "
                  "it — HarfBuzz-shaped, resolved per script. ~1ms, no renderer involved.",
                  hold=1.5)
    stage.check_preview()

    stage.caption("Export writes a real <b>.pptx</b>. On an imported deck it patches the "
                  "original package, so SmartArt, media and comments survive.", hold=0)
    stage.page.click("#export")
    try:
        stage.page.wait_for_selector(".msg.system .bubble", timeout=60_000)
    except Exception:
        stage.problems.append("export produced no confirmation")
    stage.scroll_log()
    time.sleep(stage.beat * 1.5)
    stage.caption("Built from a template, changed by asking, checked by measurement, "
                  "and out as a file. <b>ppt-harness</b>.", hold=1.6)


ACTS = ("open", "build", "customise", "review", "ship")


# ------------------------------------------------------------------------- the cut


def _duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def tighten(source: Path, waits: list[tuple[float, float]], out: Path,
            max_wait: float = MAX_WAIT) -> tuple[Path | None, str]:
    """Speed up the stretches where the harness was waiting on the model. Nothing else.

    The alternative — cutting the waits out entirely — is what makes a demo a lie: it
    produces a video in which six slides appear the instant they are asked for, and every
    viewer who has used an LLM knows that is not what happened. Compressing keeps the tool
    cards, the elapsed clock and the round counter on screen, in order, at a speed that shows
    they were real without asking anyone to watch them in real time.

    Returns the path and a one-line account of what it did, because a demo whose pacing was
    edited has to be able to say by how much.
    """
    total = _duration(source)
    if total is None:
        return None, "no ffprobe on PATH; keeping the real-time cut"

    # Merge and clip, so an overlap or a wait that ran past the last frame cannot produce a
    # backwards trim — ffmpeg accepts one and emits a black segment nobody ordered.
    spans: list[list[float]] = []
    for start, end in sorted(waits):
        start, end = max(0.0, start), min(total, end)
        if end - start < max_wait * 1.5:
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    if not spans:
        return None, "nothing long enough to compress; keeping the real-time cut"

    segments: list[tuple[float, float, float]] = []   # start, end, speed
    at = 0.0
    for start, end in spans:
        if start > at:
            segments.append((at, start, 1.0))
        segments.append((start, end, (end - start) / max_wait))
        at = end
    if at < total:
        segments.append((at, total, 1.0))

    chunks, labels = [], []
    for i, (start, end, speed) in enumerate(segments):
        chunks.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},"
                      f"setpts=(PTS-STARTPTS)/{speed:.4f}[v{i}]")
        labels.append(f"[v{i}]")
    graph = ";".join(chunks) + ";" + "".join(labels) + f"concat=n={len(segments)}:v=1:a=0[v]"

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
             "-filter_complex", graph, "-map", "[v]",
             "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p", str(out)],
            check=True, capture_output=True, text=True)
    except FileNotFoundError:
        return None, "no ffmpeg on PATH; keeping the real-time cut"
    except subprocess.CalledProcessError as exc:
        return None, f"ffmpeg refused the cut: {exc.stderr.strip().splitlines()[-1:]}"

    waited = sum(end - start for start, end in spans)
    cut = _duration(out) or 0.0
    return out, (f"{total:.0f}s real time → {cut:.0f}s: {len(spans)} waits totalling "
                 f"{waited:.0f}s compressed to {max_wait:.0f}s each; every other frame "
                 "is untouched")


# ---------------------------------------------------------------------------- recording


def record(*, template: Path, title: str, out_dir: Path, port: int, brief: str,
           asks: list[str], review_ask: str, apply_ask: str | None, acts: list[str],
           headed: bool, beat: float, compressed: bool,
           ) -> tuple[Path | None, list[str], list[tuple[float, float]]]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    # Keep the server's output. Sending it to DEVNULL cost an hour of blind guessing at a
    # run that filmed nothing: the recording showed a spinner, and the one process that knew
    # why had been silenced.
    log_path = out_dir / "server.log"
    server = subprocess.Popen(
        [sys.executable, "-m", "ppt_harness.adapters.cli", "serve",
         "--from", str(template), "--title", title, "--port", str(port)],
        cwd=ROOT, stdout=log_path.open("w"), stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_server(port):
            return None, ["server did not start; is the port free and the template readable?"], []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headed)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                record_video_dir=str(out_dir),
                record_video_size={"width": 1440, "height": 900},
            )
            # The video's zero is the first frame, which is the first page in the context —
            # so the clock has to start here, before the navigation, or every wait span is
            # offset by however long the first load took.
            stage = Stage(page=None, beat=beat, t0=time.monotonic(), compressed=compressed)
            page = context.new_page()
            stage.page = page
            page.goto(f"http://127.0.0.1:{port}", wait_until="networkidle")
            page.add_style_tag(content=BANNER_CSS)

            # Wait for the *greeting*, not for a slide chip: this deck has no slides yet, so
            # the old "wait for .chip" would sit here until the timeout on the one recording
            # that most needs to start cleanly. It is a model call like any other, so it is a
            # compressible wait like any other.
            greeting_from = stage.at()
            greeted = True
            try:
                page.wait_for_selector(".msg.assistant .bubble", timeout=90_000)
            except Exception:
                greeted = False
            stage.waits.append((greeting_from, stage.at()))

            if not greeted:
                # Stop here rather than press on. The greeting is one small model call: if it
                # has not landed in ninety seconds the provider is unreachable or stalled,
                # and every act after this is a recording of a disabled button. The run that
                # taught us this filmed an empty deck for an hour and forty minutes before
                # dying on a click timeout.
                stage.problems.append(
                    "no opening turn in 90s — the model is unreachable or stalled. "
                    f"Check the key in .env and {log_path}."
                )
                stage.caption("The model did not answer. Stopping rather than recording "
                              "an empty deck — see <b>.harness/demo/server.log</b>.",
                              hold=1.6)
                video = page.video
                context.close()
                browser.close()
                return (Path(video.path()) if video else None), stage.problems, stage.waits
            page.wait_for_timeout(1200)

            if "open" in acts:
                act_open(stage, template, title)
            if "build" in acts:
                act_build(stage, brief)
            if "customise" in acts:
                act_customise(stage, asks)
            if "review" in acts:
                act_review(stage, review_ask, apply_ask)
            if "ship" in acts:
                act_ship(stage)

            stage.clear_caption()
            page.wait_for_timeout(700)

            video = page.video
            context.close()          # the file is only finalised on close
            browser.close()
            return (Path(video.path()) if video else None), stage.problems, stage.waits
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


#: Four slides, not six. Each slide is an `add_slide` call carrying a whole block spec, and
#: the model emits that as JSON — so slide count is the single biggest lever on how long the
#: build turn runs. Four is still a deck with an argument in it.
#:
#: The numbers are in the brief on purpose. Asked for a board update with no figures, a good
#: model refuses to invent them and writes `[X]%` — correct behaviour that films terribly,
#: and the resulting deck argues nothing. Give it the numbers and it can build the slide the
#: numbers deserve, which is the thing worth showing.
DEFAULT_BRIEF = (
    "Build me a four-slide Q3 board update. The numbers: EMEA churn went 4.1% in Q1 to 6.0% "
    "in Q2 to 8.3% in Q3, all of it mid-market, while the rest of the world held at 3.9, 4.0 "
    "and 4.1; expansion revenue up 41% with net revenue retention at 108%; we want sign-off "
    "on two Berlin hires at 240k. Open with a cover, put the three headline figures on a "
    "stat row, chart the churn trend against the rest of the world, and close with the ask."
)

#: One by default. `--ask` is repeatable, and a second one costs about ninety seconds of
#: real time for a beat the first already made.
DEFAULT_ASKS = [
    "Slide 3 should set the EMEA numbers against the rest of the world side by side, "
    "not as bullets.",
]

DEFAULT_REVIEW_ASK = (
    "Review the deck as an editor would — storyline, titles, style, consistency — and tell "
    "me what you would change. Do not change anything yet."
)

DEFAULT_APPLY_ASK = "Apply the two you think matter most, and tell me what you left alone."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                        help="Deck to borrow the theme from. No slides are copied.")
    parser.add_argument("--title", default="Q3 board review")
    parser.add_argument("--out", type=Path, default=ROOT / ".harness" / "demo")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--brief", default=DEFAULT_BRIEF)
    parser.add_argument("--ask", action="append", default=None,
                        help="A customisation turn. Repeatable; replaces the defaults.")
    parser.add_argument("--review-ask", default=DEFAULT_REVIEW_ASK)
    parser.add_argument("--apply-ask", default=DEFAULT_APPLY_ASK,
                        help="Turn that acts on the review. Pass '' to end on the findings.")
    parser.add_argument("--acts", default=",".join(ACTS),
                        help=f"Comma-separated subset of: {', '.join(ACTS)}")
    parser.add_argument("--beat", type=float, default=BEAT,
                        help="Seconds a caption holds. Lower is faster and harder to read.")
    parser.add_argument("--max-wait", type=float, default=MAX_WAIT,
                        help="Seconds a wait on the model may run in the cut.")
    parser.add_argument("--no-tighten", action="store_true",
                        help="Keep the real-time cut — every wait at full length.")
    parser.add_argument("--headed", action="store_true", help="Watch it happen.")
    args = parser.parse_args()

    if not args.template.is_file():
        print(f"no template at {args.template}")
        return 1
    acts = [a.strip() for a in args.acts.split(",") if a.strip()]
    unknown = [a for a in acts if a not in ACTS]
    if unknown:
        print(f"no act {unknown[0]!r}; have {', '.join(ACTS)}")
        return 1
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("needs playwright: uv sync --extra render && uv run playwright install chromium")
        return 1

    port = free_port(args.port)
    if port != args.port:
        print(f"port {args.port} is busy; using {port}")

    video, problems, waits = record(
        template=args.template, title=args.title, out_dir=args.out, port=port,
        brief=args.brief, asks=args.ask or DEFAULT_ASKS, review_ask=args.review_ask,
        apply_ask=args.apply_ask or None, acts=acts, headed=args.headed, beat=args.beat,
        compressed=not args.no_tighten,
    )
    for problem in problems:
        print(f"problem: {problem}")
    if video is None:
        return 1

    final = args.out / "ppt-harness-demo.webm"
    shutil.move(str(video), final)
    print(f"{final}  ·  {final.stat().st_size / 1_000_000:.1f} MB  (real time, every frame)")

    if args.no_tighten:
        print("convert with:  ffmpeg -y -i "
              f"{final} -vf scale=1440:-2 -c:v libx264 -crf 22 {final.with_suffix('.mp4')}")
        return 1 if problems else 0

    cut, note = tighten(final, waits, final.with_suffix(".mp4"), max_wait=args.max_wait)
    print(f"cut: {note}")
    if cut is not None:
        print(f"{cut}  ·  {cut.stat().st_size / 1_000_000:.1f} MB  ← the one to publish")
    # A recording that captured a failed turn is still a recording, and still worth keeping —
    # but it is not a pass, and saying so is what stops one being published by mistake.
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

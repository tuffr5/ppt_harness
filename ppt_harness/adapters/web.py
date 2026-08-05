"""Web client — chat on the left, live preview on the right.

The preview is the same HTML the measurement pass lays out, served into an iframe. That is
the point: the user is looking at the artifact the harness measures, not a picture of it,
so "what you see" and "what was checked" cannot drift apart.

No build step and no bundler. One HTML string, plain JS, server-sent events for the turn.
A page you can read in one sitting is worth more here than a framework.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..core import loop as loop_mod
from ..core.loop import Agent
from ..core.session import Session
from ..fidelity import reference
from ..render import browser, html, preview
from ..state import templates
from ..tools import router


class Ask(BaseModel):
    prompt: str


class Start(BaseModel):
    template: str
    title: str | None = None


class Edit(BaseModel):
    target: str
    text: str


def create_app(session: Session, *, model: str | None = None,
               base_url: str | None = None,
               recovery: dict[str, Any] | None = None) -> FastAPI:
    app = FastAPI(title="ppt-harness")
    # One deck, one conversation, one lock — the same session the tools write through, so
    # the preview cannot show a deck the model has not actually changed.
    agent_kw: dict[str, Any] = {}
    if model:
        agent_kw["model"] = model
    if base_url:
        agent_kw["base_url"] = base_url
    # Whether the person has said which deck they are working on — *not* whether the deck has
    # anything in it yet. The picker asks a question, and it has to stop asking once it has
    # been answered: a deck started from a card is empty by definition, so a dismissal keyed
    # on slide count put the picker straight back on screen and the click read as broken.
    #
    # Everything except a bare `serve` arrives already answered: a named deck brought its own
    # slides, `--from` and `--template` named a theme, and each of those is a choice made at
    # the command line that the browser has no business asking about again.
    state: dict[str, Any] = {
        "agent": Agent(session, **agent_kw),
        "chosen": bool(session.deck.slides or session.deck.source_path
                       or session.deck.theme_from),
    }
    # The preview is the export, rendered. Warmed in the background so opening a deck is
    # not a stall; a person waits ~1s once, and never for measurement.
    cache = preview.PreviewCache(session)
    threading.Thread(target=cache.warm, daemon=True).start()

    # -- reads ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/outline")
    def outline() -> dict[str, Any]:
        data = session.outline()
        data["browser"] = browser.available()
        data["renderer"] = reference.available()
        agent = state["agent"]
        data["provider"] = agent.provider.name
        data["model"] = agent.model
        data["version"] = cache.version()
        data["theme_problems"] = session.theme_problems
        # Whether this deck was picked up mid-flight, and whether anything was lost doing
        # it. A resume that quietly pretends to be a fresh open is how someone exports a
        # deck missing the last thing they asked for.
        data["recovery"] = recovery or {"resumed": False, "turns": 0}
        data["persistent"] = session.workspace is not None
        return data

    @app.get("/api/slide/{slide_id}", response_class=HTMLResponse)
    def slide_html(slide_id: str) -> str:
        """The preview pane.

        A real render of the exported file, with the harness's measurements drawn over it.
        The picture is faithful because nothing reimplemented it; the boxes are ours,
        in the same canvas coordinates the exporter writes.

        Falls back to the HTML renderer when no Office renderer is installed — degraded,
        and honest about it, rather than a blank pane.
        """
        slide = _slide_or_404(slide_id)
        if cache.available:
            measurement = session.measure_slide(slide_id)
            return html.render_overlay(
                session.theme, slide,
                f"/api/slide/{slide_id}/preview.png?v={cache.version()}",
                measurement,
            )
        return session.render_html(slide_id, inline_assets=False)

    @app.get("/api/slide/{slide_id}/preview.png")
    def slide_preview(slide_id: str, width: int = 1600, v: str = "") -> Response:
        _slide_or_404(slide_id)
        try:
            page = cache.page(slide_id, width=width)
        except preview.PreviewUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(500, str(exc)) from exc
        # Immutable because the URL carries the deck version; a new edit is a new URL.
        return Response(page.png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/asset/{key}")
    def asset(key: str) -> Response:
        found = session.assets.get(key)
        if found is None:
            raise HTTPException(404, f"no asset {key!r}")
        content_type, blob = found
        return Response(blob, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/slide/{slide_id}/measure")
    def slide_measure(slide_id: str, freeze: bool = False) -> dict[str, Any]:
        _slide_or_404(slide_id)
        return session.measure_slide(slide_id, freeze=freeze)

    @app.get("/api/slide/{slide_id}/png")
    def slide_png(slide_id: str, width: int = 0) -> Response:
        slide = _slide_or_404(slide_id)
        cx, cy = session.slide_size_emu()
        try:
            png = browser.screenshot_slide(session.theme, slide, cx, cy, width or None,
                                           asset_src=session.asset_data_uri)
        except browser.BrowserUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return Response(png, media_type="image/png")

    @app.get("/api/search")
    def search(q: str = "") -> dict[str, Any]:
        return {"query": q, "results": session.search(q)}

    @app.get("/api/theme")
    def theme() -> dict[str, Any]:
        return router.dispatch(session, "get_theme")

    @app.get("/api/lint")
    def lint() -> dict[str, Any]:
        return router.dispatch(session, "lint")

    @app.get("/api/history")
    def history() -> dict[str, Any]:
        """The conversation so far, flattened for replay.

        Read from the provider's own message list rather than a second copy: two records of
        the same conversation drift, and the one the model sees is the one that is true.

        With one subtraction. The greeting is asked for as an ordinary user message, because
        that is the only way to ask a model for anything — but *nobody typed it*, and
        replaying it put "Open the conversation in two or three sentences…" in the log
        attributed to the person reading it, above a reply to an instruction they never gave.
        The transcript is what a human is owed; the message list is what the model is owed,
        and here they legitimately differ.
        """
        turns: list[dict[str, Any]] = []
        for message in state["agent"].messages:
            role = message.get("role") if isinstance(message, dict) else None
            if role not in ("user", "assistant"):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else _text_of(content)
            if role == "user" and text.strip() == loop_mod.OPENING.strip():
                continue
            calls = message.get("tool_calls") or []
            if not text and not calls:
                continue
            turns.append({
                "role": role,
                "text": text,
                "tools": [c["function"]["name"] for c in calls] if calls else [],
            })
        return {"turns": turns}

    @app.get("/api/log")
    def log() -> dict[str, Any]:
        """The op log, with authorship. This is what makes a correction visible as a
        correction rather than as just another edit (DESIGN §8.2)."""
        return {
            "ops": [{"seq": o.seq, "op": o.op, "target": o.target, "author": o.author.value,
                     "turn": o.turn} for o in session.store.log.ops],
            "corrections": [{"target": m.target, "model": m.patch, "user": u.patch}
                            for m, u in session.store.log.corrections()],
        }

    # -- writes -----------------------------------------------------------------

    @app.post("/api/edit")
    def edit(body: Edit) -> JSONResponse:
        """Direct edit from the inspector, attributed to the *user*.

        Authorship is not cosmetic: a user op landing on a target the model just touched is
        the observed channel of the preference profile.
        """
        from ..state.document import Author

        result = router.dispatch(session, "set_text",
                                 {"target": body.target, "text": body.text,
                                  "author": Author.USER})
        return JSONResponse(result, status_code=200 if result.get("ok") else 422)

    @app.post("/api/undo")
    def undo() -> dict[str, Any]:
        return router.dispatch(session, "undo")

    @app.post("/api/redo")
    def redo() -> dict[str, Any]:
        return router.dispatch(session, "redo")

    @app.post("/api/export")
    def export() -> dict[str, Any]:
        name = (session.deck.title or "deck").replace("/", "-")
        out_dir = preview.cache_root() / "export"
        out_dir.mkdir(parents=True, exist_ok=True)
        return router.dispatch(session, "export", {"path": str(out_dir / f"{name}.pptx")})

    @app.post("/api/chat")
    def chat(body: Ask) -> StreamingResponse:
        return StreamingResponse(_stream(state["agent"], body.prompt),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/greeting")
    def greeting() -> StreamingResponse:
        """The model's opening turn, streamed like any other.

        Same shape as `/api/chat` so the client renders it with the same code path. The
        agent caches it, so this costs one call per server process however many times the
        page is reloaded.
        """
        return StreamingResponse(_stream_greeting(state["agent"]),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- starting over ----------------------------------------------------------

    @app.get("/api/templates")
    def template_list() -> dict[str, Any]:
        """The themes a deck can start on, for the picker.

        `--from company.pptx` remains the honest way to start and the CLI still says so; this
        is the same list `ppt-harness templates` prints, for somebody who has nothing to
        borrow and is already in the browser.
        """
        return {
            "templates": [
                {"name": t.name, "description": t.description, "summary": t.summary(),
                 "brand": t.theme.palette.get("brand", ""),
                 "display": t.theme.type.families.get("display", "").split(",")[0]}
                for t in templates.catalog()
            ],
            "current": session.deck.theme_from,
            "started": state["chosen"],
        }

    @app.get("/api/templates/{name}/thumb.png")
    def template_thumb(name: str) -> Response:
        """One real slide on that theme, rendered.

        A swatch of the brand colour would be cheaper and would be a lie: it shows the hue and
        none of the things a person is actually choosing between — the display face, the type
        scale, how a card sits on the background. The preview is the export, so the honest
        thumbnail is free: build a slide, put it through the ordinary exporter, rasterise the
        page. What the card shows is what the deck will be.

        Cached on disk under the preview root. First call costs a render, the rest are a file
        read, and a theme only changes when the harness ships a new one.
        """
        cached = preview.cache_root() / "thumbs" / f"{name}.png"
        if not cached.exists():
            try:
                sample = Session.from_builtin(name, name)
            except templates.TemplateError as exc:
                raise HTTPException(404, str(exc)) from exc
            router.dispatch(sample, "add_slide", {
                "layout": "stack",
                "blocks": [
                    {"region": "header", "component": "slide_title",
                     "slots": {"title": f"{name.title()} — a slide on this theme"}},
                    {"region": "body", "component": "stat_row", "variant": "carded",
                     "slots": {"items": [{"value": "38%", "label": "Latency"},
                                         {"value": "2.4x", "label": "Growth"},
                                         {"value": "$4.2M", "label": "ARR"}]}},
                ],
            })
            try:
                page = preview.PreviewCache(sample).page(sample.deck.slides[0].id, width=640)
            except preview.PreviewUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(page.png)
        return Response(cached.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/start")
    def start(body: Start) -> dict[str, Any]:
        """Begin a new deck on a chosen theme, discarding the current one.

        Rebinding rather than restarting the process: every route closes over `session` and
        `cache` as *variables*, so `nonlocal` reaches all of them at once and there is no
        registry of consumers to keep in step. The agent goes with them — a conversation
        about the old deck would describe slides that no longer exist.

        Deliberately not undoable. Undo is scoped to a turn within a deck, and a store whose
        log spanned two decks could roll back into one the user had abandoned.
        """
        nonlocal session, cache
        if session.workspace is not None:
            raise HTTPException(409, "this deck is persistent; start a new one with "
                                     "`ppt-harness serve --template <name>`")
        try:
            session = Session.from_builtin(body.template, body.title or "Untitled")
        except templates.TemplateError as exc:
            raise HTTPException(404, str(exc)) from exc
        # A title slide, the way `new` has always opened a deck. Without one the picker
        # answered a question with an empty stage: no preview, no slide list, nothing to
        # look at, and the person who just chose a theme cannot see what they chose. The
        # theme is the whole content of the choice, so the first slide has to show it.
        router.dispatch(session, "add_slide", {
            "layout": "title",
            "blocks": [{"region": "hero", "component": "title_slide", "variant": "centered",
                        "slots": {"title": session.deck.title}}],
        })
        cache = preview.PreviewCache(session)
        state["agent"] = Agent(session, **agent_kw)
        state["chosen"] = True
        threading.Thread(target=cache.warm, daemon=True).start()
        return session.outline()

    def _slide_or_404(slide_id: str):
        slide = session.deck.slide(slide_id)
        if slide is None:
            raise HTTPException(404, f"no slide {slide_id!r}")
        return slide

    return app


def _text_of(content: Any) -> str:
    """Text from a content-block list, ignoring tool traffic.

    Anthropic returns a list of blocks; a tool_result carries JSON nobody wants replayed
    into a chat window.
    """
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        kind = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None)
        if kind != "text":
            continue
        parts.append(getattr(block, "text", None) or block.get("text", ""))
    return "\n".join(p for p in parts if p)


def _stream_greeting(agent: Agent) -> Iterator[str]:
    """The opening turn, or nothing at all.

    A greeting is the one turn nobody asked for, so it is also the one that must never
    become an error message in an empty chat window. If the endpoint is down or the key is
    wrong, the log simply stays empty and the person types their first request — which is
    exactly what they would have done anyway. The real failure surfaces on that turn, where
    it is actionable.
    """
    try:
        for event in agent.greet():
            if event.kind == "error":
                return
            yield f"data: {json.dumps(event.as_dict(), ensure_ascii=False)}\n\n"
    except Exception:
        yield 'data: {"kind": "done"}\n\n'


def _stream(agent: Agent, prompt: str) -> Iterator[str]:
    """Server-sent events for one turn.

    Tool calls are streamed as they happen rather than batched at the end, so a slow turn
    shows its work instead of looking hung.
    """
    try:
        for event in agent.run(prompt):
            yield f"data: {json.dumps(event.as_dict(), ensure_ascii=False)}\n\n"
    except Exception as exc:  # a crashed turn must still close the stream
        payload = {"kind": "error", "text": f"{type(exc).__name__}: {exc}"}
        yield f"data: {json.dumps(payload)}\n\n"
        yield 'data: {"kind": "done"}\n\n'


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ppt-harness</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#252a34; --ink:#e6e9ef; --muted:#8b93a3;
    --accent:#4f8ef7; --ok:#3fb950; --bad:#f85149; --warn:#d29922;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; height:100vh; display:grid; grid-template-columns:minmax(340px,420px) 1fr;
    background:var(--bg); color:var(--ink);
    font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
    transition:grid-template-columns .18s ease;
  }
  /* The picker covers the whole app rather than sitting in the stage: until a theme is
     chosen every control behind it acts on a deck the user has not agreed to yet. */
  #start {
    position:fixed; inset:0; z-index:20; display:grid; place-items:center;
    background:rgba(15,17,21,.94); padding:24px; overflow:auto;
  }
  #start .sheet { width:min(760px,100%); }
  #start h2 { margin:0 0 6px; font-size:20px; letter-spacing:.2px; }
  #start .lead { margin:0 0 20px; color:var(--muted); max-width:62ch; }
  /* `serve --from company.pptx` is one token to a reader; letting it wrap mid-flag turns it
     into two commands that do not exist. */
  #start code { background:var(--panel); padding:1px 5px; border-radius:4px; white-space:nowrap; }
  /* Two columns, not auto-fit: four themes into a three-up grid strands one on a row of its
     own, and the picker is the first thing anybody sees. */
  #cards { display:grid; gap:16px; grid-template-columns:repeat(2,1fr); }
  @media (max-width:640px) { #cards { grid-template-columns:1fr; } }
  .card {
    display:flex; flex-direction:column; text-align:left; padding:0 0 14px;
    border:1px solid var(--line); border-radius:10px; background:var(--panel);
    color:var(--ink); cursor:pointer; overflow:hidden; font:inherit;
    transition:border-color .12s ease, transform .12s ease;
  }
  .card:hover, .card:focus-visible { border-color:var(--accent); transform:translateY(-2px); }
  .card[aria-busy="true"] { opacity:.55; pointer-events:none; }
  /* A real slide on that theme, rendered through the ordinary exporter — see
     `/api/templates/{name}/thumb.png`. The 16:9 box is reserved before the image arrives so
     four cards do not jump about as they load. */
  .card .shot {
    display:block; width:100%; aspect-ratio:16/9; object-fit:cover;
    background:var(--bg); border-bottom:1px solid var(--line);
  }
  .card .name { display:block; padding:12px 14px 0; font-weight:650; }
  .card .desc {
    display:block; padding:4px 14px 0; color:var(--muted); font-size:12.5px;
    /* Descriptions differ by a line or two; clamping keeps every card the same height so the
       grid reads as a set rather than as four ragged boxes. */
    display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
  }
  /* Collapsed to a hairline rather than hidden: the preview gets the room, and the drawer
     stays where the user last saw it instead of reflowing back in. */
  body.collapsed { grid-template-columns:0 1fr; }
  body.collapsed aside { border-right:0; }
  aside {
    display:flex; flex-direction:column; border-right:1px solid var(--line);
    min-width:0; overflow:hidden;
  }
  header {
    padding:12px 16px; border-bottom:1px solid var(--line);
    display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  }
  header h1 { font-size:14px; margin:0; font-weight:650; letter-spacing:.2px; }
  header .meta { color:var(--muted); font-size:12px; }
  /* One scrolling column for the whole conversation: earlier turns stay written out in
     full, and the only thing between you and them is the scrollbar on the right. */
  #log {
    flex:1; overflow-y:auto; padding:14px 16px; display:flex; flex-direction:column; gap:12px;
    scrollbar-gutter:stable; scrollbar-width:thin; scrollbar-color:#39414f transparent;
  }
  /* Styled rather than default: macOS hides overlay scrollbars until you move, which reads
     as "there is nothing above" on a transcript that in fact scrolls. */
  #log::-webkit-scrollbar { width:10px; }
  #log::-webkit-scrollbar-track { background:transparent; }
  #log::-webkit-scrollbar-thumb {
    background:#39414f; border-radius:999px; border:3px solid transparent; background-clip:content-box;
  }
  #log::-webkit-scrollbar-thumb:hover { background:#4c5568; }
  .used { font-size:11.5px; color:var(--muted); }
  .used code { color:var(--accent); }
  .msg { display:flex; flex-direction:column; gap:4px; }
  .msg .who { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); }
  .msg.user .bubble { background:#1d2530; }
  .bubble { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:9px 11px; white-space:pre-wrap; }
  .tool { border:1px solid var(--line); border-radius:8px; overflow:hidden; font-size:12px; }
  .tool summary { padding:7px 10px; cursor:pointer; display:flex; gap:8px; align-items:center; background:var(--panel); }
  .tool summary code { color:var(--accent); font-weight:600; }
  .tool .dot { width:7px; height:7px; border-radius:50%; background:var(--muted); flex:none; }
  .tool.ok .dot { background:var(--ok); } .tool.bad .dot { background:var(--bad); }
  .tool.pending .dot { background:var(--accent); animation:pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.25; } }
  @keyframes sweep { to { background-position:200% 0; } }
  #working {
    display:none; align-items:center; gap:10px;
    padding:8px 16px; border-top:1px solid var(--line);
    font-size:12px; color:var(--muted);
  }
  body.busy #working { display:flex; }
  #working .bar-anim {
    flex:1; height:2px; border-radius:2px;
    background:linear-gradient(90deg,var(--line) 0%,var(--accent) 50%,var(--line) 100%);
    background-size:200% 100%; animation:sweep 1.1s linear infinite;
  }
  #working .what { white-space:nowrap; color:var(--ink); }
  #elapsed { font-variant-numeric:tabular-nums; }
  /* The send button doubles as the progress affordance, so it must not look merely
     broken while a turn runs. */
  body.busy #send { opacity:.55; }
  .tool pre { margin:0; padding:9px 11px; background:#101319; overflow-x:auto;
              color:var(--muted); font-size:11.5px; max-height:280px; }
  form { display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--line); }
  textarea {
    flex:1; resize:none; min-height:44px; max-height:180px; padding:10px;
    background:var(--panel); border:1px solid var(--line); border-radius:8px;
    color:inherit; font:inherit;
  }
  button {
    background:var(--accent); color:#fff; border:0; border-radius:8px; padding:0 16px;
    font-weight:600; cursor:pointer;
  }
  button:disabled { opacity:.45; cursor:default; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); padding:4px 10px; font-size:12px; font-weight:500; }
  main { display:flex; flex-direction:column; min-width:0; }
  .bar { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  #slides { display:flex; gap:6px; overflow-x:auto; flex:1; }
  .chip {
    padding:4px 10px; border:1px solid var(--line); border-radius:999px; cursor:pointer;
    font-size:12px; color:var(--muted); white-space:nowrap; background:var(--panel);
  }
  .chip.on { border-color:var(--accent); color:var(--ink); }
  .chip.hit { border-color:var(--ok); color:var(--ink); }
  .chip.miss { opacity:.28; }
  #find {
    flex:none; width:170px; padding:4px 10px; font:inherit; font-size:12px;
    background:var(--panel); border:1px solid var(--line); border-radius:999px;
    color:inherit;
  }
  #find:focus { outline:none; border-color:var(--accent); }
  .chip.warn { border-color:var(--warn); }
  #stage { flex:1; display:grid; place-items:center; padding:20px; overflow:auto; background:#0b0d11; }
  #frame { width:1280px; height:720px; border:0; background:#fff; box-shadow:0 10px 40px rgba(0,0,0,.5); transform-origin:center; }
  #status { padding:8px 16px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; min-height:33px; }
  .pill { padding:1px 7px; border-radius:999px; font-size:11px; border:1px solid var(--line); }
  .pill.ok { color:var(--ok); border-color:#1d3a24; } .pill.bad { color:var(--bad); border-color:#4a1f1d; }
</style></head>
<body>
<aside>
  <header>
    <h1 id="deck">loading…</h1>
    <span class="meta" id="meta"></span>
  </header>
  <div id="log"></div>
  <div id="working">
    <span class="what" id="doing">thinking…</span>
    <span class="bar-anim"></span>
    <span id="elapsed">0.0s</span>
  </div>
  <form id="ask">
    <textarea id="prompt" placeholder="Ask for a change — e.g. &quot;retitle slide 1&quot; or &quot;add a slide summarising the findings&quot;" rows="2"></textarea>
    <button id="send">Send</button>
  </form>
</aside>
<main>
  <div class="bar">
    <button class="ghost" id="drawer" title="Show or hide the chat">☰</button>
    <input id="find" type="search" placeholder="Find in deck" autocomplete="off">
    <div id="slides"></div>
    <button class="ghost" id="probes">Boxes</button>
    <button class="ghost" id="undo">Undo</button>
    <button class="ghost" id="redo">Redo</button>
    <button class="ghost" id="export">Export</button>
  </div>
  <div id="stage"><iframe id="frame" title="slide preview"></iframe></div>
  <div id="status"></div>
</main>
<div id="start" hidden>
  <div class="sheet">
    <h2>Start a deck</h2>
    <p class="lead">Pick a theme to work on. Nothing is written until you ask for a slide —
      and <code>serve --from company.pptx</code> borrows your own instead of any of these.</p>
    <div id="cards"></div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
const log = $('#log'), stage = $('#stage'), frame = $('#frame');
let current = null, outline = null, announcedRecovery = false;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function say(who, text) {
  const m = el('div', 'msg ' + who);
  m.append(el('div', 'who', who), el('div', 'bubble', text));
  log.append(m); log.scrollTop = log.scrollHeight;
  return m.querySelector('.bubble');
}
function toolCard(name, args) {
  const d = el('details', 'tool pending');
  const s = el('summary');
  s.append(el('span', 'dot'), Object.assign(el('code'), {textContent: name}));
  d.append(s, el('pre', null, JSON.stringify(args, null, 1)));
  log.append(d); log.scrollTop = log.scrollHeight;
  return d;
}

// Scale the fixed 1280x720 stage to whatever room the pane has. The canvas keeps its own
// pixel coordinates so preview, measurement, and export all speak the same numbers.
function fit() {
  const pad = 40;
  const k = Math.min((stage.clientWidth - pad) / 1280, (stage.clientHeight - pad) / 720, 1);
  frame.style.transform = `scale(${k})`;
  frame.style.margin = `${(720 * k - 720) / 2}px ${(1280 * k - 1280) / 2}px`;
}
addEventListener('resize', fit);

async function loadOutline(keep) {
  outline = await (await fetch('/api/outline')).json();
  $('#deck').textContent = outline.deck;
  // The template is named in the header for the whole session, not announced once: a deck
  // that borrowed its look from somewhere should say so while you are looking at it.
  $('#meta').textContent = `${outline.slides.length} slides · ${outline.theme}` +
    (outline.template ? ` · theme from ${outline.template}` : '') +
    ` · ${outline.model}` +
    (outline.renderer ? ` · preview: ${outline.renderer}` : ' · preview: html (no office renderer)');
  // Said once, at the top of the log: work picked up from a previous process is not the
  // same as a file freshly opened, and a degraded resume is something to know *before*
  // exporting, not after.
  if (outline.recovery && outline.recovery.resumed && !announcedRecovery) {
    announcedRecovery = true;
    const n = outline.recovery.turns;
    say(outline.recovery.degraded ? 'error' : 'assistant',
        `Resumed ${n} earlier ${n === 1 ? 'edit' : 'edits'} from the workspace.` +
        (outline.recovery.note ? ` ${outline.recovery.note}` : ''));
  }
  const bar = $('#slides'); bar.innerHTML = '';
  outline.slides.forEach(s => {
    const c = el('div', 'chip', `${s.index + 1}`);
    c.title = (s.gist || s.id) + `  [${s.mode}]`;
    c.onclick = () => show(s.id);
    c.dataset.id = s.id;
    bar.append(c);
  });
  await show(keep && outline.slides.some(s => s.id === keep) ? keep
             : (outline.slides[0] && outline.slides[0].id));
}

async function show(id) {
  if (!id) return;
  current = id;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c.dataset.id === id));
  frame.srcdoc = await (await fetch(`/api/slide/${id}`)).text();
  frame.onload = () => {
    const doc = frame.contentDocument;
    if (doc) doc.body.classList.toggle('show-probes', showProbes);
  };
  fit();
  const m = await (await fetch(`/api/slide/${id}/measure`)).json();
  const bad = !m.clean;
  const over = m.overflow_px ?? 0;
  $('#status').innerHTML =
    `<span class="pill ${bad ? 'bad' : 'ok'}">${bad ? 'overflow ' + over + 'px' : 'fits'}</span> ` +
    `&nbsp;${id} · ${m.mode || ''} · ${(m.boxes || m.slots || m.shapes || []).length} measured` +
    (m.frozen ? ' · frozen geometry' : '');
  const chip = document.querySelector(`.chip[data-id="${id}"]`);
  if (chip) chip.classList.toggle('warn', bad);
}

$('#ask').onsubmit = async e => {
  e.preventDefault();
  const box = $('#prompt'), text = box.value.trim();
  if (!text) return;
  box.value = ''; $('#send').disabled = true;
  say('user', text);
  let bubble = null, touched = null, round = 0;
  const cards = new Map();
  startWorking();

  const res = await fetch('/api/chat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: text}),
  });
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    const parts = buf.split('\n\n'); buf = parts.pop();
    for (const part of parts) {
      if (!part.startsWith('data: ')) continue;
      const ev = JSON.parse(part.slice(6));
      if (ev.kind === 'start') { setDoing(`asking ${ev.text}`); }
      else if (ev.kind === 'round') { round = ev.round; setDoing(round > 1 ? `round ${round}` : 'thinking…'); }
      else if (ev.kind === 'text') { bubble = say('assistant', ev.text); setDoing('finishing…'); }
      else if (ev.kind === 'thinking') {
        const d = el('details', 'tool');
        d.append(el('summary', null, 'reasoning'), el('pre', null, ev.text));
        log.append(d); log.scrollTop = log.scrollHeight;
      }
      else if (ev.kind === 'tool_call') {
        // Keyed by call id: a round may hold several calls, and pairing by "the last card
        // I made" mislabels every one of them but the first.
        cards.set(ev.call_id, toolCard(ev.tool, ev.args || {}));
        setDoing(`${ev.tool}…`);
      }
      else if (ev.kind === 'tool_result') {
        const card = cards.get(ev.call_id);
        if (card) {
          card.classList.remove('pending');
          card.classList.add(ev.result.ok ? 'ok' : 'bad');
          card.querySelector('pre').textContent = JSON.stringify(ev.result, null, 1);
          if (!ev.result.ok) card.open = true;
        }
        if (ev.slide) touched = ev.slide;
      }
      else if (ev.kind === 'error') { say('error', ev.text); }
    }
  }
  stopWorking();
  $('#send').disabled = false;
  await loadOutline(touched || current);
};

let workTimer = null, workStart = 0;
function setDoing(what) { $('#doing').textContent = what; }
function startWorking() {
  workStart = performance.now();
  document.body.classList.add('busy');
  setDoing('thinking…');
  // A running clock, not a spinner alone: "12.4s" tells you whether to keep waiting;
  // a spinning shape looks the same at one second and at five minutes.
  workTimer = setInterval(() => {
    $('#elapsed').textContent = ((performance.now() - workStart) / 1000).toFixed(1) + 's';
  }, 100);
}
function stopWorking() {
  clearInterval(workTimer);
  document.body.classList.remove('busy');
}

$('#drawer').onclick = () => {
  document.body.classList.toggle('collapsed');
  localStorage.setItem('drawer', document.body.classList.contains('collapsed') ? '1' : '');
  setTimeout(fit, 200);
};
if (localStorage.getItem('drawer')) document.body.classList.add('collapsed');

// Replay the conversation into the same column the live turns land in, in full: a reload
// should look like the page you left, not like a list of the questions you once asked.
// Runs once, at load — after that the stream is the record.
async function restoreTranscript() {
  const {turns} = await (await fetch('/api/history')).json();
  for (const t of turns) {
    if (t.text) say(t.role, t.text);
    if (t.tools && t.tools.length) {
      const line = el('div', 'used');
      line.append(document.createTextNode('used '));
      t.tools.forEach((name, i) => {
        if (i) line.append(document.createTextNode(', '));
        line.append(Object.assign(el('code'), {textContent: name}));
      });
      log.append(line);
    }
  }
  log.scrollTop = log.scrollHeight;
  return turns.length > 0;
}

// The model's opening turn, for a conversation that has not started yet. Streamed through
// the same renderer as any other reply. Silent on failure by design — an empty log is a
// fine place to start typing, and an error bubble nobody asked for is not.
async function greet() {
  setDoing('saying hello…'); startWorking();
  try {
    const res = await fetch('/api/greeting', {method: 'POST'});
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        const ev = JSON.parse(part.slice(6));
        if (ev.kind === 'text') say('assistant', ev.text);
      }
    }
  } catch (e) { /* an opening nobody asked for must not become an error they must read */ }
  stopWorking();
}

let findTimer = null;
$('#find').oninput = () => {
  clearTimeout(findTimer);
  findTimer = setTimeout(runFind, 140);
};
async function runFind() {
  const q = $('#find').value.trim();
  const chips = [...document.querySelectorAll('.chip')];
  if (!q) {
    chips.forEach(c => c.classList.remove('hit', 'miss'));
    $('#status').dataset.find = '';
    return;
  }
  const body = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
  const byId = new Map(body.results.map(r => [r.slide, r]));
  chips.forEach(c => {
    const hit = byId.get(c.dataset.id);
    c.classList.toggle('hit', !!hit);
    c.classList.toggle('miss', !hit);
    if (hit) c.title = hit.hits.map(h => h.snippet).join('\n');
  });
  const total = body.results.reduce((n, r) => n + r.hits.length, 0);
  $('#status').innerHTML =
    `<span class="pill ${total ? 'ok' : 'bad'}">${total} match${total === 1 ? '' : 'es'}</span>` +
    `&nbsp;on ${body.results.length} slide${body.results.length === 1 ? '' : 's'}` +
    (body.results.length ? ` · ${body.results.map(r => r.slide).join(', ')}` : '');
  if (body.results.length) show(body.results[0].slide);
}

$('#prompt').onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#ask').requestSubmit(); }
};
let showProbes = false;
$('#probes').onclick = () => {
  showProbes = !showProbes;
  $('#probes').style.color = showProbes ? 'var(--ink)' : '';
  const doc = frame.contentDocument;
  if (doc) doc.body.classList.toggle('show-probes', showProbes);
};
$('#undo').onclick = async () => { await fetch('/api/undo', {method:'POST'}); loadOutline(current); };
$('#redo').onclick = async () => { await fetch('/api/redo', {method:'POST'}); loadOutline(current); };
$('#export').onclick = async () => {
  const r = await (await fetch('/api/export', {method:'POST'})).json();
  say('system', r.summary || JSON.stringify(r));
};

// The picker, offered only for a deck with nothing in it. A deck with slides — imported,
// resumed, or already written into — is one the user has committed to, and replacing it
// from a card grid is not a choice anybody came here to make.
async function offerStart() {
  const data = await (await fetch('/api/templates')).json();
  if (data.started) return false;
  const cards = $('#cards');
  cards.textContent = '';
  for (const t of data.templates) {
    const card = el('button', 'card');
    card.type = 'button';
    const shot = el('img', 'shot');
    shot.src = `/api/templates/${encodeURIComponent(t.name)}/thumb.png`;
    shot.alt = `A slide on the ${t.name} theme`;
    shot.loading = 'lazy';
    // A renderer is optional — no LibreOffice, no thumbnail. The card still names and
    // describes the theme, so the picker degrades to what it was rather than to a broken
    // image icon.
    shot.onerror = () => shot.remove();
    card.append(shot, el('span', 'name', t.name), el('span', 'desc', t.description));
    card.onclick = async () => {
      card.setAttribute('aria-busy', 'true');
      const res = await fetch('/api/start',
        {method: 'POST', headers: {'Content-Type': 'application/json'},
         body: JSON.stringify({template: t.name})});
      if (!res.ok) { card.removeAttribute('aria-busy'); return; }
      // Reload rather than patch: a new session means a new transcript, a new preview cache
      // and a new agent, and every one of those is already correct on a fresh page.
      location.reload();
    };
    cards.append(card);
  }
  $('#start').hidden = false;
  return true;
}

// Outline first so the deck is on screen while the model composes; the greeting only for a
// conversation with nothing in it, since a reload replays the transcript and a hello pasted
// under yesterday's turns would claim to be the newest thing said.
(async () => {
  await loadOutline();
  if (await restoreTranscript()) return;
  // The picker outranks the greeting: a hello about a deck the user is still choosing would
  // be answering a question they have not asked yet.
  if (!await offerStart()) greet();
})();
</script>
</body></html>
"""


def port_is_free(host: str, port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def serve(deck: str | None = None, host: str = "127.0.0.1", port: int = 8000,
          model: str | None = None, base_url: str | None = None,
          fresh: bool = False, template: str | None = None,
          title: str = "Untitled", builtin: str | None = None) -> None:
    import uvicorn

    # 8000 is also where a local vLLM or vllm-mlx typically listens. Binding failures from
    # uvicorn are a stack trace; this is a sentence.
    if not port_is_free(host, port):
        raise RuntimeError(
            f"port {port} on {host} is already in use — a local model server often lives "
            f"there. Pass --port to pick another."
        )

    if sum(bool(x) for x in (deck, template, builtin)) > 1:
        raise RuntimeError(
            "pass a deck to open one, --from to start on another deck's theme, or "
            "--template to start on a built-in one — not more than one. Each of them "
            "answers the same question, and they would answer it differently."
        )

    if deck:
        # Resume rather than open: the web client is the one entry point with a person
        # waiting on it, so it is the one that must survive a crash with the work intact.
        session, recovery = Session.resume(deck, fresh=fresh)
    elif template or builtin:
        # No resume: a template start is a *new* deck every time, and picking up the last
        # one under the same name is how somebody's finished work reappears inside their
        # next one.
        session = (Session.from_template(template, title) if template
                   else Session.from_builtin(builtin, title))  # type: ignore[arg-type]
        recovery = {"resumed": False, "turns": 0}
    else:
        session, recovery = Session.blank(title), {"resumed": False, "turns": 0}

    if recovery.get("resumed"):
        note = recovery.get("note")
        print(f"resumed {recovery['turns']} turns of earlier work"
              + (f" — {note}" if note else ""))

    uvicorn.run(create_app(session, model=model, base_url=base_url, recovery=recovery),
                host=host, port=port, log_level="warning")

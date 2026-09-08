"""Minimal operator surface (deliberately bare; see REPORT.md §5).

Shows an intervention's context and live screenshot, lets the operator act on
the same live session through the same Surface, and signal resume/abort.
"""

from __future__ import annotations

import html
import threading
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from agent.prompts import elements_to_text

if TYPE_CHECKING:
    from .manager import HandoffManager

from .manager import OperatorCommand


def build_app(mgr: HandoffManager) -> FastAPI:
    app = FastAPI(title="Operator handoff console")

    @app.get("/interventions")
    def list_interventions():
        return [r.public() for r in mgr.requests.values()]

    @app.get("/interventions/{iid}/state")
    def state(iid: str):
        r = mgr.requests.get(iid)
        if not r:
            raise HTTPException(404)
        return {
            **r.public(),
            "controller": mgr.controller.value,
            "elements": elements_to_text(r.perception.elements).splitlines() if r.perception else [],
        }

    @app.get("/interventions/{iid}/screenshot.png")
    def screenshot(iid: str):
        r = mgr.requests.get(iid)
        if not r or not r.perception:
            raise HTTPException(404)
        return Response(r.perception.screenshot, media_type="image/png")

    @app.get("/interventions/{iid}", response_class=HTMLResponse)
    def page(iid: str):
        r = mgr.requests.get(iid)
        if not r:
            raise HTTPException(404)
        els = html.escape(elements_to_text(r.perception.elements)) if r.perception else ""
        acts = "".join(f"<li>{html.escape(str(a))}</li>" for a in r.human_actions)
        open_ = r.state == "open"
        return f"""<html><head><title>Intervention {r.id}</title>
<style>body{{font-family:system-ui;margin:20px;max-width:1200px}} pre{{background:#f4f4f4;padding:8px;max-height:300px;overflow:auto}}
img{{max-width:100%;border:1px solid #999}} .box{{border:1px solid #ccc;padding:12px;margin:8px 0}}</style></head><body>
<h2>Intervention {r.id} — <span style="color:{"#c00" if open_ else "#080"}">{r.state.upper()}</span>
 (controller: {mgr.controller.value})</h2>
<div class="box"><b>Capability:</b> {html.escape(r.capability)} &nbsp; <b>Mode:</b> {r.mode} &nbsp; <b>Step:</b> {r.step_id}<br>
<b>Reason:</b> {html.escape(r.reason)}<br><b>Detail:</b> <pre>{html.escape(r.detail)}</pre>
<b>URL:</b> {html.escape(r.url)} &nbsp; <b>Requested:</b> {r.created_at}</div>
<div class="box"><b>Live screenshot</b> <a href="/interventions/{r.id}/refresh">[refresh]</a><br>
<img src="/interventions/{r.id}/screenshot.png?t={id(r.perception)}"></div>
<div class="box"><b>Addressable elements</b><pre>{els}</pre>
<form method="post" action="/interventions/{r.id}/act">
 <select name="action"><option>click</option><option>type</option><option>select</option><option>extract</option></select>
 ref <input name="ref" size="6"> value <input name="value" size="30"> <button {"disabled" if not open_ else ""}>Do it on the live session</button>
</form></div>
<div class="box"><b>Actions taken by operator</b><ul>{acts or "<li>(none)</li>"}</ul></div>
<div class="box"><form method="post" action="/interventions/{r.id}/resume">
 notes <input name="notes" size="60"> resume from step (optional) <input name="resume_from_step" size="10">
 <button name="decision" value="resume" {"disabled" if not open_ else ""}>Hand control back &amp; resume</button>
 <button name="decision" value="abort" {"disabled" if not open_ else ""}>Abort run</button></form></div>
</body></html>"""

    @app.get("/interventions/{iid}/refresh")
    def refresh(iid: str):
        mgr.submit(OperatorCommand(kind="refresh"))
        return RedirectResponse(f"/interventions/{iid}", status_code=303)

    @app.post("/interventions/{iid}/act")
    def act(iid: str, action: str = Form("click"), ref: str = Form(...), value: str | None = Form(None)):
        res = mgr.submit(OperatorCommand(kind="act", action=action, ref=ref, value=value or None))
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return RedirectResponse(f"/interventions/{iid}", status_code=303)

    @app.post("/interventions/{iid}/resume")
    def resume(
        iid: str, decision: str = Form("resume"), notes: str = Form(""), resume_from_step: str | None = Form(None)
    ):
        res = mgr.submit(
            OperatorCommand(
                kind="resume" if decision == "resume" else "abort",
                notes=notes,
                resume_from_step=resume_from_step or None,
            )
        )
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return HTMLResponse(
            f"<p>Control handed back to automation ({decision}). <a href='/interventions/{iid}'>view</a></p>"
        )

    return app


def serve_in_thread(mgr: HandoffManager, port: int) -> threading.Thread:
    config = uvicorn.Config(build_app(mgr), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="operator-console")
    t.start()
    return t

"""Human-in-the-loop handoff.

Control-transfer model
----------------------
A session is owned by exactly one controller at a time: AUTOMATION or HUMAN.
When automation cannot safely proceed it creates an InterventionRequest
(why, where, what the screen looks like), flips the owner to HUMAN and blocks.
The live Playwright session is NOT closed; it is exposed in two ways:

1. Operator surface (FastAPI, handoff/server.py): shows the request context and
   the live screenshot, and lets the operator click/type/select on the same
   page through the same Surface. Every operator action is executed by the
   automation thread (Playwright sync objects are thread-affine) via a command
   queue, and recorded to evidence — that is "what the human did".
2. Raw session: the browser can be launched headed, and/or with a CDP port so
   a real co-browsing console could attach. Actions done that way are captured
   as a before/after accessibility diff rather than as discrete steps.

The operator ends the intervention with resume (automation continues from the
step it paused on, or from a step the operator names), or abort.
"""

from __future__ import annotations

import difflib
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from artifact.schema import ActionType
from observability.runlog import RunLog, now_iso
from surface.base import Perception, Surface


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class OperatorCommand:
    kind: str  # "act" | "resume" | "abort" | "refresh"
    ref: str | None = None
    action: str | None = None
    value: str | None = None
    notes: str = ""
    resume_from_step: str | None = None
    reply: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    mode: str  # discovery | replay
    capability: str
    step_id: str | None
    reason: str
    detail: str
    url: str
    created_at: str
    state: str = "open"  # open | resolved | aborted
    controller: Controller = Controller.HUMAN
    perception: Perception | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    resume_from_step: str | None = None
    resolved_at: str | None = None
    a11y_before: str = ""
    a11y_after: str = ""

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in ("perception", "a11y_before", "a11y_after")}


class HandoffAborted(Exception):
    pass


class HandoffManager:
    def __init__(
        self,
        surface: Surface,
        runlog: RunLog,
        *,
        port: int = 8765,
        open_server: bool = True,
        timeout_s: float = 1800,
        auto_resolver: Callable[[HandoffManager, InterventionRequest], None] | None = None,
    ):
        self.surface = surface
        self.runlog = runlog
        self.port = port
        self.timeout_s = timeout_s
        self.controller = Controller.AUTOMATION
        self.requests: dict[str, InterventionRequest] = {}
        self.commands: queue.Queue[OperatorCommand] = queue.Queue()
        self._server_started = False
        self._open_server = open_server
        self._auto_resolver = auto_resolver  # tests / demos: a programmatic "operator"

    # -- server ---------------------------------------------------------------
    def _ensure_server(self) -> None:
        if self._server_started or not self._open_server:
            return
        from .server import serve_in_thread

        serve_in_thread(self, self.port)
        self._server_started = True

    # -- API used by the automation side ---------------------------------------
    def request_intervention(
        self, *, mode: str, capability: str, step_id: str | None, reason: str, detail: str = ""
    ) -> InterventionRequest:
        perception = self.surface.perceive()
        req = InterventionRequest(
            id=uuid.uuid4().hex[:8],
            run_id=self.runlog.run_id,
            mode=mode,
            capability=capability,
            step_id=step_id,
            reason=reason,
            detail=detail,
            url=perception.url,
            created_at=now_iso(),
            perception=perception,
            a11y_before=perception.a11y_text(),
        )
        self.requests[req.id] = req
        self.controller = Controller.HUMAN
        self.runlog.binary(f"handoff/{req.id}/screenshot_before.png", perception.screenshot)
        self.runlog.artifact_file(f"handoff/{req.id}/request.json", req.public())
        self.runlog.event("handoff_requested", intervention_id=req.id, reason=reason, step_id=step_id, url=req.url)
        self._ensure_server()
        if self._open_server:
            print(
                f"\n*** HUMAN INTERVENTION REQUIRED ***\n  reason: {reason}\n  operator console: "
                f"http://127.0.0.1:{self.port}/interventions/{req.id}\n",
                flush=True,
            )
        if self._auto_resolver:
            threading.Thread(target=self._auto_resolver, args=(self, req), daemon=True).start()
        return req

    def wait_for_human(self, req: InterventionRequest) -> InterventionRequest:
        """Block the automation thread, servicing operator commands, until resume/abort."""
        deadline = time.time() + self.timeout_s
        while True:
            try:
                cmd = self.commands.get(timeout=0.5)
            except queue.Empty:
                if time.time() > deadline:
                    req.state = "aborted"
                    self._finish(req)
                    raise HandoffAborted("operator did not respond before timeout")
                continue
            try:
                cmd.reply.put(self._service(req, cmd))
            except Exception as e:
                cmd.reply.put({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
            if cmd.kind in ("resume", "abort"):
                self._finish(req)
                if cmd.kind == "abort":
                    raise HandoffAborted(req.notes or "operator aborted")
                return req

    def _service(self, req: InterventionRequest, cmd: OperatorCommand) -> dict[str, Any]:
        if cmd.kind == "refresh":
            req.perception = self.surface.perceive()
            return {"ok": True}
        if cmd.kind == "act":
            assert req.perception is not None
            el = req.perception.element(cmd.ref or "")
            if el is None:
                return {"ok": False, "error": f"unknown ref {cmd.ref}"}
            action = ActionType(cmd.action or "click")
            handle, used, _ = self.surface.resolve(el.candidate_locators(), 5000)
            extracted = self.surface.act(action, handle, cmd.value, 5000)
            record = {
                "at": now_iso(),
                "action": action.value,
                "ref": cmd.ref,
                "target": used.describe(),
                "value": cmd.value,
                "result": extracted or "ok",
                "url_after": self.surface.current_url(),
            }
            req.human_actions.append(record)
            self.runlog.event("human_action", intervention_id=req.id, **record)
            req.perception = self.surface.perceive()
            return {"ok": True, "record": record}
        if cmd.kind in ("resume", "abort"):
            req.notes = cmd.notes
            req.resume_from_step = cmd.resume_from_step
            req.state = "resolved" if cmd.kind == "resume" else "aborted"
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd.kind}"}

    def _finish(self, req: InterventionRequest) -> None:
        req.resolved_at = now_iso()
        req.controller = Controller.AUTOMATION
        self.controller = Controller.AUTOMATION
        after = self.surface.perceive()
        req.a11y_after = after.a11y_text()
        diff = "\n".join(
            difflib.unified_diff(
                req.a11y_before.splitlines(),
                req.a11y_after.splitlines(),
                "before_handoff",
                "after_handoff",
                lineterm="",
            )
        )
        self.runlog.binary(f"handoff/{req.id}/screenshot_after.png", after.screenshot)
        self.runlog.artifact_file(
            f"handoff/{req.id}/resolution.json", {**req.public(), "a11y_diff": diff, "url_after": after.url}
        )
        self.runlog.event(
            "handoff_" + req.state,
            intervention_id=req.id,
            human_actions=len(req.human_actions),
            notes=req.notes,
            resume_from_step=req.resume_from_step,
        )

    # -- API used by the operator side (any thread) ----------------------------
    def submit(self, cmd: OperatorCommand, timeout: float = 30) -> dict[str, Any]:
        if self.controller != Controller.HUMAN:
            return {"ok": False, "error": "automation is in control; no open intervention"}
        self.commands.put(cmd)
        try:
            return cmd.reply.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "automation thread did not service the command in time"}

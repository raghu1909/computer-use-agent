"""Discovery: the only place a model decides anything.

observe (perceive) -> decide (LLM) -> guardrail -> act -> record, until the
model says done, or a stopping condition (max steps, dead-end, model asks for
a human) triggers. A successful run is distilled into a CapabilityArtifact.

Probing: after the happy path is recorded, the agent can replay the artifact
with inputs designed to hit exceptional states (bad member id, injected
session timeout). When the deterministic replay stops, the model is asked
once to classify the screen; the classification is written into the artifact
as an OutcomeRule or RecoveryRule and verified by replaying again. Replay
itself never calls the model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from agent.llm import LLM
from agent.prompts import elements_to_text
from artifact.recorder import Recorder, StepRecord
from artifact.schema import (
    ActionType,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    OutcomeRule,
    RecoveryRule,
    RiskLevel,
    SafetyPolicy,
    StepDef,
)
from handoff.manager import HandoffAborted, HandoffManager
from observability.runlog import RunLog
from replay.engine import ReplayEngine
from replay.result import ReplayResult
from safety.policy import Guardrails, classify_risk
from surface.base import ResolveError, Surface


class DiscoveryResult(BaseModel):
    status: str  # success | failed | aborted
    steps: int
    llm_calls: int
    reason: str = ""
    artifact_path: str | None = None
    handoffs: list[str] = []
    probes: list[dict[str, Any]] = []


@dataclass
class DiscoveryAgent:
    surface: Surface
    llm: LLM
    runlog: RunLog
    policy: SafetyPolicy
    handoff: HandoffManager | None = None
    max_steps: int = 25
    stuck_after: int = 3
    transcript: list[StepRecord] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    llm_calls: int = 0
    _secret_values: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------------
    def run(
        self, goal: str, entry_url: str, params: dict[str, str], secrets: dict[str, str], app_family: str | None = None
    ) -> tuple[CapabilityArtifact | None, DiscoveryResult]:
        guard = Guardrails(self.policy)
        for k, v in secrets.items():
            self.runlog.redactor.add_secret(k, v)
            self._secret_values.add(v)
        self.runlog.set_meta(
            goal=goal,
            entry_url=entry_url,
            params=params,
            secret_params=list(secrets),
            llm=self.llm.name,
            max_steps=self.max_steps,
        )
        chk = guard.check(ActionType.NAVIGATE, url=entry_url, target_url=entry_url)
        if not chk.allowed:
            return None, DiscoveryResult(status="failed", steps=0, llm_calls=0, reason=chk.reason)
        self.surface.navigate(entry_url, 15000)
        all_values = {**params, **secrets}
        handoffs: list[str] = []
        same_screen = 0
        last_fp = None
        pending_intervention: str | None = None

        for step_no in range(1, self.max_steps + 1):
            t0 = time.time()
            perception = self.surface.perceive()
            elements_text = elements_to_text(perception.elements)
            if perception.fingerprint == last_fp:
                same_screen += 1
            else:
                same_screen, last_fp = 0, perception.fingerprint

            if same_screen >= self.stuck_after:
                iid = self._escalate(
                    goal,
                    "stuck: screen unchanged after repeated actions",
                    f"{same_screen} consecutive actions left the screen unchanged at {perception.url}",
                )
                if iid is None:
                    return None, self._finish("aborted", "operator aborted during stuck handoff", handoffs)
                handoffs.append(iid)
                pending_intervention = iid
                same_screen = 0
                self.history.append(f"[step {step_no}] human operator intervened; continue from the current screen")
                continue

            decision = self.llm.decide(
                goal, perception.a11y_text(), elements_text, self.history, all_values, perception.screenshot
            )
            self.llm_calls += 1
            rec = StepRecord(
                index=step_no,
                url_before=perception.url,
                fingerprint_before=perception.fingerprint,
                decision=decision,
                a11y_before=perception.a11y_text(),
                human_intervention=pending_intervention,
            )
            pending_intervention = None

            if decision.action == "done":
                rec.duration_ms = int((time.time() - t0) * 1000)
                self._log(rec, perception.screenshot)
                artifact = self._build(goal, entry_url, params, secrets, perception, app_family)
                return artifact, self._finish("success", decision.reasoning, handoffs)
            if decision.action == "fail":
                self._log(rec, perception.screenshot)
                return None, self._finish("failed", f"model gave up: {decision.reasoning}", handoffs)
            if decision.action == "ask_human":
                self._log(rec, perception.screenshot)
                iid = self._escalate(goal, "model requested a human", decision.reasoning)
                if iid is None:
                    return None, self._finish("aborted", "operator aborted", handoffs)
                handoffs.append(iid)
                pending_intervention = iid
                self.history.append(f"[step {step_no}] asked for a human; operator acted; continue")
                continue

            # -- act -------------------------------------------------------------
            action = ActionType(decision.action)
            element = perception.element(decision.ref) if decision.ref else None
            risk = (
                RiskLevel.RISKY
                if decision.risk == "risky"
                else classify_risk(action, element.name if element else None)
            )
            verdict = guard.check(
                action,
                url=perception.url,
                risk=risk,
                value=decision.value,
                target_url=decision.value if action == ActionType.NAVIGATE else None,
            )
            if verdict.verdict == "block":
                rec.error = f"blocked by policy: {verdict.reason}"
                self._log(rec, perception.screenshot)
                self.history.append(
                    f"[step {step_no}] {decision.action} {decision.ref or ''} BLOCKED: {verdict.reason}"
                )
                continue
            if verdict.verdict == "confirm":
                iid = self._escalate(
                    goal,
                    "risky step requires confirmation",
                    f"{decision.action} on {element.name if element else decision.ref}: {decision.reasoning}. "
                    "Resume = confirm, Abort = refuse.",
                )
                if iid is None:
                    return None, self._finish("aborted", "operator refused risky step", handoffs)
                handoffs.append(iid)
                rec.human_intervention = iid
            try:
                if action == ActionType.NAVIGATE:
                    self.surface.navigate(decision.value or "", 15000)
                elif action == ActionType.PRESS and element is None:
                    self.surface.act(action, None, decision.value, 5000)
                else:
                    if element is None:
                        raise LookupError(f"ref {decision.ref!r} is not on screen")
                    rec.element = element
                    handle, _, _ = self.surface.resolve(element.candidate_locators(), 5000)
                    rec.extracted = self.surface.act(action, handle, decision.value, 5000)
                rec.url_after = self.surface.current_url()
                if decision.expect:
                    ok, _ = self.surface.wait_for(
                        Condition(kind=ConditionKind.TEXT_PRESENT, value=decision.expect, timeout_ms=4000)
                    )
                    rec.expect_seen = ok
                shown = "[secret]" if decision.value and decision.value in secrets.values() else decision.value
                self.history.append(
                    f"[step {step_no}] {decision.action} {decision.ref or ''} "
                    f"{'value=' + repr(shown) if shown else ''} -> url={rec.url_after}"
                    f"{' expect_seen=' + str(rec.expect_seen) if decision.expect else ''}"
                    f"{' extracted=' + repr(rec.extracted) if rec.extracted else ''}"
                )
            except (ResolveError, LookupError) as e:
                rec.error = str(e).splitlines()[0]
                self.history.append(f"[step {step_no}] {decision.action} {decision.ref or ''} FAILED: {rec.error}")
            rec.duration_ms = int((time.time() - t0) * 1000)
            self._log(rec, self.surface.screenshot())

        return None, self._finish("failed", f"max steps ({self.max_steps}) reached", handoffs)

    # ------------------------------------------------------------------------
    def probe(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        secrets: dict[str, str],
        label: str,
        entry_url: str | None = None,
    ) -> dict[str, Any]:
        """Replay with adversarial inputs; ask the model once to classify where it stops; encode; verify."""
        probe_log = RunLog("probe", root=self.runlog.dir.parent, redactor=self.runlog.redactor, label=label)
        probe_artifact = artifact.model_copy(update={"entry_url": entry_url or artifact.entry_url}, deep=True)
        engine = ReplayEngine(self.surface, probe_log)
        first = engine.replay(probe_artifact, params, secrets)
        report: dict[str, Any] = {
            "label": label,
            "params": params,
            "entry_url": probe_artifact.entry_url,
            "first_replay": first.summary(),
            "evidence": str(probe_log.dir),
        }
        if (
            first.status != "failure"
            or not first.failure
            or not first.failure.step_id
            or first.failure.step_id not in {s.step_id for s in artifact.steps}
        ):
            report["encoded"] = None
            return report
        step_id = first.failure.step_id
        step = next(s for s in artifact.steps if s.step_id == step_id)
        perception = self.surface.perceive()
        cls = self.llm.classify(
            artifact.meta.source_goal or artifact.description,
            f"{step_id}: {step.description}",
            perception.a11y_text(),
            elements_to_text(perception.elements),
            [s.step_id for s in artifact.steps],
        )
        self.llm_calls += 1
        report["classification"] = cls.model_dump()
        cond = Condition(
            kind=ConditionKind.TEXT_PRESENT,
            value=cls.match_text,
            timeout_ms=0,
            description=f"identified during probe '{label}'",
        )
        if cls.kind == "business_outcome":
            step.outcomes.append(OutcomeRule(code=cls.code, when=cond, message=cls.message or cls.code))
            report["encoded"] = f"outcome {cls.code} on {step_id}"
        elif cls.kind == "recoverable":
            actions: list[StepDef] = []
            if cls.clear_ref:
                el = perception.element(cls.clear_ref)
                if el:
                    actions.append(
                        StepDef(
                            step_id=f"clear_{cls.code.lower()}",
                            action=ActionType.CLICK,
                            description=f"clear {cls.code}: click {el.name}",
                            locators=el.candidate_locators(),
                        )
                    )
            rule = RecoveryRule(
                code=cls.code,
                when=cond,
                actions=actions,
                retry_from_step=cls.retry_from_step
                if cls.retry_from_step in {s.step_id for s in artifact.steps}
                else None,
            )
            # interstitials such as session expiry can appear at any step -> global
            artifact.global_recoveries.append(rule)
            report["encoded"] = f"recovery {cls.code} (global) retry_from={rule.retry_from_step}"
        else:
            report["encoded"] = None
            return report
        # verify: a fresh replay must now reach the state the rule claims, not merely stop differently
        verify_log = RunLog("probe_verify", root=self.runlog.dir.parent, redactor=self.runlog.redactor, label=label)
        second = self._verify(artifact, params, secrets, entry_url, verify_log)
        report["verify_replay"] = second.summary()
        report["verified"] = self._rule_verified(cls.kind, cls.code, second)
        if not report["verified"] and cls.kind == "recoverable" and rule.retry_from_step:
            # The model's resume point was too late (typed input was lost with the interstitial):
            # deterministically fall back to the first type step of the form being submitted and re-verify.
            fallback = self._form_start(artifact, rule.retry_from_step)
            if fallback != rule.retry_from_step:
                rule.retry_from_step = fallback
                verify_log = RunLog(
                    "probe_verify", root=self.runlog.dir.parent, redactor=self.runlog.redactor, label=f"{label}_2"
                )
                second = self._verify(artifact, params, secrets, entry_url, verify_log)
                report["verify_replay"] = second.summary()
                report["verified"] = self._rule_verified(cls.kind, cls.code, second)
                report["encoded"] = f"recovery {cls.code} (global) retry_from={fallback} (adjusted)"
        if not report["verified"]:
            # never ship a rule that does not demonstrably work; the probe evidence still records the attempt
            if cls.kind == "business_outcome":
                step.outcomes.remove(step.outcomes[-1])
            else:
                artifact.global_recoveries.remove(rule)
            report["encoded"] = f"{report['encoded']} -> dropped (verification failed)"
        return report

    def _verify(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        secrets: dict[str, str],
        entry_url: str | None,
        log: RunLog,
    ) -> ReplayResult:
        probe_artifact = artifact.model_copy(update={"entry_url": entry_url or artifact.entry_url}, deep=True)
        return ReplayEngine(self.surface, log).replay(probe_artifact, params, secrets)

    @staticmethod
    def _rule_verified(kind: str, code: str, res: ReplayResult) -> bool:
        if kind == "business_outcome":
            return res.status == "business_outcome" and res.outcome is not None and res.outcome.code == code
        return res.status == "success" and code in res.recoveries

    @staticmethod
    def _form_start(artifact: CapabilityArtifact, step_id: str) -> str:
        """Earliest step of the contiguous run of type steps that leads up to (and includes) step_id."""
        ids = [s.step_id for s in artifact.steps]
        i = ids.index(step_id)
        while i > 0 and artifact.steps[i - 1].action == ActionType.TYPE:
            i -= 1
        return ids[i]

    # ------------------------------------------------------------------------
    def _escalate(self, goal: str, reason: str, detail: str) -> str | None:
        if not self.handoff:
            return None
        req = self.handoff.request_intervention(
            mode="discovery", capability=goal, step_id=None, reason=reason, detail=detail
        )
        try:
            self.handoff.wait_for_human(req)
        except HandoffAborted:
            return None
        return req.id

    def _log(self, rec: StepRecord, screenshot: bytes) -> None:
        redact = rec.decision.value is not None and rec.decision.value in self._secret_values
        self.transcript.append(rec)
        self.runlog.step(rec.index, {"mode": "discovery", **rec.as_log(redact)}, screenshot)

    def _build(
        self, goal: str, entry_url: str, params: dict[str, str], secrets: dict[str, str], final, app_family: str | None
    ) -> CapabilityArtifact:
        transcript_text = "\n".join(self.history)
        outputs = [
            r.decision.output_name or f"value_{i}"
            for i, r in enumerate(self.transcript)
            if r.decision.action == "extract" and not r.error
        ]
        distill = None
        try:
            distill = self.llm.distill(goal, transcript_text, params, outputs)
            self.llm_calls += 1
        except Exception as e:  # distillation is cosmetic; never lose the run over it
            self.runlog.event("distill_failed", error=str(e))
        rec = Recorder(params, secrets, self.policy)
        return rec.build(
            goal,
            entry_url,
            self.transcript,
            final,
            distill,
            model=self.llm.name,
            run_id=self.runlog.run_id,
            app_family=app_family,
        )

    def _finish(self, status: str, reason: str, handoffs: list[str]) -> DiscoveryResult:
        return DiscoveryResult(
            status=status, steps=len(self.transcript), llm_calls=self.llm_calls, reason=reason, handoffs=handoffs
        )

"""Deterministic replay: no model in the loop.

Per step: guardrail check -> resolve locator chain -> act -> wait for declared
post-conditions. Any deviation is classified in this order:
  1. declared business outcome  -> terminal, reported as such (not a failure)
  2. declared recoverable       -> run its clearing actions, resume from the declared step
  3. HTTP error page            -> hard failure
  4. otherwise                  -> handoff to a human if available, else hard failure
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from artifact.schema import (
    ActionType,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    OutcomeRule,
    ParamType,
    RecoveryRule,
    SafetyPolicy,
    StepDef,
)
from handoff.manager import HandoffAborted, HandoffManager
from observability.runlog import RunLog
from safety.policy import Guardrails
from surface.base import ResolveError, Surface

from .result import BusinessOutcome, FailureDetail, FailureKind, ReplayResult, StepTrace


class ConditionFailed(Exception):
    def __init__(self, cond: Condition, observed: str):
        self.cond = cond
        self.observed = observed
        super().__init__(f"{cond.kind.value}={cond.value!r} not met; observed {observed}")


class ParamError(Exception):
    pass


def structural_fingerprint(a11y_text: str) -> str:
    """Hash of the accessibility tree with quoted content removed — shape, not data."""
    import hashlib

    shape = re.sub(r'"[^"]*"', '""', a11y_text)
    shape = re.sub(r"— \S+", "", shape)
    return hashlib.sha1(shape.encode()).hexdigest()[:16]


def render(template: str | None, values: dict[str, str]) -> str | None:
    if template is None:
        return None

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise ParamError(f"template references unknown parameter {key!r}")
        return values[key]

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", sub, template)


def postprocess(value: str, how: str | None, typ: str) -> Any:
    v = value.strip()
    if how == "money" or typ == "decimal":
        cleaned = re.sub(r"[^\d.\-]", "", v)
        try:
            return str(Decimal(cleaned))
        except InvalidOperation:
            return v
    if how == "digits" or typ == "integer":
        digits = re.sub(r"\D", "", v)
        return int(digits) if digits else v
    return v


def validate_params(artifact: CapabilityArtifact, params: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in artifact.parameters:
        if p.name not in params or params[p.name] in (None, ""):
            if p.required:
                raise ParamError(f"missing required parameter {p.name!r}")
            continue
        v = str(params[p.name])
        if p.pattern and not re.fullmatch(p.pattern, v):
            raise ParamError(f"parameter {p.name!r} does not match /{p.pattern}/")
        if p.type == ParamType.INTEGER and not re.fullmatch(r"-?\d+", v):
            raise ParamError(f"parameter {p.name!r} must be an integer")
        if p.type == ParamType.DECIMAL and not re.fullmatch(r"-?\d+(\.\d+)?", v):
            raise ParamError(f"parameter {p.name!r} must be a decimal number")
        if p.type == ParamType.ENUM and p.choices and v not in p.choices:
            raise ParamError(f"parameter {p.name!r} must be one of {p.choices}")
        out[p.name] = v
    unknown = set(params) - {p.name for p in artifact.parameters}
    if unknown:
        raise ParamError(f"unknown parameters: {sorted(unknown)}")
    return out


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        runlog: RunLog,
        *,
        handoff: HandoffManager | None = None,
        confirm_risky: bool = False,
        policy_override: SafetyPolicy | None = None,
        require_approved: bool = False,
    ):
        self.surface = surface
        self.log = runlog
        self.handoff = handoff
        self.confirm_risky = confirm_risky
        self.policy_override = policy_override
        self.require_approved = require_approved

    # ------------------------------------------------------------------------
    def replay(
        self, artifact: CapabilityArtifact, params: dict[str, Any], secrets: dict[str, str] | None = None
    ) -> ReplayResult:
        t0 = time.time()
        secrets = secrets or {}
        for k, v in secrets.items():
            self.log.redactor.add_secret(k, v)
        res = ReplayResult(
            capability=artifact.name,
            capability_version=artifact.version,
            run_id=self.log.run_id,
            status="failure",
            params={
                **{
                    k: ("[REDACTED]" if (pd := artifact.param(k)) is not None and pd.sensitive else v)
                    for k, v in params.items()
                },
                **{k: "[REDACTED]" for k in secrets},
            },
        )
        self.log.set_meta(
            capability=artifact.name,
            capability_version=artifact.version,
            params=res.params,
            entry_url=artifact.entry_url,
        )
        if self.require_approved and artifact.status != "approved":
            return self._fail(
                res,
                None,
                "guardrail_blocked",
                "artifact status 'approved'",
                f"artifact status is {artifact.status!r}",
                t0,
            )
        try:
            values = validate_params(artifact, {**params, **secrets})
        except ParamError as e:
            return self._fail(res, None, "param_invalid", "valid parameters", str(e), t0)

        policy = self.policy_override or artifact.safety
        guard = Guardrails(policy)
        chk = guard.check(ActionType.NAVIGATE, url=artifact.entry_url, target_url=artifact.entry_url)
        if not chk.allowed:
            return self._fail(res, None, "guardrail_blocked", "entry url inside allowlist", chk.reason, t0)
        self.surface.navigate(artifact.entry_url, 15000)

        steps = artifact.steps
        index = {s.step_id: n for n, s in enumerate(steps)}
        recovery_attempts: dict[str, int] = {}
        i = 0
        while i < len(steps):
            step = steps[i]
            st0 = time.time()
            trace = StepTrace(step_id=step.step_id, action=step.action.value)
            try:
                value = render(step.value, values)
                trace.value = "[REDACTED]" if self._is_secret(step.value, artifact) else value
                # -- guardrail --------------------------------------------------
                verdict = guard.check(
                    step.action,
                    url=self.surface.current_url(),
                    risk=step.risk,
                    value=value,
                    target_url=value if step.action == ActionType.NAVIGATE else None,
                )
                if verdict.verdict == "block":
                    trace.status = "blocked"
                    res.trace.append(trace)
                    return self._fail(
                        res, step.step_id, "guardrail_blocked", "action permitted by policy", verdict.reason, t0
                    )
                if verdict.verdict == "confirm" and not self.confirm_risky:
                    if not self._confirm_via_human(res, artifact, step, verdict.reason):
                        trace.status = "blocked"
                        res.trace.append(trace)
                        return self._fail(
                            res, step.step_id, "guardrail_blocked", "confirmation for risky step", "not confirmed", t0
                        )
                # -- act ----------------------------------------------------------
                extracted = self._execute(step, value, values, trace)
                if step.action == ActionType.EXTRACT and step.output:
                    odef = next(o for o in artifact.outputs if o.name == step.output)
                    res.outputs[step.output] = postprocess(extracted or "", odef.postprocess, odef.type)
                    trace.observed = f"{step.output}={res.outputs[step.output]!r}"
                # outcomes can also show up on a step that technically 'succeeded'
                hit = self._match_outcomes(step.outcomes + artifact.global_outcomes, values)
                if hit:
                    trace.status = "outcome"
                    self._record(trace, st0)
                    res.trace.append(trace)
                    return self._outcome(res, step, hit, t0)
                trace.status = "ok"
                self._record(trace, st0)
                res.trace.append(trace)
                i += 1

            except (ResolveError, ConditionFailed) as e:
                trace.observed = str(e)
                if isinstance(e, ResolveError):
                    trace.locator_attempts = e.attempts
                # 1. business outcome?
                hit = self._match_outcomes(step.outcomes + artifact.global_outcomes, values)
                if hit:
                    trace.status = "outcome"
                    self._record(trace, st0)
                    res.trace.append(trace)
                    return self._outcome(res, step, hit, t0)
                # 2. recoverable?
                rec = self._match_recovery(step.recoveries + artifact.global_recoveries, values)
                if rec and recovery_attempts.get(rec.code, 0) < rec.max_attempts:
                    recovery_attempts[rec.code] = recovery_attempts.get(rec.code, 0) + 1
                    trace.status = "recovered"
                    trace.observed += f" -> recovery {rec.code}"
                    self._record(trace, st0)
                    res.trace.append(trace)
                    res.recoveries.append(rec.code)
                    self.log.event("recovery", code=rec.code, step_id=step.step_id)
                    for a in rec.actions:
                        atrace = StepTrace(step_id=f"{step.step_id}/recover:{a.step_id}", action=a.action.value)
                        self._execute(a, render(a.value, values), values, atrace)
                        atrace.status = "ok"
                        res.trace.append(atrace)
                    i = index[rec.retry_from_step] if rec.retry_from_step else i
                    continue
                # 3. classify the hard failure
                is_err, obs = self.surface.check(Condition(kind=ConditionKind.HTTP_ERROR_PAGE))
                if is_err:
                    kind: FailureKind = "http_error_page"
                    observed = obs
                elif isinstance(e, ResolveError):
                    kind, observed = "locator_unresolved", "no locator matched"
                else:
                    kind, observed = "checkpoint_failed", e.observed
                # 4. hand off (human fixes the live session, tells us where to resume), or fail
                if self.handoff:
                    trace.status = "handoff"
                    self._record(trace, st0)
                    res.trace.append(trace)
                    try:
                        resume_from = self._handoff(
                            res,
                            artifact,
                            step,
                            f"automation stuck ({kind})",
                            f"{self._expected(step, e)} | observed: {observed}",
                        )
                    except HandoffAborted as ha:
                        return self._fail(res, step.step_id, "handoff_aborted", self._expected(step, e), str(ha), t0)
                    i = index.get(resume_from, i + 1) if resume_from else i + 1
                    continue
                trace.status = "failed"
                self._record(trace, st0)
                res.trace.append(trace)
                return self._fail(
                    res, step.step_id, kind, self._expected(step, e), observed, t0, attempts=trace.locator_attempts
                )
            except ParamError as e:
                return self._fail(res, step.step_id, "param_invalid", "resolvable parameter template", str(e), t0)
            except Exception as e:  # noqa: BLE001 — surface exceptions become debuggable failures
                trace.status = "failed"
                trace.observed = f"{e.__class__.__name__}: {e}"
                self._record(trace, st0)
                res.trace.append(trace)
                return self._fail(
                    res, step.step_id, "exception", step.description or step.action.value, trace.observed, t0
                )

        # -- final checkpoint ---------------------------------------------------
        for cond in artifact.success_checkpoint:
            c = cond.model_copy(update={"value": render(cond.value, values) or ""})
            ok, obs = self.surface.wait_for(c)
            if not ok:
                return self._fail(res, "checkpoint", "checkpoint_failed", f"{c.kind.value}: {c.value!r}", obs, t0)
        self._drift_check(artifact)
        missing = [o.name for o in artifact.outputs if o.name not in res.outputs]
        if missing:
            return self._fail(res, "outputs", "unexpected_state", f"outputs {missing} extracted", "not extracted", t0)
        res.status = "success"
        res.duration_ms = int((time.time() - t0) * 1000)
        self.log.finish(res, status=res.status, outputs=res.outputs)
        return res

    # ------------------------------------------------------------------------
    def _execute(self, step: StepDef, value: str | None, values: dict[str, str], trace: StepTrace) -> str | None:
        extracted = None
        if step.action == ActionType.NAVIGATE:
            self.surface.navigate(value or "", step.timeout_ms)
        elif step.action in (ActionType.WAIT, ActionType.ASSERT):
            pass
        elif step.action == ActionType.PRESS and not step.locators:
            self.surface.act(step.action, None, value, step.timeout_ms)
        else:
            handle, used, attempts = self.surface.resolve(step.locators, step.timeout_ms)
            trace.locator_used = used.describe()
            trace.locator_attempts = attempts
            extracted = self.surface.act(step.action, handle, value, step.timeout_ms)
        for cond in step.wait_for:
            c = cond.model_copy(update={"value": render(cond.value, values) or ""})
            ok, obs = self.surface.wait_for(c)
            if not ok:
                raise ConditionFailed(c, obs)
        return extracted

    def _match_outcomes(self, rules: list[OutcomeRule], values: dict[str, str]) -> tuple[OutcomeRule, str] | None:
        for r in rules:
            c = r.when.model_copy(update={"value": render(r.when.value, values) or "", "timeout_ms": 0})
            ok, obs = self.surface.check(c)
            if ok:
                return r, obs
        return None

    def _match_recovery(self, rules: list[RecoveryRule], values: dict[str, str]) -> RecoveryRule | None:
        for r in rules:
            c = r.when.model_copy(update={"value": render(r.when.value, values) or "", "timeout_ms": 0})
            ok, _ = self.surface.check(c)
            if ok:
                return r
        return None

    def _confirm_via_human(self, res: ReplayResult, artifact: CapabilityArtifact, step: StepDef, reason: str) -> bool:
        if not self.handoff:
            return False
        try:
            self._handoff(
                res,
                artifact,
                step,
                "risky step requires confirmation",
                f"{reason}: {step.description or step.action.value}. Resume = confirm, Abort = refuse.",
            )
            return True
        except HandoffAborted:
            return False

    def _handoff(
        self, res: ReplayResult, artifact: CapabilityArtifact, step: StepDef, reason: str, detail: str
    ) -> str | None:
        assert self.handoff is not None
        req = self.handoff.request_intervention(
            mode="replay", capability=artifact.name, step_id=step.step_id, reason=reason, detail=detail
        )
        res.handoffs.append(req.id)
        self.handoff.wait_for_human(req)
        return req.resume_from_step

    def _drift_check(self, artifact: CapabilityArtifact) -> None:
        recorded = artifact.meta.checkpoint_fingerprints.get("final")
        if recorded:
            current = structural_fingerprint(self.surface.a11y_dump())
            self.log.event("drift_check", recorded=recorded, current=current, drift=(recorded != current))

    def _is_secret(self, template: str | None, artifact: CapabilityArtifact) -> bool:
        if not template:
            return False
        return any(p.sensitive and f"{{{p.name}}}" in template for p in artifact.parameters)

    @staticmethod
    def _expected(step: StepDef, e: Exception) -> str:
        if isinstance(e, ConditionFailed):
            return f"after {step.action.value}: {e.cond.kind.value} {e.cond.value!r}"
        return f"{step.action.value} target: " + " | ".join(loc.describe() for loc in step.locators)

    def _record(self, trace: StepTrace, st0: float) -> None:
        trace.duration_ms = int((time.time() - st0) * 1000)
        self.log.step(None, {"mode": "replay", **trace.model_dump(mode="json")}, screenshot=self.surface.screenshot())

    def _outcome(self, res: ReplayResult, step: StepDef, hit: tuple[OutcomeRule, str], t0: float) -> ReplayResult:
        rule, obs = hit
        res.status = "business_outcome"
        res.outcome = BusinessOutcome(
            code=rule.code,
            message=rule.message or rule.code,
            at_step=step.step_id,
            observed_text=obs if rule.capture_text else "",
        )
        res.duration_ms = int((time.time() - t0) * 1000)
        self.log.event("business_outcome", code=rule.code, step_id=step.step_id)
        self.log.finish(res, status=res.status, outcome=rule.code)
        return res

    def _fail(
        self,
        res: ReplayResult,
        step_id: str | None,
        kind: FailureKind,
        expected: str,
        observed: str,
        t0: float,
        attempts: list[str] | None = None,
    ) -> ReplayResult:
        res.status = "aborted" if kind in ("handoff_aborted",) else "failure"
        detail = FailureDetail(
            step_id=step_id,
            kind=kind,
            expected=expected,
            observed=observed,
            locator_attempts=attempts or [],
            url=self.surface.current_url(),
            evidence_dir=str(self.log.dir),
        )
        res.failure = detail
        res.duration_ms = int((time.time() - t0) * 1000)
        self.log.failure_snapshot(self.surface.a11y_dump(), self.surface.screenshot(), detail.model_dump(mode="json"))
        self.log.finish(res, status=res.status, failure_kind=kind)
        return res

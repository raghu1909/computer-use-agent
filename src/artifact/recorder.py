"""Turn a discovery transcript into a CapabilityArtifact.

The transcript is the model's raw trail (what it saw, what it chose, why).
The artifact is what survives: per step an ordered locator chain derived from
the element the model acted on, values re-templated onto the declared
parameters, post-conditions taken from what actually happened after the action
(URL change, expected text that was then observed), and outputs from the
extract steps. The model's reasoning goes to evidence, not into the artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from agent.llm import Decision, Distillation
from safety.policy import classify_risk
from surface.base import ElementInfo, Perception

from .schema import (
    ActionType,
    ArtifactMeta,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    OutputDef,
    ParamDef,
    ParamType,
    RiskLevel,
    SafetyPolicy,
    StepDef,
)


@dataclass
class StepRecord:
    index: int
    url_before: str
    fingerprint_before: str
    decision: Decision
    element: ElementInfo | None = None
    url_after: str = ""
    expect_seen: bool | None = None
    extracted: str | None = None
    error: str | None = None
    duration_ms: int = 0
    a11y_before: str = ""
    human_intervention: str | None = None  # intervention id if a handoff happened before this step

    def as_log(self, redact_value: bool) -> dict[str, Any]:
        d = self.decision.model_dump(exclude_none=True)
        if redact_value and "value" in d:
            d["value"] = "[REDACTED]"
        return {
            "url_before": self.url_before,
            "fingerprint_before": self.fingerprint_before,
            "decision": d,
            "element": None
            if not self.element
            else {
                "ref": self.element.ref,
                "role": self.element.role,
                "name": self.element.name,
                "frame": self.element.frame,
                "candidate_locators": [loc.describe() for loc in self.element.candidate_locators()],
            },
            "url_after": self.url_after,
            "expect_seen": self.expect_seen,
            "extracted": self.extracted,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "human_intervention": self.human_intervention,
        }


def templatize(value: str | None, params: dict[str, str]) -> str | None:
    """Replace concrete parameter values with {name} placeholders (longest first)."""
    if value is None:
        return None
    by_value = {v: n for n, v in params.items() if v}
    if not by_value:
        return value
    pattern = "|".join(re.escape(v) for v in sorted(by_value, key=len, reverse=True))
    return re.sub(pattern, lambda m: f"{{{by_value[m.group(0)]}}}", value)  # single pass: no re-substitution


def _path(url: str) -> str:
    u = urlparse(url)
    return u.path + (f"?{u.query}" if u.query else "")


MONEY_RE = re.compile(r"^\$?\s*-?[\d,]+\.\d{2}$")


class Recorder:
    def __init__(self, params: dict[str, str], secrets: dict[str, str], policy: SafetyPolicy):
        self.params = params
        self.secrets = secrets
        self.policy = policy
        self.all_values = {**params, **secrets}

    def build(
        self,
        goal: str,
        entry_url: str,
        transcript: list[StepRecord],
        final: Perception,
        distill: Distillation | None = None,
        model: str | None = None,
        run_id: str | None = None,
        app_family: str | None = None,
    ) -> CapabilityArtifact:
        from replay.engine import structural_fingerprint  # local: avoid import cycle at module load

        steps: list[StepDef] = []
        outputs: list[OutputDef] = []
        output_labels: dict[str, str] = {}
        n = 0
        acting = [
            r
            for r in transcript
            if r.decision.action in ("click", "type", "select", "press", "navigate", "extract") and not r.error
        ]
        for r in acting:
            n += 1
            sid = f"s{n:02d}"
            d = r.decision
            action = ActionType(d.action)
            locators = r.element.candidate_locators() if r.element else []
            value = templatize(d.value, self.all_values)
            wait_for: list[Condition] = []
            if r.url_after and _path(r.url_after) != _path(r.url_before) and action != ActionType.EXTRACT:
                wait_for.append(
                    Condition(
                        kind=ConditionKind.URL_CONTAINS,
                        value=templatize(_path(r.url_after), self.all_values) or "",
                        description="navigation landed where it did during discovery",
                    )
                )
            # an expectation that is (or contains) the extracted sample is data, not a checkpoint
            data_like = bool(r.extracted and d.expect and (r.extracted in d.expect or d.expect in r.extracted))
            if d.expect and r.expect_seen and not data_like:
                wait_for.append(
                    Condition(
                        kind=ConditionKind.TEXT_PRESENT,
                        value=templatize(d.expect, self.all_values) or d.expect,
                        description="text the agent predicted and then observed",
                    )
                )
            risk = (
                RiskLevel.RISKY if d.risk == "risky" else classify_risk(action, r.element.name if r.element else None)
            )
            if distill and sid in distill.risky_steps:
                risk = RiskLevel.RISKY
            desc = (
                (distill.step_descriptions.get(sid) if distill else None)
                or d.reasoning
                or f"{d.action} {r.element.name if r.element else ''}".strip()
            )
            out_name = None
            if action == ActionType.EXTRACT:
                out_name = d.output_name or f"value_{n}"
                sample = r.extracted or ""
                outputs.append(
                    OutputDef(
                        name=out_name,
                        type="decimal" if MONEY_RE.match(sample) else "string",
                        description=(distill.output_descriptions.get(out_name) if distill else None)
                        or f"Text of '{r.element.name if r.element else ''}'",
                        extracted_by=sid,
                        postprocess="money" if MONEY_RE.match(sample) else "strip",
                    )
                )
                if r.element:
                    output_labels[out_name] = r.element.name
            steps.append(
                StepDef(
                    step_id=sid,
                    action=action,
                    description=desc,
                    locators=locators,
                    value=value,
                    output=out_name,
                    risk=risk,
                    wait_for=wait_for,
                    # a safe read/navigation step that waits on the server may be slow once; retry it once
                    retries=1 if (wait_for and risk == RiskLevel.SAFE and action != ActionType.TYPE) else 0,
                )
            )

        params = [
            ParamDef(
                name=k,
                type=ParamType.INTEGER if re.fullmatch(r"\d+", v) else ParamType.STRING,
                description=(distill.parameter_descriptions.get(k) if distill else None) or k,
                example=v,
                pattern=r"\d+" if re.fullmatch(r"\d+", v) else None,
            )
            for k, v in self.params.items()
        ]
        params += [
            ParamDef(
                name=k,
                type=ParamType.STRING,
                sensitive=True,
                required=True,
                description=(distill.parameter_descriptions.get(k) if distill else None)
                or f"{k} (secret; supplied at invocation, never stored)",
            )
            for k in self.secrets
        ]

        checkpoint = [
            Condition(
                kind=ConditionKind.URL_CONTAINS,
                value=templatize(_path(final.url), self.all_values) or "/",
                description="final screen reached during discovery",
            )
        ]
        for label in output_labels.values():
            checkpoint.append(
                Condition(
                    kind=ConditionKind.TEXT_PRESENT, value=label, description="label of an extracted value is on screen"
                )
            )
        if not output_labels:
            last_expect = next(
                (r.decision.expect for r in reversed(acting) if r.decision.expect and r.expect_seen), None
            )
            if last_expect:
                checkpoint.append(
                    Condition(
                        kind=ConditionKind.TEXT_PRESENT, value=templatize(last_expect, self.all_values) or last_expect
                    )
                )

        name = (distill.name if distill else None) or re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")[:48]
        return CapabilityArtifact(
            name=name,
            description=(distill.description if distill else goal),
            entry_url=entry_url,
            parameters=params,
            outputs=outputs,
            steps=steps,
            success_checkpoint=checkpoint,
            safety=self.policy,
            meta=ArtifactMeta(
                model=model,
                discovery_run_id=run_id,
                source_goal=goal,
                app_family=app_family,
                checkpoint_fingerprints={"final": structural_fingerprint(final.a11y_text())},
            ),
        )

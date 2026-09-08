"""The replay result contract returned to the calling agent.

status:
  success           goal reached, checkpoint verified, outputs populated
  business_outcome  a legitimate, declared result (e.g. MEMBER_NOT_FOUND); not an error
  failure           could not complete; `failure` says which step, what was expected, what was observed
  aborted           an operator took over and aborted, or a guardrail refused
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class StepTrace(BaseModel):
    step_id: str
    action: str
    status: Literal["pending", "ok", "recovered", "outcome", "failed", "skipped", "blocked", "handoff"] = "pending"
    locator_used: str | None = None
    locator_attempts: list[str] = Field(default_factory=list)
    value: str | None = None
    observed: str = ""
    duration_ms: int = 0
    attempt: int = 1


FailureKind = Literal[
    "locator_unresolved",
    "checkpoint_failed",
    "guardrail_blocked",
    "http_error_page",
    "unexpected_state",
    "handoff_aborted",
    "param_invalid",
    "exception",
]


class FailureDetail(BaseModel):
    step_id: str | None
    kind: FailureKind
    expected: str
    observed: str
    locator_attempts: list[str] = Field(default_factory=list)
    url: str = ""
    evidence_dir: str | None = None


class BusinessOutcome(BaseModel):
    code: str
    message: str
    at_step: str
    observed_text: str = ""


class ReplayResult(BaseModel):
    capability: str
    capability_version: int
    run_id: str
    status: Literal["success", "business_outcome", "failure", "aborted"]
    params: dict[str, Any]
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    failure: FailureDetail | None = None
    recoveries: list[str] = Field(default_factory=list)  # codes of recoveries applied
    handoffs: list[str] = Field(default_factory=list)  # intervention ids
    trace: list[StepTrace] = Field(default_factory=list)
    duration_ms: int = 0
    llm_calls: int = 0  # always 0 on the production path

    def summary(self) -> str:
        if self.status == "success":
            return f"SUCCESS outputs={self.outputs}"
        if self.status == "business_outcome" and self.outcome:
            return f"OUTCOME {self.outcome.code}: {self.outcome.message} (at {self.outcome.at_step})"
        if self.failure:
            return (
                f"{self.status.upper()} at {self.failure.step_id} [{self.failure.kind}] "
                f"expected: {self.failure.expected} | observed: {self.failure.observed}"
            )
        return self.status.upper()

"""CapabilityArtifact: the typed, versioned contract between discovery and replay.

Design notes (see REPORT.md §2):
- A step never carries *one* locator. It carries an ordered chain; replay walks
  the chain until one resolves. Semantics (role/label/text) come first, structure
  (xpath/css) last, pixels never unless recorded from a screenshot-only surface.
- Errors are declared, not caught. Each step (and the artifact as a whole) lists
  the business outcomes it can legitimately produce and the recoverable
  conditions it knows how to clear. Anything else is a hard failure.
- Parameters are templated into values as ``{name}``; outputs are extracted by
  dedicated steps. The artifact is therefore reusable across invocations and
  reviewable without the model transcript that produced it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"  # keyboard key, e.g. Enter
    WAIT = "wait"  # wait for conditions only
    EXTRACT = "extract"  # read text into an output
    ASSERT = "assert"  # verify conditions, no side effect


class RiskLevel(str, Enum):
    SAFE = "safe"  # read-only or reversible (navigate, type into a form, open a menu)
    RISKY = "risky"  # commits state: submit/confirm/open account/transfer
    BLOCKED = "blocked"  # never executed by automation


class LocatorStrategy(str, Enum):
    ROLE = "role"  # role + accessible name    (most stable; works on desktop a11y trees)
    LABEL = "label"  # associated label text
    PLACEHOLDER = "placeholder"
    TEXT = "text"  # visible text content
    LABEL_ADJACENT = "label_adjacent"  # value cell next to a label cell (legacy table layouts)
    CSS = "css"
    XPATH = "xpath"
    COORDINATE = "coordinate"  # "x,y" — last resort, screenshot-only surfaces


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    name: str | None = None  # accessible name when strategy == role
    frame: str | None = None  # frame name/url substring; None = main frame
    exact: bool = True
    rationale: str | None = None  # why we believe this survives

    def describe(self) -> str:
        frame = f" in frame '{self.frame}'" if self.frame else ""
        if self.strategy == LocatorStrategy.ROLE:
            return f"role={self.value} name={self.name!r}{frame}"
        return f"{self.strategy.value}={self.value!r}{frame}"


class ConditionKind(str, Enum):
    URL_CONTAINS = "url_contains"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    HTTP_ERROR_PAGE = "http_error_page"  # title/body signals a 4xx/5xx page


class Condition(BaseModel):
    kind: ConditionKind
    value: str = ""  # text, url fragment, or a Locator's value
    locator: Locator | None = None  # for element_* kinds
    frame: str | None = None  # for text_* kinds; None = search all frames
    timeout_ms: int = 5000
    description: str = ""


class OutcomeRule(BaseModel):
    """A legitimate business result, e.g. "member not found". Terminal, not a failure."""

    code: str  # e.g. MEMBER_NOT_FOUND
    when: Condition
    message: str = ""
    capture_text: bool = True  # include the matched on-screen text in the result


class RecoveryRule(BaseModel):
    """A known interstitial/transient we can clear and continue from."""

    code: str  # e.g. SESSION_TIMEOUT
    when: Condition
    actions: list[StepDef] = Field(default_factory=list)  # how to clear it (e.g. click CONTINUE)
    retry_from_step: str | None = None  # resume here after clearing; None = re-run current step
    max_attempts: int = 1


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    ENUM = "enum"


class ParamDef(BaseModel):
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    example: str | None = None
    pattern: str | None = None  # regex validated before replay
    choices: list[str] | None = None  # for enum
    sensitive: bool = False  # never logged in clear text


class OutputDef(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "date", "boolean"] = "string"
    description: str = ""
    extracted_by: str  # step_id of the EXTRACT step
    postprocess: Literal["strip", "money", "digits"] | None = "strip"


class StepDef(BaseModel):
    step_id: str
    action: ActionType
    description: str = ""
    locators: list[Locator] = Field(default_factory=list)  # ordered fallback chain
    value: str | None = None  # literal or "{param}" template; key name for PRESS; url for NAVIGATE
    output: str | None = None  # OutputDef.name for EXTRACT
    risk: RiskLevel = RiskLevel.SAFE
    wait_for: list[Condition] = Field(default_factory=list)  # post-conditions proving the action landed
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    recoveries: list[RecoveryRule] = Field(default_factory=list)
    timeout_ms: int = 10000
    retries: int = 0  # extra attempts for transient slowness; replay only honours this on risk=safe steps

    @model_validator(mode="after")
    def _needs_target(self) -> StepDef:
        needs = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}
        if self.action in needs and not self.locators:
            raise ValueError(f"{self.step_id}: action {self.action.value} requires at least one locator")
        if self.action == ActionType.EXTRACT and not self.output:
            raise ValueError(f"{self.step_id}: extract requires an output name")
        return self


class SafetyPolicy(BaseModel):
    allowed_origins: list[str]  # e.g. ["http://localhost:5000"]
    allowed_path_prefixes: list[str] = ["/"]
    allowed_actions: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    risky_step_handling: Literal["execute", "confirm", "block"] = "confirm"
    # "execute": run risky steps unattended (artifact was reviewed & approved)
    # "confirm": pause and require an operator/ caller confirmation token
    # "block":   refuse
    blocked_text_patterns: list[str] = Field(default_factory=list)  # never type these (regex)


class ArtifactMeta(BaseModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = "discovery_agent"
    model: str | None = None
    discovery_run_id: str | None = None
    source_goal: str = ""
    app_family: str | None = None  # vendor product identity, e.g. "corebank-os"
    app_version: str | None = None  # as observed at discovery; used for drift detection
    tenant: str | None = None  # None = base (vendor-generic) artifact
    checkpoint_fingerprints: dict[str, str] = Field(default_factory=dict)  # step_id -> a11y hash


class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str  # snake_case, agent-invocable
    version: int = 1
    status: Literal["draft", "approved", "deprecated"] = "draft"
    description: str
    entry_url: str
    parameters: list[ParamDef]
    outputs: list[OutputDef]
    steps: list[StepDef]
    success_checkpoint: list[Condition]  # all must hold at the end
    max_duration_ms: int | None = 120000  # wall-clock budget for one replay; None = unbounded
    global_outcomes: list[OutcomeRule] = Field(default_factory=list)  # checked after every step
    global_recoveries: list[RecoveryRule] = Field(default_factory=list)
    safety: SafetyPolicy
    meta: ArtifactMeta = Field(default_factory=ArtifactMeta)

    @model_validator(mode="after")
    def _consistent(self) -> CapabilityArtifact:
        ids = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step_id")
        out_names = {o.name for o in self.outputs}
        for s in self.steps:
            if s.action == ActionType.EXTRACT and s.output not in out_names:
                raise ValueError(f"{s.step_id} extracts undeclared output {s.output!r}")
        for o in self.outputs:
            if o.extracted_by not in ids:
                raise ValueError(f"output {o.name} references unknown step {o.extracted_by}")
        for r in self.global_recoveries + [r for s in self.steps for r in s.recoveries]:
            if r.retry_from_step and r.retry_from_step not in ids:
                raise ValueError(f"recovery {r.code} retries from unknown step {r.retry_from_step}")
        return self

    def param(self, name: str) -> ParamDef | None:
        return next((p for p in self.parameters if p.name == name), None)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True)

    @classmethod
    def from_json(cls, text: str) -> CapabilityArtifact:
        return cls.model_validate_json(text)

    def tool_schema(self) -> dict[str, Any]:
        """Function-calling schema an agent can use to invoke this capability."""
        props: dict[str, Any] = {}
        for p in self.parameters:
            js: dict[str, Any] = {"description": p.description}
            js["type"] = {"integer": "integer", "decimal": "number"}.get(p.type.value, "string")
            if p.choices:
                js["enum"] = p.choices
            if p.pattern:
                js["pattern"] = p.pattern
            props[p.name] = js
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.parameters if p.required],
            },
            "returns": {o.name: {"type": o.type, "description": o.description} for o in self.outputs},
            "possible_outcomes": sorted(
                {r.code for r in self.global_outcomes} | {r.code for s in self.steps for r in s.outcomes}
            ),
        }


RecoveryRule.model_rebuild()

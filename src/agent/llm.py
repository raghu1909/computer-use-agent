"""LLM adapters for the discovery loop.

AnthropicLLM  – real model (vision + structured tool-use output).
ScriptedLLM   – deterministic stand-in for tests / offline demos; replays a fixed
                list of decisions. It exercises the same code path (perceive →
                decide → act → record) without a model or a key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from anthropic.types import MessageParam, ToolParam

from .prompts import CLASSIFY_SYSTEM, DISTILL_SYSTEM, STEP_SYSTEM, render_classify, render_distill, render_step

DecisionAction = Literal["click", "type", "select", "press", "navigate", "extract", "done", "fail", "ask_human"]


class Decision(BaseModel):
    action: DecisionAction
    ref: str | None = None  # element ref from perception (e12)
    value: str | None = None  # text to type / option label / key / url
    output_name: str | None = None  # for extract
    risk: Literal["safe", "risky"] = "safe"
    reasoning: str = ""
    expect: str | None = None  # short text expected on the next screen if this works


class Classification(BaseModel):
    kind: Literal["business_outcome", "recoverable", "hard_failure"]
    code: str  # e.g. MEMBER_NOT_FOUND, SESSION_TIMEOUT
    match_text: str  # stable on-screen text that identifies this state
    message: str = ""
    clear_ref: str | None = None  # for recoverable: element to click to clear it
    retry_from_step: str | None = None  # for recoverable: where to resume (None = same step)
    reasoning: str = ""


class Distillation(BaseModel):
    name: str
    description: str
    parameter_descriptions: dict[str, str] = Field(default_factory=dict)
    output_descriptions: dict[str, str] = Field(default_factory=dict)
    step_descriptions: dict[str, str] = Field(default_factory=dict)
    risky_steps: list[str] = Field(default_factory=list)


class LLM:
    name: str = "llm"

    def decide(
        self,
        goal: str,
        perception_text: str,
        elements_text: str,
        history: list[str],
        params: dict[str, str],
        screenshot_png: bytes | None,
    ) -> Decision:
        raise NotImplementedError

    def classify(
        self, goal: str, step_desc: str, perception_text: str, elements_text: str, step_ids: list[str]
    ) -> Classification:
        raise NotImplementedError

    def distill(self, goal: str, transcript_text: str, params: dict[str, str], outputs: list[str]) -> Distillation:
        raise NotImplementedError


# ---------------------------------------------------------------------------
class AnthropicLLM(LLM):
    def __init__(self, model: str | None = None, use_vision: bool = True):
        import anthropic  # local import keeps the key optional for scripted mode

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.name = f"anthropic:{self.model}"
        self.use_vision = use_vision
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _call(self, system: str, text: str, schema: type[BaseModel], png: bytes | None = None) -> BaseModel:
        import base64

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if png and self.use_vision:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
                }
            )
        tool: ToolParam = {
            "name": "respond",
            "description": "Return your decision.",
            "input_schema": schema.model_json_schema(),
        }
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "respond"},
            messages=[cast("MessageParam", {"role": "user", "content": content})],
        )
        self.usage["calls"] += 1
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        block = next(b for b in resp.content if b.type == "tool_use")
        return schema.model_validate(block.input)

    def decide(self, goal, perception_text, elements_text, history, params, screenshot_png) -> Decision:
        return self._call(
            STEP_SYSTEM, render_step(goal, perception_text, elements_text, history, params), Decision, screenshot_png
        )  # type: ignore[return-value]

    def classify(self, goal, step_desc, perception_text, elements_text, step_ids) -> Classification:
        return self._call(
            CLASSIFY_SYSTEM, render_classify(goal, step_desc, perception_text, elements_text, step_ids), Classification
        )  # type: ignore[return-value]

    def distill(self, goal, transcript_text, params, outputs) -> Distillation:
        return self._call(DISTILL_SYSTEM, render_distill(goal, transcript_text, params, outputs), Distillation)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
@dataclass
class ScriptedLLM(LLM):
    """Replays canned decisions. Refs may be given as 'ref:e3' or resolved by
    matching element name/text with 'find:<role>:<name>' so scripts stay stable."""

    decisions: list[dict[str, Any]]
    classifications: dict[str, dict[str, Any]] = field(default_factory=dict)  # match on step_desc substring
    distillation: dict[str, Any] = field(default_factory=dict)
    name: str = "scripted"
    _i: int = 0

    @classmethod
    def from_file(cls, path: str) -> ScriptedLLM:
        data = json.loads(open(path).read())
        return cls(
            decisions=data["decisions"],
            classifications=data.get("classifications", {}),
            distillation=data.get("distillation", {}),
        )

    @staticmethod
    def _resolve_ref(spec: str | None, elements_text: str) -> str | None:
        if not spec or not spec.startswith("find:"):
            return spec
        _, role, name = spec.split(":", 2)
        for line in elements_text.splitlines():
            # line format: e3 [control] button "SIGN ON" ...
            if f" {role} " in f" {line} " and (f'"{name}"' in line or f'"{name} -> ' in line):
                return line.split()[0]
        raise LookupError(f"scripted ref {spec!r} not on screen")

    def decide(self, goal, perception_text, elements_text, history, params, screenshot_png) -> Decision:
        if self._i >= len(self.decisions):
            return Decision(action="fail", reasoning="script exhausted")
        d = dict(self.decisions[self._i])
        self._i += 1
        d["ref"] = self._resolve_ref(d.get("ref"), elements_text)
        return Decision.model_validate(d)

    def classify(self, goal, step_desc, perception_text, elements_text, step_ids) -> Classification:
        for key, c in self.classifications.items():
            if key.lower() in (step_desc + perception_text).lower():
                c = dict(c)
                c["clear_ref"] = self._resolve_ref(c.get("clear_ref"), elements_text)
                return Classification.model_validate(c)
        return Classification(kind="hard_failure", code="UNCLASSIFIED", match_text="", reasoning="no script match")

    def distill(self, goal, transcript_text, params, outputs) -> Distillation:
        base = {"name": "capability", "description": goal}
        base.update(self.distillation)
        return Distillation.model_validate(base)

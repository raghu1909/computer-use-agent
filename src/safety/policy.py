"""Guardrails: the same check runs before every action in discovery and replay.

The policy is embedded in the artifact, so a caller cannot invoke a capability
outside the boundaries it was recorded under. It can be tightened further at
invocation time by passing a narrower policy (intersection, never union).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from artifact.schema import ActionType, RiskLevel, SafetyPolicy

Verdict = Literal["allow", "confirm", "block"]


@dataclass(frozen=True)
class CheckResult:
    verdict: Verdict
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class GuardrailViolation(Exception):
    def __init__(self, result: CheckResult):
        super().__init__(result.reason)
        self.result = result


class Guardrails:
    def __init__(self, policy: SafetyPolicy):
        self.policy = policy
        self._blocked = [re.compile(p, re.I) for p in policy.blocked_text_patterns]

    def url_allowed(self, url: str) -> bool:
        if url in ("", "about:blank"):
            return True
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.policy.allowed_origins:
            return False
        return any(u.path.startswith(p) for p in self.policy.allowed_path_prefixes)

    def check(
        self,
        action: ActionType,
        *,
        url: str,
        risk: RiskLevel = RiskLevel.SAFE,
        value: str | None = None,
        target_url: str | None = None,
    ) -> CheckResult:
        if not self.url_allowed(url):
            return CheckResult("block", f"current page {url} is outside allowed origins/paths")
        if action == ActionType.NAVIGATE and target_url and not self.url_allowed(target_url):
            return CheckResult("block", f"navigation target {target_url} is outside the allowlist")
        if action not in self.policy.allowed_actions:
            return CheckResult("block", f"action {action.value} is not permitted by policy")
        if value and any(p.search(value) for p in self._blocked):
            return CheckResult("block", "value matches a blocked text pattern")
        if risk == RiskLevel.BLOCKED:
            return CheckResult("block", "step is marked BLOCKED")
        if risk == RiskLevel.RISKY:
            if self.policy.risky_step_handling == "block":
                return CheckResult("block", "risky step and policy blocks risky steps")
            if self.policy.risky_step_handling == "confirm":
                return CheckResult("confirm", "risky/irreversible step requires confirmation")
        return CheckResult("allow")

    def enforce(self, *args, confirmed: bool = False, **kwargs) -> CheckResult:
        r = self.check(*args, **kwargs)
        if r.verdict == "block" or (r.verdict == "confirm" and not confirmed):
            raise GuardrailViolation(r)
        return r


RISKY_HINTS = re.compile(
    r"\b(open account|submit|confirm|approve|post|transfer|delete|close|remove|pay|"
    r"withdraw|deposit|commit|finali[sz]e|save changes|apply)\b",
    re.I,
)


def classify_risk(action: ActionType, element_name: str | None, url: str = "") -> RiskLevel:
    """Heuristic default used at record time; the reviewer can override in the artifact."""
    if action in (ActionType.CLICK, ActionType.PRESS) and element_name and RISKY_HINTS.search(element_name):
        return RiskLevel.RISKY
    return RiskLevel.SAFE

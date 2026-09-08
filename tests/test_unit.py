"""Fast tests: no browser."""

import pytest
from pydantic import ValidationError

from artifact.recorder import templatize
from artifact.schema import (
    ActionType,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorStrategy,
    OutputDef,
    ParamDef,
    ParamType,
    RiskLevel,
    SafetyPolicy,
    StepDef,
)
from replay.engine import ParamError, postprocess, render, validate_params
from safety.policy import Guardrails, classify_risk
from safety.redactor import Redactor

POLICY = SafetyPolicy(
    allowed_origins=["http://bank.local"],
    allowed_path_prefixes=["/members", "/"],
    allowed_actions=[ActionType.CLICK, ActionType.TYPE, ActionType.NAVIGATE, ActionType.EXTRACT],
    risky_step_handling="confirm",
    blocked_text_patterns=[r"<script"],
)


def _artifact(**over) -> CapabilityArtifact:
    base = dict(
        name="lookup",
        description="d",
        entry_url="http://bank.local/",
        parameters=[ParamDef(name="member_id", type=ParamType.INTEGER, pattern=r"\d+")],
        outputs=[OutputDef(name="bal", type="decimal", extracted_by="s2", postprocess="money")],
        steps=[
            StepDef(
                step_id="s1",
                action=ActionType.TYPE,
                value="{member_id}",
                locators=[Locator(strategy=LocatorStrategy.CSS, value="input")],
            ),
            StepDef(
                step_id="s2",
                action=ActionType.EXTRACT,
                output="bal",
                locators=[Locator(strategy=LocatorStrategy.LABEL_ADJACENT, value="BAL")],
            ),
        ],
        success_checkpoint=[Condition(kind=ConditionKind.TEXT_PRESENT, value="BAL")],
        safety=POLICY,
    )
    base.update(over)
    return CapabilityArtifact(**base)


# -- schema ------------------------------------------------------------------
def test_artifact_roundtrip_and_tool_schema():
    a = _artifact()
    b = CapabilityArtifact.from_json(a.to_json())
    assert b == a
    ts = a.tool_schema()
    assert ts["input_schema"]["properties"]["member_id"]["type"] == "integer"
    assert ts["input_schema"]["required"] == ["member_id"]
    assert "bal" in ts["returns"]


def test_artifact_rejects_inconsistencies():
    with pytest.raises(ValidationError, match="duplicate step_id"):
        _artifact(
            steps=[
                StepDef(
                    step_id="s1", action=ActionType.CLICK, locators=[Locator(strategy=LocatorStrategy.CSS, value="a")]
                )
            ]
            * 2
        )
    with pytest.raises(ValidationError):
        _artifact(outputs=[OutputDef(name="bal", type="decimal", extracted_by="nope")])
    with pytest.raises(ValidationError):  # extract must declare an output
        StepDef(step_id="x", action=ActionType.EXTRACT, locators=[Locator(strategy=LocatorStrategy.CSS, value="a")])
    with pytest.raises(ValidationError):  # click needs a target
        StepDef(step_id="x", action=ActionType.CLICK)


# -- params / templates ---------------------------------------------------------
def test_validate_params():
    a = _artifact()
    assert validate_params(a, {"member_id": 12345}) == {"member_id": "12345"}
    with pytest.raises(ParamError, match="missing required"):
        validate_params(a, {})
    with pytest.raises(ParamError, match="does not match"):
        validate_params(a, {"member_id": "12a"})
    with pytest.raises(ParamError, match="unknown parameters"):
        validate_params(a, {"member_id": "1", "evil": "x"})


def test_render_and_templatize():
    assert render("/members/{member_id}", {"member_id": "42"}) == "/members/42"
    with pytest.raises(ParamError):
        render("{missing}", {})
    # single pass: substituted names are never re-substituted
    t = templatize("letmein-demo", {"operator_user": "operator", "operator_password": "letmein-demo"})
    assert t == "{operator_password}"
    assert templatize("/members/12345", {"member_id": "12345"}) == "/members/{member_id}"


def test_postprocess():
    assert postprocess("$4,812.37", "money", "decimal") == "4812.37"
    assert postprocess(" 007 ", "digits", "integer") == 7
    assert postprocess(" ACTIVE ", "strip", "string") == "ACTIVE"


# -- safety ---------------------------------------------------------------------
def test_guardrails():
    g = Guardrails(POLICY)
    assert g.check(ActionType.CLICK, url="http://bank.local/members").allowed
    assert g.check(ActionType.CLICK, url="http://evil.local/").verdict == "block"
    assert g.check(ActionType.NAVIGATE, url="http://bank.local/", target_url="http://evil.local/x").verdict == "block"
    assert g.check(ActionType.SELECT, url="http://bank.local/").verdict == "block"  # action not allowed
    assert g.check(ActionType.TYPE, url="http://bank.local/", value="<script>").verdict == "block"
    assert g.check(ActionType.CLICK, url="http://bank.local/", risk=RiskLevel.RISKY).verdict == "confirm"
    assert (
        Guardrails(POLICY.model_copy(update={"risky_step_handling": "block"}))
        .check(ActionType.CLICK, url="http://bank.local/", risk=RiskLevel.RISKY)
        .verdict
        == "block"
    )


def test_classify_risk():
    assert classify_risk(ActionType.CLICK, "SUBMIT TRANSFER") == RiskLevel.RISKY
    assert classify_risk(ActionType.CLICK, "INQUIRE") == RiskLevel.SAFE
    assert classify_risk(ActionType.TYPE, "DELETE") == RiskLevel.SAFE


def test_redactor():
    r = Redactor({"operator_password": "letmein-demo"})
    out = r.obj(
        {
            "a": "pw=letmein-demo ssn 123-45-6789 card 4111 1111 1111 1111",
            "b": ["Bearer abcdefghijklmnopqrstuvwxyz0123456789"],
        }
    )
    assert "letmein-demo" not in str(out) and "[REDACTED:operator_password]" in out["a"]
    assert "123-45-6789" not in out["a"] and "4111 1111" not in out["a"]
    assert "abcdefghijklmnop" not in out["b"][0]
    assert r.text("balance 4,812.37") == "balance 4,812.37"  # ordinary numbers untouched

"""End-to-end: real browser against the mock legacy target, no LLM key needed."""

import json

from artifact.schema import RiskLevel
from artifact.store import CapabilityStore
from handoff.manager import HandoffManager, OperatorCommand
from replay.engine import ReplayEngine
from tests.conftest import SECRETS


# -- discovery -> artifact ------------------------------------------------------
def test_discovery_produces_reviewable_artifact(artifact, tmp_path):
    assert artifact.name == "member_savings_balance"
    assert [s.step_id for s in artifact.steps] == ["s01", "s02", "s03", "s04", "s05", "s06"]
    assert artifact.steps[3].value == "{member_id}"  # concrete value parameterised
    assert artifact.steps[1].value == "{operator_password}"  # secret templated, not stored
    assert "letmein-demo" not in artifact.to_json()
    # discovery-run values must not survive anywhere (a raw '12345' in a wait/checkpoint would break other inputs)
    for cond in [c for s in artifact.steps for c in s.wait_for] + artifact.success_checkpoint:
        assert "12345" not in cond.value
    assert artifact.steps[5].locators[0].strategy.value == "label_adjacent"
    assert artifact.steps[5].locators[0].frame == "mainframe"  # frameset handled
    assert [o.name for o in artifact.outputs] == ["savings_balance"]
    assert any(c.kind.value == "url_contains" and "{member_id}" in c.value for c in artifact.success_checkpoint)
    # probes encoded outcome + recovery rules
    assert [o.code for o in artifact.steps[4].outcomes] == ["MEMBER_NOT_FOUND"]
    assert [r.code for r in artifact.global_recoveries] == ["SESSION_EXPIRED", "MAINTENANCE_NOTICE"]
    assert artifact.global_recoveries[0].retry_from_step == "s04"
    assert artifact.global_recoveries[1].retry_from_step is None  # dismiss-and-continue interstitial
    assert artifact.steps[4].retries == 1 and artifact.steps[3].retries == 0  # safe click retried, typing not
    # store round trip
    store = CapabilityStore(tmp_path / "caps")
    p = store.save(artifact)
    assert store.get("member_savings_balance") == artifact
    assert store.catalog()[0]["name"] == "member_savings_balance"
    assert p.name == "member_savings_balance.v1.json"


# -- replay: result taxonomy ---------------------------------------------------------
def test_replay_success_returns_typed_outputs(artifact, surface, runlog):
    res = ReplayEngine(surface, runlog("replay")).replay(artifact, {"member_id": "23456"}, SECRETS)
    assert res.status == "success", res.summary()
    assert res.outputs == {"savings_balance": "250.00"}
    assert res.llm_calls == 0
    assert res.params["operator_password"] == "[REDACTED]"
    assert [t.status for t in res.trace] == ["ok"] * 6


def test_replay_business_outcome_not_found(artifact, surface, runlog):
    res = ReplayEngine(surface, runlog("replay")).replay(artifact, {"member_id": "99999"}, SECRETS)
    assert res.status == "business_outcome"
    assert res.outcome.code == "MEMBER_NOT_FOUND" and res.outcome.at_step == "s05"
    assert "NO MEMBER FOUND" in res.outcome.observed_text
    assert res.failure is None and res.outputs == {}


def test_replay_recovers_from_session_timeout(artifact, surface, runlog, target_url):
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=session_timeout"})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "success", res.summary()
    assert res.recoveries == ["SESSION_EXPIRED"]
    assert res.outputs["savings_balance"] == "4812.37"
    statuses = [t.status for t in res.trace]
    assert "recovered" in statuses and statuses.count("ok") >= 6  # s04/s05 re-ran after recovery


def test_replay_hard_failure_http_error_has_debug_evidence(artifact, surface, runlog, target_url):
    log = runlog("replay")
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=app_error"})
    res = ReplayEngine(surface, log).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure"
    f = res.failure
    assert f.kind == "http_error_page" and f.step_id == "s05"
    assert "HTTP 500" in f.observed and "SAVINGS BALANCE" in f.expected
    assert (log.dir / "failure" / "screenshot.png").exists()
    assert "INTERNAL APPLICATION ERROR" in (log.dir / "failure" / "a11y_snapshot.txt").read_text()


def test_replay_tolerates_slow_load(artifact, surface, runlog, target_url):
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=slow_load"})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "success", res.summary()


def test_replay_dismisses_maintenance_interstitial_and_continues(artifact, surface, runlog, target_url):
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=maintenance_notice"})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "success", res.summary()
    assert res.recoveries == ["MAINTENANCE_NOTICE"]
    ids = [t.step_id for t in res.trace]
    assert ids.count("s05") == 1  # cleared the overlay, did NOT re-click INQUIRE
    assert res.outputs["savings_balance"] == "4812.37"


def test_replay_retry_is_bounded_and_safe_only(artifact, surface, runlog):
    art = artifact.model_copy(deep=True)
    art.steps[4].wait_for[0].value = "TEXT THAT NEVER APPEARS"
    art.steps[4].wait_for[0].timeout_ms = 500
    art.steps[4].retries = 2
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "checkpoint_failed" and res.failure.step_id == "s05"
    assert [t.status for t in res.trace if t.step_id == "s05"] == ["retrying"] * 3 + ["failed"]
    assert "3 waits" in res.failure.observed
    art.steps[4].risk = RiskLevel.RISKY
    res = ReplayEngine(surface, runlog("replay"), confirm_risky=True).replay(art, {"member_id": "12345"}, SECRETS)
    assert [t.status for t in res.trace if t.step_id == "s05"] == ["failed"]  # risky: never doubled


def test_replay_recovery_loop_is_bounded(artifact, surface, runlog, target_url):
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=session_timeout"}, deep=True)
    art.global_recoveries[0].actions = []  # 'clearing' does nothing -> the same interstitial keeps matching
    art.global_recoveries[0].max_attempts = 5
    res = ReplayEngine(surface, runlog("replay"), max_recoveries=2).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.recoveries == ["SESSION_EXPIRED", "SESSION_EXPIRED"]


def test_replay_recovery_action_failure_is_reported(artifact, surface, runlog, target_url):
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=session_timeout"}, deep=True)
    for loc in art.global_recoveries[0].actions[0].locators:
        loc.value = "NO SUCH BUTTON"
    art.global_recoveries[0].actions[0].timeout_ms = 800
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "recovery_failed"
    assert "SESSION_EXPIRED" in res.failure.expected


def test_replay_wall_clock_deadline(artifact, surface, runlog):
    res = ReplayEngine(surface, runlog("replay"), deadline_ms=1).replay(artifact, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "timeout"


def test_replay_unreachable_entry_is_a_clean_failure(artifact, surface, runlog):
    art = artifact.model_copy(update={"entry_url": "http://127.0.0.1:1/"})
    art.safety = art.safety.model_copy(update={"allowed_origins": ["http://127.0.0.1:1"]})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "unexpected_state" and res.failure.step_id is None


def test_unexpected_js_dialog_is_dismissed_and_logged(artifact, surface, runlog, target_url):
    surface.navigate(f"{target_url}/", 5000)
    surface.page.evaluate("setTimeout(() => confirm('DELETE EVERYTHING?'), 50)")
    surface.page.wait_for_timeout(300)
    assert surface.drain_dialogs() == ["confirm: DELETE EVERYTHING?"]
    assert surface.drain_dialogs() == []
    assert "USER ID" in surface.a11y_dump()  # page still usable after auto-dismiss


def test_replay_param_validation_before_touching_ui(artifact, surface, runlog):
    res = ReplayEngine(surface, runlog("replay")).replay(artifact, {"member_id": "abc"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "param_invalid"
    res = ReplayEngine(surface, runlog("replay")).replay(artifact, {"member_id": "1"}, {})
    assert res.failure.kind == "param_invalid" and "operator_user" in res.failure.observed


# -- safety ------------------------------------------------------------------------
def test_replay_refuses_entry_outside_allowlist(artifact, surface, runlog):
    art = artifact.model_copy(update={"entry_url": "http://evil.example/"})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "1"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "guardrail_blocked"


def test_risky_step_blocked_by_policy(artifact, surface, runlog):
    art = artifact.model_copy(deep=True)
    art.steps[4].risk = RiskLevel.RISKY
    art.safety = art.safety.model_copy(update={"risky_step_handling": "block"})
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "guardrail_blocked" and res.failure.step_id == "s05"


def test_risky_step_confirm_without_human_fails_with_pre_confirm_passing(artifact, surface, runlog):
    art = artifact.model_copy(deep=True)
    art.steps[4].risk = RiskLevel.RISKY
    res = ReplayEngine(surface, runlog("replay")).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "failure" and res.failure.kind == "guardrail_blocked"
    assert "confirmation" in res.failure.expected and res.failure.step_id == "s05"
    res = ReplayEngine(surface, runlog("replay"), confirm_risky=True).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "success"


def test_evidence_never_contains_secrets(artifact, surface, runlog):
    log = runlog("replay")
    ReplayEngine(surface, log).replay(artifact, {"member_id": "12345"}, SECRETS)
    for p in log.dir.rglob("*.json"):
        assert "letmein-demo" not in p.read_text(), p


# -- handoff -------------------------------------------------------------------------
def test_handoff_human_fixes_live_session_and_automation_resumes(artifact, surface, runlog, target_url):
    """500 on the detail frame -> intervention -> operator re-clicks MEMBER DETAIL on the *same*
    browser session -> resume from s06 -> success. Operator is a thread using the manager API
    (the FastAPI console calls exactly these methods)."""
    log = runlog("replay")

    def operator(mgr: HandoffManager, req):
        assert mgr.controller.value == "human"
        state = mgr.submit(OperatorCommand(kind="refresh"))
        assert state["ok"]
        el = next(e for e in req.perception.elements if e.name == "MEMBER DETAIL")
        r = mgr.submit(OperatorCommand(kind="act", action="click", ref=el.ref))
        assert r["ok"], r
        mgr.submit(OperatorCommand(kind="resume", notes="reloaded detail frame", resume_from_step="s06"))

    mgr = HandoffManager(surface, log, open_server=False, auto_resolver=operator, timeout_s=30)
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=app_error"})
    res = ReplayEngine(surface, log, handoff=mgr).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "success", res.summary()
    assert len(res.handoffs) == 1
    assert res.outputs["savings_balance"] == "4812.37"
    assert mgr.controller.value == "automation"
    iid = res.handoffs[0]
    resolution = json.loads((log.dir / "handoff" / iid / "resolution.json").read_text())
    assert resolution["state"] == "resolved" and resolution["resume_from_step"] == "s06"
    assert resolution["human_actions"][0]["action"] == "click"
    assert "HTTP 500" in resolution["a11y_diff"] and "SAVINGS BALANCE" in resolution["a11y_diff"]
    assert (log.dir / "handoff" / iid / "screenshot_before.png").exists()


def test_handoff_abort_is_reported_as_aborted(artifact, surface, runlog, target_url):
    log = runlog("replay")

    def operator(mgr, req):
        mgr.submit(OperatorCommand(kind="abort", notes="host is down, do not retry"))

    mgr = HandoffManager(surface, log, open_server=False, auto_resolver=operator, timeout_s=30)
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=app_error"})
    res = ReplayEngine(surface, log, handoff=mgr).replay(art, {"member_id": "12345"}, SECRETS)
    assert res.status == "aborted" and res.failure.kind == "handoff_aborted"
    assert "host is down" in res.failure.observed


def test_operator_http_console(artifact, surface, runlog, target_url):
    """The FastAPI operator surface drives the same manager."""
    import threading
    import time

    import httpx

    from tests.conftest import _free_port

    log = runlog("replay")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    checks: dict = {}

    def operator():  # Playwright sync objects are thread-bound, so replay stays on the main thread
        for _ in range(100):
            time.sleep(0.2)
            try:
                items = httpx.get(f"{base}/interventions").json()
                if items:
                    break
            except httpx.HTTPError:
                continue
        iid = items[0]["id"]
        checks["listing"] = items[0]
        st = httpx.get(f"{base}/interventions/{iid}/state").json()
        ref = next(line.split()[0] for line in st["elements"] if "MEMBER DETAIL" in line)
        checks["png"] = httpx.get(f"{base}/interventions/{iid}/screenshot.png").headers["content-type"]
        checks["html"] = httpx.get(f"{base}/interventions/{iid}").text
        checks["act"] = httpx.post(f"{base}/interventions/{iid}/act", data={"action": "click", "ref": ref}).status_code
        checks["resume"] = httpx.post(
            f"{base}/interventions/{iid}/resume", data={"decision": "resume", "resume_from_step": "s06"}
        ).status_code

    mgr = HandoffManager(surface, log, port=port, timeout_s=30)
    art = artifact.model_copy(update={"entry_url": f"{target_url}/?force_error=app_error"})
    t = threading.Thread(target=operator, daemon=True)
    t.start()
    res = ReplayEngine(surface, log, handoff=mgr).replay(art, {"member_id": "12345"}, SECRETS)
    t.join(30)
    assert res.status == "success", res.summary()
    assert checks["listing"]["step_id"] == "s05" and checks["listing"]["controller"] == "human"
    assert checks["png"] == "image/png" and "Intervention" in checks["html"]
    assert checks["act"] == 303 and checks["resume"] == 200

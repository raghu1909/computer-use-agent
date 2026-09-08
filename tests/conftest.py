import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from agent.llm import ScriptedLLM
from agent.loop import DiscoveryAgent
from artifact.schema import ActionType, SafetyPolicy
from observability.runlog import RunLog
from safety.redactor import Redactor
from surface.browser import BrowserSurface
from target_app.app import app as flask_app

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "scripted_savings_balance.json"
SECRETS = {"operator_user": "operator", "operator_password": "letmein-demo"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def target_url():
    port = _free_port()
    srv = make_server("127.0.0.1", port, flask_app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(scope="session")
def surface():
    s = BrowserSurface(headless=True)
    yield s
    s.close()


@pytest.fixture
def policy(target_url):
    return SafetyPolicy(
        allowed_origins=[target_url],
        allowed_actions=list(ActionType),
        risky_step_handling="confirm",
        blocked_text_patterns=[r"<script"],
    )


@pytest.fixture
def runlog(tmp_path):
    def make(mode="test", label=None):
        return RunLog(mode, root=tmp_path / "evidence", redactor=Redactor(SECRETS), label=label)

    return make


@pytest.fixture(scope="session")
def artifact(target_url, surface, tmp_path_factory):
    """One scripted discovery per session (with probes) -> the artifact all replay tests use."""
    root = tmp_path_factory.mktemp("evidence")
    log = RunLog("discovery", root=root, redactor=Redactor(SECRETS), label="fixture")
    policy = SafetyPolicy(allowed_origins=[target_url], allowed_actions=list(ActionType))
    agent = DiscoveryAgent(surface, ScriptedLLM.from_file(str(SCRIPT)), log, policy, max_steps=12)
    art, result = agent.run(
        "Look up member 12345 and read their current savings balance", f"{target_url}/", {"member_id": "12345"}, SECRETS
    )
    assert result.status == "success", result
    assert art is not None
    agent.probe(art, {"member_id": "99999"}, SECRETS, "not_found")
    agent.probe(art, {"member_id": "12345"}, SECRETS, "timeout", entry_url=f"{target_url}/?force_error=session_timeout")
    return art

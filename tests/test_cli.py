"""The exact README demo commands, run as subprocesses against the test target."""

import json
import os
import subprocess
import sys

from tests.conftest import ROOT, SCRIPT

ENV = {
    **os.environ,
    "PYTHONPATH": str(ROOT / "src"),
    "CAP_SECRET_operator_user": "operator",
    "CAP_SECRET_operator_password": "letmein-demo",
}


def cua(*args, cwd):
    return subprocess.run([sys.executable, "-m", "cli", *args], cwd=cwd, env=ENV, capture_output=True, text=True)


def test_cli_discover_then_replay_then_invoke(target_url, tmp_path):
    common = ["--evidence", str(tmp_path / "evidence"), "--out-dir", str(tmp_path / "caps"), "--no-handoff"]
    r = cua(
        "discover",
        "--goal",
        "Look up member 12345 and read their current savings balance",
        "--entry",
        f"{target_url}/",
        "--params",
        '{"member_id":"12345"}',
        "--secret-names",
        "operator_user",
        "operator_password",
        "--llm",
        f"scripted:{SCRIPT}",
        "--label",
        "cli",
        *common,
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    art = tmp_path / "caps" / "member_savings_balance.v1.json"
    assert art.exists() and "letmein-demo" not in art.read_text()
    ev = next((tmp_path / "evidence").glob("*_discovery_cli"))
    assert (ev / "run_meta.json").exists() and (ev / "artifact.json").exists()
    assert "letmein-demo" not in "".join(p.read_text() for p in ev.rglob("*.json"))

    r = cua("replay", "--artifact", str(art), "--params", '{"member_id":"23456"}', *common, cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert '"savings_balance": "250.00"' in r.stdout and "SUCCESS" in r.stdout

    r = cua(
        "replay",
        "--artifact",
        str(art),
        "--params",
        '{"member_id":"12345"}',
        "--entry",
        f"{target_url}/?force_error=app_error",
        *common,
        cwd=tmp_path,
    )
    assert r.returncode == 2 and "http_error_page" in r.stdout

    r = cua("catalog", "--out-dir", str(tmp_path / "caps"), cwd=tmp_path)
    cat = json.loads(r.stdout)
    assert cat[0]["name"] == "member_savings_balance" and "member_id" in cat[0]["input_schema"]["properties"]

    r = cua("invoke", "member_savings_balance", "--args", '{"member_id":"12345"}', *common, cwd=tmp_path)
    assert r.returncode == 0 and '"savings_balance": "4812.37"' in r.stdout

"""Command line entry points.

cua discover  --goal ... --entry URL --params JSON [--secrets JSON] [--llm anthropic|scripted:FILE] [--probe ...]
cua replay    --artifact FILE --params JSON [--secrets-env] [--headed] [--no-handoff]
cua catalog   [--dir capabilities]              agent-facing list of callable capabilities
cua invoke    NAME --args JSON                   invoke a capability by name (what an agent would call)
cua target-app                                   run the mock legacy banking UI
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from artifact.schema import ActionType, SafetyPolicy
from artifact.store import CapabilityStore


def _policy(entry: str, allow_risky: str) -> SafetyPolicy:
    from urllib.parse import urlparse

    u = urlparse(entry)
    return SafetyPolicy(
        allowed_origins=[f"{u.scheme}://{u.netloc}"],
        allowed_path_prefixes=["/"],
        allowed_actions=[a for a in ActionType],
        risky_step_handling=allow_risky,  # type: ignore[arg-type]
        blocked_text_patterns=[r"\bDROP\s+TABLE\b", r"<script"],
    )


def _secrets_from_env(names: list[str]) -> dict[str, str]:
    """CAP_SECRET_<name> env vars -> {name: value}. Secrets never travel through argv/history."""
    out = {}
    for n in names:
        v = os.environ.get(f"CAP_SECRET_{n}")
        if v:
            out[n] = v
    return out


def _make_llm(spec: str):
    from agent.llm import AnthropicLLM, ScriptedLLM

    if spec.startswith("scripted:"):
        return ScriptedLLM.from_file(spec.split(":", 1)[1])
    if spec == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set (or use --llm scripted:FILE)")
        return AnthropicLLM(model=os.environ.get("ANTHROPIC_MODEL"))
    sys.exit(f"unknown --llm {spec}")


def cmd_discover(a: argparse.Namespace) -> int:
    from agent.loop import DiscoveryAgent
    from handoff.manager import HandoffManager
    from observability.runlog import RunLog
    from safety.redactor import Redactor
    from surface.browser import BrowserSurface

    params = json.loads(a.params) if a.params else {}
    secrets = json.loads(a.secrets) if a.secrets else {}
    secrets.update(_secrets_from_env(a.secret_names or []))
    llm = _make_llm(a.llm)
    log = RunLog("discovery", root=a.evidence, redactor=Redactor(secrets), label=a.label)
    policy = _policy(a.entry, a.risky)
    surface = BrowserSurface(headless=not a.headed, cdp_port=a.cdp_port)
    handoff = None if a.no_handoff else HandoffManager(surface, log, port=a.operator_port, timeout_s=a.handoff_timeout)
    agent = DiscoveryAgent(surface, llm, log, policy, handoff=handoff, max_steps=a.max_steps)
    try:
        artifact, result = agent.run(a.goal, a.entry, params, secrets, app_family=a.app_family)
        if artifact:
            for spec in a.probe or []:
                label, _, rest = spec.partition("=")
                probe = json.loads(rest)
                rep = agent.probe(artifact, probe.get("params", params), secrets, label, probe.get("entry_url"))
                result.probes.append(rep)
                print(
                    f"probe {label}: {rep.get('first_replay')} -> encoded {rep.get('encoded')} "
                    f"-> verified {rep.get('verified')}"
                )
            if a.name:
                artifact.name = a.name
            if a.approve:
                artifact.status = "approved"
            path = CapabilityStore(a.out_dir).save(artifact, a.output)
            result.artifact_path = str(path)
            log.artifact_file("artifact.json", json.loads(artifact.to_json()))
        result.llm_calls = agent.llm_calls
        log.finish(result, status=result.status, llm_usage=getattr(llm, "usage", None))
        print(json.dumps(result.model_dump(), indent=2))
        print(f"evidence: {log.dir}")
        return 0 if result.status == "success" else 1
    finally:
        surface.close()


def cmd_replay(a: argparse.Namespace) -> int:
    from handoff.manager import HandoffManager
    from observability.runlog import RunLog
    from replay.engine import ReplayEngine
    from safety.redactor import Redactor
    from surface.browser import BrowserSurface

    store = CapabilityStore(a.out_dir)
    artifact = store.load(a.artifact) if a.artifact else store.get(a.name)
    params = json.loads(a.params) if a.params else {}
    secrets = json.loads(a.secrets) if a.secrets else {}
    secrets.update(_secrets_from_env([p.name for p in artifact.parameters if p.sensitive]))
    if a.entry:
        artifact = artifact.model_copy(update={"entry_url": a.entry})
    repeat = getattr(a, "repeat", 1)
    statuses: list[str] = []
    rc = 0
    for _ in range(repeat):
        log = RunLog("replay", root=a.evidence, redactor=Redactor(secrets), label=a.label)
        surface = BrowserSurface(headless=not a.headed, cdp_port=a.cdp_port)
        handoff = (
            None if a.no_handoff else HandoffManager(surface, log, port=a.operator_port, timeout_s=a.handoff_timeout)
        )
        engine = ReplayEngine(
            surface,
            log,
            handoff=handoff,
            confirm_risky=a.confirm_risky,
            require_approved=a.require_approved,
            deadline_ms=getattr(a, "deadline_ms", None),
        )
        try:
            res = engine.replay(artifact, params, secrets)
            print(json.dumps(res.model_dump(mode="json", exclude={"trace"}), indent=2))
            print(res.summary())
            print(f"evidence: {log.dir}")
            statuses.append(res.status)
            rc = {"success": 0, "business_outcome": 0, "failure": 2, "aborted": 3}[res.status]
        finally:
            surface.close()
    if repeat > 1:
        ok = sum(s in ("success", "business_outcome") for s in statuses)
        print(f"stability: {ok}/{repeat} runs terminal-ok ({100 * ok // repeat}%) statuses={statuses}")
        rc = 0 if ok == repeat else 2
    return rc


def cmd_catalog(a: argparse.Namespace) -> int:
    print(json.dumps(CapabilityStore(a.out_dir).catalog(), indent=2))
    return 0


def cmd_invoke(a: argparse.Namespace) -> int:
    a.artifact = None
    a.params = a.args
    return cmd_replay(a)


def cmd_target_app(a: argparse.Namespace) -> int:
    os.environ["TARGET_APP_PORT"] = str(a.port)
    from target_app.app import main

    main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--evidence", default="evidence")
        sp.add_argument("--out-dir", default="capabilities")
        sp.add_argument("--label", default=None, help="suffix for the evidence run directory")
        sp.add_argument("--headed", action="store_true", help="show the browser (lets an operator use it directly)")
        sp.add_argument("--cdp-port", type=int, default=None, help="expose Chrome DevTools Protocol on this port")
        sp.add_argument("--no-handoff", action="store_true", help="fail instead of escalating to a human")
        sp.add_argument("--operator-port", type=int, default=8765)
        sp.add_argument("--handoff-timeout", type=float, default=1800)
        sp.add_argument("--secrets", default=None, help="JSON of sensitive params (prefer CAP_SECRET_<name> env vars)")

    d = sub.add_parser("discover", help="LLM-driven discovery run; writes a capability artifact")
    common(d)
    d.add_argument("--goal", required=True)
    d.add_argument("--entry", required=True, help="entry URL")
    d.add_argument("--params", default=None, help="JSON of caller-supplied parameters used in this run")
    d.add_argument("--secret-names", nargs="*", help="names of secret params read from CAP_SECRET_<name>")
    d.add_argument("--llm", default="anthropic", help="anthropic | scripted:FILE")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--risky", default="confirm", choices=["execute", "confirm", "block"])
    d.add_argument(
        "--probe",
        action="append",
        metavar='LABEL={"params":{...},"entry_url":"..."}',
        help="after success, replay with these inputs and encode the exceptional state found",
    )
    d.add_argument("--approve", action="store_true", help="mark the artifact approved for unattended replay")
    d.add_argument("--app-family", default=None)
    d.add_argument("--name", default=None, help="pin the capability name (default: chosen by the model)")
    d.add_argument("--output", default=None, help="artifact path (default capabilities/<name>.v1.json)")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of an artifact (no LLM)")
    common(r)
    r.add_argument("--artifact", default=None)
    r.add_argument("--name", default=None, help="capability name (latest version) instead of --artifact")
    r.add_argument("--params", default=None, help="JSON input parameters")
    r.add_argument("--entry", default=None, help="override entry URL (e.g. to inject ?force_error=)")
    r.add_argument("--confirm-risky", action="store_true", help="caller pre-confirms risky steps")
    r.add_argument("--require-approved", action="store_true")
    r.add_argument("--repeat", type=int, default=1, help="replay N times and report a stability signal")
    r.add_argument("--deadline-ms", type=int, default=None, help="override the artifact's wall-clock budget")
    r.set_defaults(fn=cmd_replay)

    c = sub.add_parser("catalog", help="list capabilities as agent-callable tools")
    c.add_argument("--out-dir", default="capabilities")
    c.set_defaults(fn=cmd_catalog)

    i = sub.add_parser("invoke", help="invoke a capability by name with typed args (agent-facing)")
    common(i)
    i.add_argument("name")
    i.add_argument("--args", required=True, help="JSON arguments")
    i.add_argument("--entry", default=None)
    i.add_argument("--confirm-risky", action="store_true")
    i.add_argument("--require-approved", action="store_true")
    i.set_defaults(fn=cmd_invoke)

    t = sub.add_parser("target-app", help="run the mock legacy banking UI")
    t.add_argument("--port", type=int, default=5000)
    t.set_defaults(fn=cmd_target_app)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

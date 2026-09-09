# computer-use-agent

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

An LLM drives a legacy back-office UI once to accomplish a natural-language goal. The successful run is
recorded as a typed, versioned **capability artifact**. From then on the capability is invoked by
**deterministic replay** — no model in the loop — with typed inputs, typed outputs, an explicit error
taxonomy (business outcome / recoverable / hard failure), safety guardrails, and a **human handoff** that
takes over the *same live browser session* when automation is stuck.

Design write-up: [REPORT.md](REPORT.md). Evidence of real runs: [evidence/](evidence/).

```
goal ──► DiscoveryAgent (LLM: observe→decide→act) ──► Recorder ──► capabilities/<name>.vN.json
                │                                                          │
                ▼                                                          ▼
          Surface (Playwright)  ◄────────────────  ReplayEngine (no LLM) ──► ReplayResult
                ▲                                                  │  success | business_outcome | failure | aborted
                └── HandoffManager + operator console  ◄───────────┘  (stuck → human acts on the live session → resume)
```

## Setup

Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Configuration (all optional; copy `.env.example`):

| variable | purpose |
|---|---|
| `ANTHROPIC_API_KEY` | needed only for real LLM discovery (`--llm anthropic`) |
| `ANTHROPIC_MODEL` | model id, default `claude-sonnet-4-5` |
| `CAP_SECRET_<param>` | values for `sensitive` parameters (e.g. `CAP_SECRET_operator_password`). Never written to artifacts or logs |
| `TARGET_URL` | target app base URL for the demo scripts, default `http://localhost:5000` |

No live services are required. The target is a local mock and the discovery loop has a **scripted LLM
mode** (`--llm scripted:FILE`) that exercises the identical perceive→decide→act→record code path without a
key. The test suite uses it. The checked-in `evidence/` comes from a **genuine `claude-sonnet-4-5` run**
(`evidence/*_discovery_anthropic`: 12 model calls, ~34k input tokens; the model's reasoning is in `steps/NNN.json`)
plus the probes/replays/handoff executed against the artifact it produced; a scripted discovery run is kept
alongside for offline comparison.

## Target application

`src/target_app` is a deliberately hostile mock of a core-banking console: framesets, table layout,
`<font>` tags, no `<label for>`, no ids or test ids. Fictional data only; the demo login is
`operator / letmein-demo`. Any page accepts `?force_error=<code>` to inject a runtime condition once:
`session_timeout`, `maintenance_notice`, `permission_denied`, `slow_load`, `app_error`. Unknown member
numbers yield a "NO MEMBER FOUND" business outcome.

```bash
cua target-app --port 5000        # terminal 1, leave running
```

## Demo path

Everything below is in [`scripts/demo.sh`](scripts/demo.sh) (`scripts/demo.sh anthropic` for the real model).

```bash
export CAP_SECRET_operator_user=operator CAP_SECRET_operator_password=letmein-demo
```

**1. Discover** — the LLM drives the UI to the goal, then probes two exceptional inputs so the artifact
learns a business outcome (`MEMBER_NOT_FOUND`) and a recovery (`SESSION_EXPIRED` → click CONTINUE SESSION,
retry from s04). Writes `capabilities/member_savings_balance.v1.json` and `evidence/<ts>_discovery_*`.

```bash
cua discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --entry http://localhost:5000/ \
  --params '{"member_id":"12345"}' \
  --secret-names operator_user operator_password \
  --llm anthropic \
  --probe 'not_found={"params":{"member_id":"99999"}}' \
  --probe 'session_timeout={"entry_url":"http://localhost:5000/?force_error=session_timeout"}' \
  --probe 'maintenance={"entry_url":"http://localhost:5000/?force_error=maintenance_notice"}' \
  --name member_savings_balance --approve --label anthropic
```

Offline (no key, deterministic): replace `--llm anthropic` with `--llm scripted:examples/scripted_savings_balance.json`.
`scripts/demo.sh [anthropic]` runs the whole discover → replay → error-replay sequence.

**2. Replay** — deterministic, zero LLM calls, new input:

```bash
cua replay --artifact capabilities/member_savings_balance.v1.json --params '{"member_id":"23456"}' --require-approved
#   SUCCESS outputs={'savings_balance': '250.00'}          exit 0
cua replay --artifact capabilities/member_savings_balance.v1.json --params '{"member_id":"99999"}'
#   OUTCOME MEMBER_NOT_FOUND: No member exists with that number (at s05)          exit 0
cua replay --artifact ... --params '{"member_id":"12345"}' --entry 'http://localhost:5000/?force_error=session_timeout'
#   SUCCESS (recoveries=['SESSION_EXPIRED'])
cua replay --artifact ... --params '{"member_id":"12345"}' --entry 'http://localhost:5000/?force_error=maintenance_notice'
#   SUCCESS (recoveries=['MAINTENANCE_NOTICE'])   overlay dismissed, step not re-clicked
cua replay --artifact ... --params '{"member_id":"23456"}' --repeat 5
#   stability: 5/5 runs terminal-ok (100%)
cua replay --artifact ... --params '{"member_id":"12345"}' --entry 'http://localhost:5000/?force_error=app_error' --no-handoff
#   FAILURE at s05 [http_error_page] expected: text_present 'SAVINGS BALANCE' | observed: 'HTTP 500'   exit 2
#   evidence/<run>/failure/{screenshot.png,a11y_snapshot.txt,detail.json}
```

**3. Handoff** — same failure, but an operator is available. Automation pauses, prints the console URL,
the human fixes the live session, resumes; the run completes with the output.

```bash
scripts/demo_handoff.sh                    # scripted operator answers over the console's HTTP API
OPERATOR=human scripts/demo_handoff.sh --headed   # you act: open http://127.0.0.1:8765/interventions/<id>
```

**4. Agent-facing catalog / invoke** (stretch goal):

```bash
cua catalog                                                   # tool schemas for every saved capability
cua invoke member_savings_balance --args '{"member_id":"12345"}'
```

Exit codes: `0` success or business outcome, `2` failure, `3` aborted by operator.

## Evidence layout

```
evidence/<ts>_<mode>_<label>/
  run_meta.json          who/what/when, params (secrets redacted), llm usage
  steps/NNN.json|png     one record + screenshot per action (decision & reasoning in discovery; locator attempts in replay)
  events.json            recoveries, outcomes, handoff lifecycle, drift check
  final_state.json       DiscoveryResult / ReplayResult
  failure/               screenshot + accessibility snapshot + detail on hard failure
  handoff/<id>/          request.json, screenshots before/after, resolution.json (human actions, a11y diff)
  artifact.json          (discovery) the emitted capability
```

What is checked in: `*_discovery_anthropic` (the real model run) → `*_probe_*` / `*_probe_verify_*` (adversarial
probes and the verification replays that gate each encoded rule) → `*_replay_{happy,not_found,session_timeout,
maintenance,app_error,bad_input,stability}` → `*_replay_handoff` (injected 500 → operator fixes the live session
→ resume). `*_discovery_scripted` is the same flow driven by the fixture LLM.

## Tests & checks

```bash
pytest -q                 # unit + end-to-end (real browser against the mock app; ~2.5 min)
ruff check src tests && ruff format --check src tests && mypy src
```

## Layout

```
src/target_app    mock legacy banking UI (Flask) with error injection
src/surface       Surface protocol + BrowserSurface (Playwright; perceive / resolve / act / check)
src/agent         DiscoveryAgent loop, prompts, AnthropicLLM + ScriptedLLM
src/artifact      CapabilityArtifact schema (pydantic), Recorder (transcript → artifact), JSON store
src/replay        ReplayEngine (locator chain, waits, outcomes, recoveries, checkpoint), ReplayResult
src/safety        SafetyPolicy guardrails, risk classification, Redactor
src/handoff       HandoffManager (control transfer) + FastAPI operator console
src/observability RunLog evidence writer
src/cli.py        cua discover | replay | catalog | invoke | target-app
```

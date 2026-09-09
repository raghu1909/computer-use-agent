# REPORT

## 1. Architecture

One Python process, sync Playwright, five boundaries:

```
Surface (perceive/resolve/act/check)     ← the only code that knows it is a browser
   ▲              ▲
DiscoveryAgent    ReplayEngine            ← both consume Surface; only the agent imports an LLM
   │  Recorder    │  ReplayResult
   ▼              │
CapabilityArtifact (pydantic, JSON)  ─────┘   ← the contract between them
HandoffManager  ← pauses either side, lends the *same* Surface to a human, records, resumes
RunLog          ← every step/decision/screenshot/failure, redacted, under evidence/<run>/
```

**Discovery** (`agent/loop.py`): `perceive()` returns a per-frame accessibility snapshot plus a numbered
list of actionable elements (`e12 [control] textbox "MEMBER NUMBER"`) and a screenshot. The model gets text
+ image and returns one structured decision (tool-use, schema-validated): `click/type/select/press/navigate/
extract/done/fail/ask_human` with a `ref`, an `expect`ed text on the next screen, and a risk flag. Every
decision passes the guardrails before it touches the UI. Stopping conditions: goal met, `fail`, max steps,
or *stuck* (screen fingerprint unchanged after N actions) which escalates to a human. After success a
single **distill** call names the capability, describes params/outputs/steps and flags risky steps; then
**probes** replay the fresh artifact against adversarial inputs (unknown member, injected timeout) and, on
the first failure, ask the model to classify the screen once — the answer is encoded as an `OutcomeRule` or
`RecoveryRule` and verified by another replay. This is how the artifact learns the exceptional states
without a human authoring them.

**Recorder** (`artifact/recorder.py`) turns the transcript into the artifact: concrete values become
`{param}` templates (caller declares params up front — the recorder cannot guess that "12345" is a member
id), each element becomes an ordered locator chain, URL changes and confirmed `expect` texts become
`wait_for` conditions, extracts become typed outputs, the final screen becomes the checkpoint.

**Replay** (`replay/engine.py`) is a small interpreter over the artifact: validate params → guardrails →
resolve locator chain → act → wait_for → outcome/recovery rules → next step → final checkpoint → drift
check. Zero model calls (`ReplayResult.llm_calls` is asserted `0` in tests).

Key trade-offs: **sync over async** (determinism and testability; concurrency belongs at the job layer, not
in the executor). **Accessibility tree + semantic locators, not screenshot coordinates** — coordinates are
the least stable thing about a legacy UI, while role/name/label-adjacency are what a human operator uses;
screenshots are still given to the model and kept as evidence. **JSON files, not a DB** for capabilities
and evidence; the store interface is three methods. **Real target app instead of a public site**: it let me
inject exactly the runtime conditions the brief cares about (timeout, 500, slow load, permission, not-found)
and use no real data.

## 2. Artifact schema

`artifact/schema.py`, `schema_version: "1.0"`. Top level = a callable contract; `tool_schema()` renders it
as a function-calling tool (`cua catalog`).

```
CapabilityArtifact
  name, version:int, status: draft|approved|deprecated, description, entry_url
  parameters: [ParamDef{name, type: string|integer|decimal|date|enum, required, pattern, choices, example, sensitive}]
  outputs:    [OutputDef{name, type, extracted_by: step_id, postprocess: strip|money|digits}]
  steps:      [StepDef{step_id, action, description, locators:[Locator], value:"{param}", output, risk,
                       wait_for:[Condition], outcomes:[OutcomeRule], recoveries:[RecoveryRule], timeout_ms, retries}]
  success_checkpoint: [Condition]
  max_duration_ms                           # wall-clock budget for one invocation
  global_outcomes / global_recoveries      # apply to every step (e.g. SESSION_EXPIRED interstitial)
  safety: SafetyPolicy                      # allowlist travels with the capability
  meta: {model, discovery_run_id, source_goal, app_family, app_version, tenant, checkpoint_fingerprints}

Locator     {strategy: role|label|label_adjacent|placeholder|text|css|xpath, value, name?, frame?, exact, rationale}
Condition   {kind: url_contains|text_present|text_absent|element_visible|element_hidden|http_error_page, value, timeout_ms}
OutcomeRule {code, when: Condition, message, capture_text}       # legitimate business result → stop, report
RecoveryRule{code, when: Condition, actions:[StepDef], retry_from_step, max_attempts}   # clear & continue
```

Why this shape:
- **Locators are an ordered chain, flat, with a `rationale`.** The replay tries them in order and records
  which one matched. The rationale is for the reviewer ("form field name is part of the server contract";
  "structural fallback; brittle, last resort") and encodes the robustness reasoning the brief asks for.
  `label_adjacent` exists specifically for legacy table forms with no `<label>`: "the control in the cell
  next to the cell that says PASSWORD" is how the operator thinks, and it survives re-skins.
- **Conditions are one type used everywhere** — waits, checkpoints, outcome triggers, recovery triggers —
  so the replay has a single `check`/`wait_for` seam and a single vocabulary for reviewers.
- **Outcomes and recoveries are first-class and separate.** Conflating "no such member" with a crash is the
  mistake the brief warns about; here they are different rule types with different result statuses.
- **Risk is per step, not per action type.** Clicking INQUIRE and clicking POST TRANSFER are both clicks.
- **Sensitive params are declared, never valued.** The artifact contains `{operator_password}`; the value
  comes from `CAP_SECRET_*` at invocation and is redacted from every log.
- **Versioned + status-gated.** `vN` files are immutable; `--require-approved` blocks unattended replay of
  drafts. Pydantic validators reject duplicate step ids, outputs pointing at non-extract steps, unknown
  `retry_from_step`, unresolved `{templates}`.
- Deliberately *not* in the artifact: the model transcript (evidence only), screenshots, tenant-specific
  branding, timing data.

## 3. Determinism & error handling

Determinism = no decision at replay time is made by anything other than the artifact and the observed page:
1. **Inputs** validated (type, pattern, required) before the browser is touched → `param_invalid`.
2. **Targeting**: locator chain in recorded order, scoped to the recorded frame; the first that resolves to
   exactly one visible element wins; every attempt is logged. Unresolved → `locator_unresolved` with the full
   attempt list.
3. **Waits are explicit, never sleeps**: each step's `wait_for` (URL/text/visibility) with a timeout, so
   slow loads (`slow_load`, ~4 s) pass and hangs fail fast with "expected X, observed Y".
4. **Checkpoints** after every navigating step and at the end; the final accessibility *shape* (quoted
   content stripped) is fingerprinted at discovery and compared at replay — mismatch is logged as
   `drift_check.drift=true` (secondary signal, does not fail the run since the UI is stable by design).

Runtime conditions are checked on every step *after* the action and *before* declaring failure:

| class | detected by | replay does | result |
|---|---|---|---|
| business outcome | `OutcomeRule.when` (e.g. text "NO MEMBER FOUND") | stop cleanly | `status=business_outcome, outcome={code, message, at_step, observed_text}` |
| recoverable | `RecoveryRule.when` (e.g. "SESSION EXPIRED", "SCHEDULED MAINTENANCE") | run `actions`; then either jump to `retry_from_step`, or — if the failed step's post-conditions now hold — just continue (a dismissed overlay must not re-click INQUIRE). Bounded by `max_attempts` per rule and `max_recoveries` per run | continues |
| transient | a **safe** step's post-condition timed out, no error page | wait one more budget for the post-conditions (`StepDef.retries`, recorder sets 1 on safe navigating steps); locator miss → re-run the step. Risky steps are never retried: a doubled click could post twice | continues or fails |
| unexpected dialog | Playwright `dialog` event (alert/confirm/prompt) | auto-**dismiss** (never accept — accepting a `confirm()` could commit), log `unexpected_dialog` in the step trace | continues |
| hard failure | `http_error_page` signature, locator chain exhausted, wait/checkpoint timeout, recovery actions themselves failing (`recovery_failed`), wall-clock budget exceeded (`timeout`), unreachable entry URL, guardrail block, exception | snapshot screenshot + a11y tree + detail, stop | `status=failure, failure={step_id, kind, expected, observed, locator_attempts, evidence_dir}` |
| aborted | operator chose abort during a handoff | stop | `status=aborted` |

Every loop is bounded: per-rule `max_attempts`, per-run `max_recoveries` (6) and `max_handoffs` (3),
per-step `retries`, and `max_duration_ms` (120 s) — a replay can never spin. Multi-run stability:
`cua replay --repeat N` prints `stability: k/N` so approval can be gated on clean repeats.

Evidence for each is in `evidence/`: `*_replay_not_found`, `*_replay_session_timeout`, `*_replay_maintenance`,
`*_replay_app_error` (with `failure/`), `*_replay_bad_input`, `*_replay_handoff`. Exit codes map to the classes so a calling
agent can branch without parsing.

Drift (secondary): the locator chain degrades gracefully (semantic → attribute → structural), each fallback
hit is logged so a reviewer can see an artifact "wearing out", and the fingerprint check flags shape change.

## 4. Heterogeneity & multi-tenant

**Surface seam.** `surface/base.py` defines `Surface` = `perceive() → Perception{url, frames[a11y], elements[ElementInfo], screenshot, fingerprint}`, `resolve(locators) → handle`, `act(action, handle, value)`, `check/wait_for(Condition)`. The artifact never references DOM, CSS or coordinates except as *one strategy inside a locator chain*; the agent and the replay engine import nothing from Playwright.
- *Legacy web* is already the implemented case: framesets (`Locator.frame`), table forms (`label_adjacent`), no ids. A server-rendered app with popups would add `window` next to `frame`.
- *Desktop* = a `DesktopSurface` over the OS accessibility API (UIA / AT-SPI / AX): `role`, `label`, `label_adjacent` (geometry from the a11y tree) and `text` strategies map 1:1; `css/xpath` become `automation_id` / `a11y_path`; `url_contains` conditions become `window_title_contains`. `Condition.kind` and `LocatorStrategy` are enums for exactly this extension, and a step's chain can legitimately hold strategies for more than one surface kind.
- Screenshot+coordinates is a *last-resort strategy* (`bbox` is already captured in `ElementInfo`), not the primary one.

**Multi-tenant reuse.** `meta.app_family` / `app_version` / `tenant` model "same vendor product, configured differently":
- Record once per **app family** (`tenant=None`, the base artifact). Locator chains are semantic-first so branding/CSS/layout changes don't matter; `{param}` templating and route canonicalization (`/members/{member_id}`) keep values out of the artifact.
- **Per-tenant overlays**, not copies: a small `TenantOverride{tenant, app_version, step_id → {locators?, value?, wait_for?}, extra recoveries}` merged at load time. Most tenants need none; a tenant with an extra MFA interstitial adds one `RecoveryRule`; a tenant with a renamed button overrides one step's chain.
- **Drift detection & management**: replay logs which fallback in the chain matched and the checkpoint fingerprint per tenant/version. Trend those signals (this tenant now hits fallback #2 on s05; fingerprint changed after vendor release X) → mark the base artifact `needs_review` for that `app_version`, run a *bounded* re-discovery for that step only, and promote the result to an overlay or a new base version. Nothing is re-recorded from scratch per tenant.

## 5. Escalation & handoff

**Detecting stuck.** Discovery: the model says `ask_human`, or the screen fingerprint hasn't changed after
`stuck_after=3` actions, or a risky decision needs confirmation under `risky=confirm`. Replay: hard
failure (500 page, locator chain exhausted, checkpoint timeout, `max_attempts` exceeded), or a `risk=risky`
step under `confirm`. Both routes call `HandoffManager.request_intervention()`.

**Control-transfer model.** `Controller ∈ {AUTOMATION, HUMAN}` is a single owned state on the manager; the
automation thread flips it to HUMAN, writes `InterventionRequest{capability, goal/mode, step_id, url,
reason, expected vs observed, screenshot, element list}`, prints the console URL, and *blocks* servicing a
command queue. The **operator surface** (`handoff/server.py`, FastAPI in a thread) is intentionally minimal
but real: screenshot, current elements with refs, an action form (click/type/select/extract by ref), and
resume/abort with notes and an optional `resume_from_step`. Operator commands are executed *by the
automation thread on the same Playwright page* (Playwright objects are thread-bound, which conveniently
enforces single-controller), so the human literally drives the session the run was using — same cookies,
same frames, same state. With `--headed` the operator may also just use the visible browser.

**Hand-back.** `resume` flips `Controller` to AUTOMATION and the engine continues at `resume_from_step`
(default: the step after the one that failed; the operator picks earlier if they navigated away). `abort`
yields `status=aborted`. Everything is preserved in `evidence/<run>/handoff/<id>/`: request, before/after
screenshots, every human action (`click e1 … url_after`), notes, and a unified diff of the accessibility
tree before vs after — the run `evidence/*_replay_handoff` shows a 500 page turning into the member detail
and the replay finishing with the balance. Operator actions go through the same `Surface.act` and are
logged with the same redaction as automation.

What's mocked: a co-browsing/VNC view (the console works off screenshots + refs), auth on the console, a
queue/routing layer for many operators. The seam (`request_intervention` / `wait_for_human` / `submit`)
would not change.

## 6. Safety

`SafetyPolicy` travels inside the artifact and is enforced by `Guardrails.check()` before every action in
both discovery and replay: **allowed origins + path prefixes** (current URL *and* navigation targets; the
`evil.example` test shows a `guardrail_blocked` result before any action), **allowed action types**,
**blocked value patterns** (e.g. `<script`), and **risk handling**. Risk is per step: recorder heuristics
flag SUBMIT/POST/TRANSFER/DELETE-style controls, the model flags its own decisions, the distill step
re-labels, and a reviewer edits the JSON. `risky_step_handling ∈ {execute, confirm, block}`: default
`confirm`, where replay either has a caller pre-confirmation (`--confirm-risky`) or raises a handoff so a
human confirms *that specific step* on the live screen; no operator ⇒ blocked. Reads (`extract`) are
always safe; irreversibility is decided by what the control does, not by the action type.

**Data.** Sensitive params are declared `sensitive`, supplied via `CAP_SECRET_*`, templated in the
artifact, and replaced with `[REDACTED:name]` by the `Redactor` on *every* write path (step records,
events, screenshots' JSON siblings, results, handoff records). The redactor also masks card-like numbers,
SSNs, routing numbers, bearer tokens and API-key shapes. Tests assert no secret appears in any JSON under
the evidence tree or the artifact. The model receives the redacted transcript for distillation, never
secret values; it does see the live page during discovery, which is inherent to computer use.

**Limits.** Screenshots are stored unredacted (a real deployment would mask regions by a11y bbox or store
them encrypted with short retention). The allowlist is URL-based; a desktop surface needs a window/process
allowlist. Risk classification is heuristic + model + reviewer, not proven; that is why `confirm` is the
default and approval gating exists. The operator console has no authentication.

## 7. Cuts

Cut deliberately (seam exists, documented above): desktop `Surface`; tenant overlay loader (schema fields
present, merge not implemented); real co-browsing and console auth; artifact DB (JSON files); async/queued
execution; screenshot redaction; `permission_denied` is injectable in the mock but not exercised by the
read-only capability (it would be a `business_outcome` on a sub-account-opening capability).

Cut for time: LLM-assisted single-step recovery on replay failure (would sit exactly where the handoff is
raised, bounded to one step, policy-checked, recorded as evidence); page-object code generation from the
artifact (the schema has everything needed); network-level retry/backoff (only the UI-level `retries` exists).

Next, in order: (1) tenant overlays + drift trending from the fallback/fingerprint signals — this is where
the multi-tenant value is; (2) bounded LLM recovery as a `RecoveryRule` of kind `assisted`; (3) `--repeat`
stability feeding `status` transitions automatically (draft → approved after N clean replays); (4) `DesktopSurface` on
AT-SPI/UIA to prove the seam with a second surface kind.

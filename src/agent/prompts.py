"""Prompts for the discovery agent. Kept as plain strings so reviewers can read
exactly what the model is told."""

from __future__ import annotations

STEP_SYSTEM = """You are the hands of a back-office automation agent operating a legacy banking application through its UI.
You see the page as an accessibility tree (what a screen reader sees), a numbered list of addressable elements, and a screenshot.
Each turn you choose exactly ONE primitive action. Rules:
- Only act on elements listed by ref (e.g. e7). Never invent refs.
- Use the provided PARAMETERS verbatim when typing values that come from the caller (member IDs, amounts). Never make up data.
- Prefer the most direct path. Do not sign off, do not open unrelated menus.
- Reading a value for the caller is done with action "extract" on the element that holds the value, giving it an output_name in snake_case.
- Mark an action "risky" if it commits or changes institution state (opening an account, posting, transferring, deleting). Reading and searching are safe.
- When the goal is fully met (all required values extracted / the required screen is reached), respond with action "done".
- If the goal cannot be achieved from the current screen (e.g. the record does not exist, permission denied, unexpected error), respond with "fail" and explain.
- If you are unsure and a human must decide, respond with "ask_human".
- In "expect", give a short distinctive text you expect to see on the next screen if the action works (used as a checkpoint).
Be terse in reasoning (one sentence)."""


def render_step(goal: str, perception_text: str, elements_text: str, history: list[str], params: dict[str, str]) -> str:
    hist = "\n".join(history[-12:]) or "(none yet)"
    prm = "\n".join(f"- {k} = {v}" for k, v in params.items()) or "(none)"
    return (
        f"GOAL: {goal}\n\nPARAMETERS (caller-supplied, use verbatim):\n{prm}\n\n"
        f"HISTORY (what you already did):\n{hist}\n\n"
        f"CURRENT SCREEN (accessibility tree per frame):\n{perception_text}\n\n"
        f"ADDRESSABLE ELEMENTS:\n{elements_text}\n\nChoose the next action."
    )


CLASSIFY_SYSTEM = """You are reviewing a screen where a recorded UI automation stopped unexpectedly. Classify the state:
- business_outcome: a legitimate result the caller must be told about (record not found, validation error, permission denied). Terminal for this invocation.
- recoverable: a known interstitial or transient the automation can clear and then continue (session expired -> press continue; a notice to acknowledge; a slow load). Give clear_ref (the element to click) and retry_from_step (which recorded step to resume from; the step that was in progress is usually right when the interstitial replaced the form; use the step where typed input must be re-entered: if the interstitial replaced a form, resume from the FIRST step that typed into that form, not the click that submitted it).
- hard_failure: the application is broken or in an unknown state (HTTP 500, blank page). Not something to encode.
Return a SHORT_UPPER_SNAKE code, and match_text: the most stable operator-facing phrase that identifies this state (avoid variable parts like IDs or amounts)."""


def render_classify(goal: str, step_desc: str, perception_text: str, elements_text: str, step_ids: list[str]) -> str:
    return (
        f"GOAL of the capability: {goal}\nSTEP THAT FAILED: {step_desc}\nRECORDED STEP IDS: {', '.join(step_ids)}\n\n"
        f"SCREEN:\n{perception_text}\n\nELEMENTS:\n{elements_text}"
    )


DISTILL_SYSTEM = """You turn a successful UI run into documentation for a reusable capability that other AI agents will call.
Return a snake_case name, a one-paragraph description (what it does, what it needs, what it returns), a description per parameter and per output,
a one-line description per step, and the list of step ids that commit institution state (risky). Do not include any concrete personal data."""


def render_distill(goal: str, transcript_text: str, params: dict[str, str], outputs: list[str]) -> str:
    return f"GOAL: {goal}\nPARAMETERS: {list(params)}\nOUTPUTS: {outputs}\n\nTRANSCRIPT:\n{transcript_text}"


def elements_to_text(elements) -> str:
    lines = []
    for e in elements:
        extra = []
        if e.attrs.get("options"):
            extra.append(f"options=[{e.attrs['options']}]")
        if e.attrs.get("type"):
            extra.append(f"type={e.attrs['type']}")
        frame = f" frame={e.frame}" if e.frame else ""
        label = e.name if e.kind == "control" else f"{e.name} -> {e.text}"
        lines.append(f'{e.ref} [{e.kind}] {e.role} "{label}"{frame} {" ".join(extra)}'.rstrip())
    return "\n".join(lines)

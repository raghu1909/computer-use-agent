#!/usr/bin/env bash
# End-to-end demo: discovery -> artifact -> deterministic replays (happy / outcome / recovery / failure).
# Requires the target app running:  cua target-app --port 5000
#
#   scripts/demo.sh                 # offline: scripted LLM (no key)
#   scripts/demo.sh anthropic       # real LLM discovery (needs ANTHROPIC_API_KEY)
set -euo pipefail
cd "$(dirname "$0")/.."

LLM="${1:-scripted}"
TARGET="${TARGET_URL:-http://localhost:5000}"
export PYTHONPATH=src
export CAP_SECRET_operator_user="${CAP_SECRET_operator_user:-operator}"        # fictional demo login
export CAP_SECRET_operator_password="${CAP_SECRET_operator_password:-letmein-demo}"
CUA="python -m cli"

if [ "$LLM" = "anthropic" ]; then LLM_ARG="anthropic"; else LLM_ARG="scripted:examples/scripted_savings_balance.json"; fi

echo "### 1. discovery ($LLM): the model drives the UI once, probes two exceptional states, writes the artifact"
$CUA discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --entry "$TARGET/" \
  --params '{"member_id":"12345"}' \
  --secret-names operator_user operator_password \
  --llm "$LLM_ARG" \
  --probe "not_found={\"params\":{\"member_id\":\"99999\"}}" \
  --probe "session_timeout={\"entry_url\":\"$TARGET/?force_error=session_timeout\"}" \
  --approve --no-handoff --label "$LLM"

ART=capabilities/member_savings_balance.v1.json

echo; echo "### 2. replay, new input (no LLM)"
$CUA replay --artifact $ART --params '{"member_id":"23456"}' --require-approved --no-handoff --label happy

echo; echo "### 3. replay, business outcome: member does not exist"
$CUA replay --artifact $ART --params '{"member_id":"99999"}' --no-handoff --label not_found || true

echo; echo "### 4. replay, recoverable condition: session expires mid-flow"
$CUA replay --artifact $ART --params '{"member_id":"12345"}' --entry "$TARGET/?force_error=session_timeout" \
  --no-handoff --label session_timeout

echo; echo "### 5. replay, hard failure: injected HTTP 500 (no operator available)"
$CUA replay --artifact $ART --params '{"member_id":"12345"}' --entry "$TARGET/?force_error=app_error" \
  --no-handoff --label app_error || true

echo; echo "### 6. replay, invalid input rejected before the UI is touched"
$CUA replay --artifact $ART --params '{"member_id":"12-ABC"}' --no-handoff --label bad_input || true

echo; echo "### 7. agent-facing catalog"
$CUA catalog

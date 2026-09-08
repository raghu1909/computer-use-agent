#!/usr/bin/env bash
# Handoff demo: replay hits an injected HTTP 500 -> automation pauses and opens the operator console
# -> the operator fixes the live browser session -> automation resumes and finishes.
#
#   scripts/demo_handoff.sh          # a scripted "operator" answers via the console's HTTP API
#   OPERATOR=human scripts/demo_handoff.sh   # you act: open the printed console URL (add --headed to watch)
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${TARGET_URL:-http://localhost:5000}"
PORT="${OPERATOR_PORT:-8765}"
export PYTHONPATH=src
export CAP_SECRET_operator_user="${CAP_SECRET_operator_user:-operator}"
export CAP_SECRET_operator_password="${CAP_SECRET_operator_password:-letmein-demo}"

if [ "${OPERATOR:-scripted}" = "scripted" ]; then
  python - "$PORT" <<'EOF' &
import sys, time, httpx
base = f"http://127.0.0.1:{sys.argv[1]}"
for _ in range(300):                       # wait for the intervention to be raised
    time.sleep(0.5)
    try:
        items = httpx.get(f"{base}/interventions").json()
        if items: break
    except httpx.HTTPError: pass
iid = items[0]["id"]
print(f"[operator] intervention {iid}: {items[0]['reason']} at step {items[0]['step_id']}", flush=True)
state = httpx.get(f"{base}/interventions/{iid}/state").json()
ref = next(l.split()[0] for l in state["elements"] if "MEMBER DETAIL" in l)
print(f"[operator] clicking {ref} (MEMBER DETAIL link in the nav frame) on the live session", flush=True)
httpx.post(f"{base}/interventions/{iid}/act", data={"action": "click", "ref": ref})
httpx.post(f"{base}/interventions/{iid}/resume",
           data={"decision": "resume", "notes": "reloaded detail frame after transient 500", "resume_from_step": "s06"})
print("[operator] handed control back, resume from s06", flush=True)
EOF
fi

python -m cli replay --artifact capabilities/member_savings_balance.v1.json --params '{"member_id":"12345"}' \
  --entry "$TARGET/?force_error=app_error" --operator-port "$PORT" --label handoff "$@"
wait

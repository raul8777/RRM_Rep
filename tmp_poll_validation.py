"""Poll smart backtest validation run until completed or error, then print final decision."""
import os, json, urllib.request, time

BASE = "http://localhost:8000"
KEY = os.environ.get("DASHBOARD_API_KEY", "")
HDR = {"X-API-Key": KEY} if KEY else {}
RID = "83a0bba6-b1bb-4d5e-9d63-051ea6bfa63c"
DEADLINE = time.time() + 720  # 12 min max

def _get(path):
    req = urllib.request.Request(BASE + path, headers=HDR, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

last_pct = -1
st = "running"
while time.time() < DEADLINE:
    p = _get(f"/smart-backtest/progress/{RID}")
    st = p.get("status")
    pct = p.get("progress_pct", 0)
    sc = p.get("current_scenario", "?")
    scc = p.get("scenario_completed", 0)
    if pct != last_pct:
        print(f"  status={st}  progress={pct}%  scenario={sc}  completed={scc}/4", flush=True)
        last_pct = pct
    if st in ("completed", "error"):
        break
    time.sleep(15)

print(f"\nFINAL run status: {st}")

# Fetch decision
try:
    dec = _get(f"/tuning/candidates/spot-bull-v1/decision/{RID}")
    print(f"go_no_go      : {dec.get('go_no_go')}")
    print(f"overall        : {(dec.get('validation') or {}).get('overall')}")
    scenarios = (dec.get("validation") or {}).get("scenarios") or []
    for s in scenarios:
        print(f"  scenario={s.get('scenario'):20s}  status={s.get('status')}  candidate_pnl={((s.get('candidate') or {}).get('pnl_total','?'))}  baseline_pnl={((s.get('baseline') or {}).get('pnl_total','?'))}")
except Exception as e:
    print(f"Decision fetch failed: {e}")

# Progress stdout tail
out = p.get("stdout") or ""
if out:
    tail = out.strip().split("\n")[-20:]
    print("\n--- stdout tail ---")
    for line in tail:
        print(" ", line)

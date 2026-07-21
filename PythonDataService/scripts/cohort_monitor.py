import datetime
import json
import subprocess
import sys
import time
import urllib.request

ACCT = "DUM284968"
BASE = "http://localhost:8000"
LOG = "/tmp/cohort5_monitor.log"
MODE = sys.argv[1] if len(sys.argv) > 1 else "until_all_up"  # or "until_window_end"


def secret():
    return subprocess.run(
        ["podman", "exec", "polygon-data-service", "printenv", "DATA_PLANE_CONTROL_SECRET"],
        capture_output=True, text=True,
    ).stdout.strip()


def get(path, sec):
    req = urllib.request.Request(BASE + path, headers={"X-Data-Plane-Control-Secret": sec})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def log(msg):
    stamp = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"{stamp} {msg}\n")


start = time.time()
sec = secret()
receipt = get(f"/api/accounts/{ACCT}/cohort-batch-launches/latest", sec)
members = receipt["member_strategy_instance_ids"]
window_end = receipt["window_end_ms"]
ever_onduty: set[str] = set()
log(f"[monitor {MODE}] cohort={receipt['cohort_id']} profile={receipt.get('launch_profile')}")

while True:
    elapsed = time.time() - start
    if elapsed > 80 * 60:
        log("[timeout] 80 min"); print("TIMEOUT"); sys.exit(3)
    try:
        sec = secret()
        latest = get(f"/api/accounts/{ACCT}/cohort-batch-launches/latest", sec)
        cat = get("/api/live-instances/catalog", sec)
        acct = get("/api/accounts", sec)
    except Exception as e:
        log(f"[poll error] {e}")
        time.sleep(90); continue

    bots = {b["strategy_instance_id"]: (b.get("daily_lifecycle") or {}).get("display_status")
            for b in cat.get("bots", [])}
    onduty = [m for m in members if bots.get(m) == "On duty"]
    bad = [o for o in latest.get("outcomes", []) if o.get("state") in ("blocked", "skipped")]
    ev = latest.get("evidence", {})
    verdict = ev.get("verdict"); overlap = ev.get("healthy_overlap_ms", 0); samples = ev.get("sample_count", 0)
    truth = None
    for row in acct.get("rows", []):
        if row.get("account_id") == ACCT:
            truth = (row.get("latest_verdict_summary") or {}).get("state")

    log(f"[{int(elapsed)}s] onduty={len(onduty)}/5 {onduty} ev={verdict} overlap_ms={overlap} "
        f"samples={samples} truth={truth} bad={len(bad)}")

    if bad:
        log(f"[FAIL] blocked/skipped: {[(o['strategy_instance_id'], o.get('reason')) for o in bad]}")
        print("MEMBER_BLOCKED"); sys.exit(1)

    # Crash detection: a member that reached On duty then dropped off is a
    # runtime fatal-halt (the Fix A failure mode). Flag it distinctly.
    ever_onduty.update(onduty)
    dropped = [m for m in ever_onduty if bots.get(m) != "On duty"]
    if dropped and len(onduty) < 5:
        log(f"[FAIL] member(s) dropped after starting (crash?): {dropped} "
            f"| current statuses: {{m: bots.get(m) for m in dropped}}")
        for m in dropped:
            log(f"    {m} -> {bots.get(m)}")
        print("MEMBER_DROPPED"); sys.exit(2)

    if MODE == "until_all_up":
        if len(onduty) >= 5:
            log("[ALL_UP] 5 concurrent"); print("ALL_UP"); sys.exit(0)
        time.sleep(45)
    else:  # until_window_end
        now_ms = int(time.time() * 1000)
        # regressions: a member that was up dropping off is a health risk — flag but keep going
        if len(onduty) < 5:
            log(f"[WARN] only {len(onduty)}/5 on duty during overlap window")
        if truth not in ("CLEAN", None):
            log(f"[WARN] account truth={truth}")
        if now_ms >= window_end:
            log("[WINDOW_ENDED]"); print("WINDOW_ENDED"); sys.exit(0)
        time.sleep(120)

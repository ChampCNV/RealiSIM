# Low-disk guard (every 5 min). Pauses NEW RealiSIM processing when /workspace is nearly full, so uploads,
# exports and SpacetimeDB writes fail visibly instead of mid-job. Only clears a hold it set itself.
import json, os, shutil, time
from pathlib import Path
HOLD = Path("/workspace/photo2grasp/maintenance-hold.json")
free = shutil.disk_usage("/workspace").free / 2**30
try:
    h = json.loads(HOLD.read_text())
except Exception:
    h = {"enabled": False}
def write(d):
    t = HOLD.with_name(HOLD.name + ".tmp"); t.write_text(json.dumps(d)); os.replace(t, HOLD)
if free < 1.5 and not h.get("enabled"):
    write({**h, "enabled": True, "by": "disk-guard", "at": int(time.time() * 1000), "reason": f"Storage is nearly full ({free:.1f} GB free). New processing is paused; your saved work is safe."})
    from notification import notify_user
    notify_user(f"RealiSIM paused new jobs: only {free:.1f} GB disk left in the sandbox.", level="warning", title="RealiSIM storage low", url="https://mana.space/home/9fK1vhrVDLg53aTW10xgWPHK7Zt2/apps/photo2grasp")
    print("hold set", round(free, 2))
elif free > 2.5 and h.get("enabled") and h.get("by") == "disk-guard":
    write({**h, "enabled": False, "at": int(time.time() * 1000), "reason": "Storage recovered"}); print("hold cleared", round(free, 2))
else:
    print("ok", round(free, 2), "GB free; hold", bool(h.get("enabled")), h.get("by"))

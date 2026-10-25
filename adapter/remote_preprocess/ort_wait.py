import json, os, sys, time
spool, rid, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
os.replace(f"{spool}/{rid}.req.tmp", f"{spool}/{rid}.req.npz")
t = time.time()
while time.time() - t < timeout:
    if os.path.exists(f"{spool}/{rid}.res.npz"): print(json.dumps(dict(ok=True, wait_s=round(time.time() - t, 2)))); sys.exit(0)
    if os.path.exists(f"{spool}/{rid}.err"): print(json.dumps(dict(ok=False, error=open(f"{spool}/{rid}.err").read()))); sys.exit(0)
    st = f"{spool}/status.json"
    if os.path.exists(st):
        s = json.load(open(st))
        if s.get("state") in ("stopped", "failed"): print(json.dumps(dict(ok=False, error="worker " + s["state"] + ": " + str(s.get("reason"))))); sys.exit(0)
    time.sleep(0.05)
print(json.dumps(dict(ok=False, error="inference timeout"))); sys.exit(0)

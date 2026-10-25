# MPM node, scheduled every minute (cron "* * * * *"; parent: apps/photo2grasp build-plan/autoUpdates).
# Dispatches at most one bounded maintainer task per (candidate SHA, adapter fingerprint); the task works only in an
# isolated candidate + staged adapter copy. If that maintainer chat ends without approve/reject, the gate allows ONE
# bounded retry for the same key (never a parallel duplicate). Polling/promotion itself runs in the backend's 60 s loop.
import sys
import mana
sys.path.insert(0, "/workspace/photo2grasp/backend")
import release_gate as RG

TERMINAL = ("complete", "error", "aborted")
st = RG.load_state()
m = st.get("maintainer") or {}
if m.get("outcome") == "dispatched" and m.get("chat_id") and m.get("message_id"):
    try:
        p = mana.get_handle(m["chat_id"], message_id=m["message_id"], verbose=False).poll()
        status = p.get("status") if isinstance(p, dict) else getattr(p, "status", None)
    except Exception as e:
        status = None
        print("maintainer status unavailable:", type(e).__name__)
    if status in TERMINAL:
        RG.mark_maintainer_ended(m["last_key"], status)
        print("maintainer", m["last_key"], "ended without decision:", status)
    else:
        print("maintainer", m["last_key"], "still", status)

task = RG.maintainer_pending()
if not task:
    print("release gate:", RG.public_status()["mode"], RG.public_status()["status"], "- no maintainer task")
else:
    cid = mana.mint_chat_id()
    h = mana.ask(cid, task["prompt"], notify=False)
    import time
    for _ in range(10):                     # the recovery coordinate is chat id + message id; wait briefly for it
        if getattr(h, "message_id", None):
            break
        try:
            h.poll()
        except Exception:
            pass
        time.sleep(1)
    RG.mark_maintainer_dispatched(task["dedupe_key"], cid, getattr(h, "message_id", None))
    print("release gate: dispatched maintainer", task["dedupe_key"], "attempt", task.get("attempt", 1), cid)

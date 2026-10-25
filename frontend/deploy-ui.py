from pathlib import Path
import hashlib,json,subprocess,sys,time
from firebase_helper import firebase
from env import get_user_id
B=Path("/workspace/photo2grasp")
sys.path.insert(0,str(B))
import build_public   # product-only beta wrapper (public-shell.jsx); main code is never modified by it
base=(B/"ui-video-base.jsx").read_text();start=base.index("function UserDiv() {");tail=base.index("function RailItem(")
code="\n".join([base[:start]]+[(B/name).read_text() for name in ("workflow-ui.jsx","legacy-object-flow.jsx","support-ui.jsx","workflow-root.jsx")])+"\n\n"+base[tail:]
assert (B/"activity-tracker.jsx").read_text().strip() in code
assert "useManaRouter(" not in code
(B/"ui-video-candidate.jsx").write_text(code)
subprocess.run([str(B/"tools/node_modules/.bin/esbuild"),str(B/"ui-video-candidate.jsx"),"--loader:.jsx=jsx","--format=esm","--outfile=/tmp/realisim-ui-checked.js"],check=True,capture_output=True,text=True)
# Beta = the same main code wrapped by the product shell. Built and compiled BEFORE any write; a missing anchor aborts the
# whole deploy so the beta can never receive unwrapped code (fix build_public.py / public-shell.jsx, then rerun).
app=f"{get_user_id()}/file-system/apps/photo2grasp/app"
has_beta=firebase.read(app+"/beta/app/metadata/type")=="user_div"
if has_beta:
    public,pinfo=build_public.build_public(code)
    build_public.compile_check(public,"public")
(B/"ui2.jsx").write_text(code)
now=int(time.time()*1000);sha=hashlib.sha256(code.encode()).hexdigest()
patch={"code":code,"metadata/updatedAt":now}
if has_beta:patch.update(build_public.beta_patch(public,pinfo,now))
firebase.update(app,patch)
receipt={"at":now,"sha256":sha,"source":"ui2.jsx","nativePath":"apps/photo2grasp","privateBetaPath":"apps/photo2grasp/app/beta","public":False}
if has_beta:
    assert hashlib.sha256(firebase.read(app+"/beta/app/code").encode()).hexdigest()==pinfo["publicSha256"],"beta readback mismatch"
    receipt.update(betaPublicSha256=pinfo["publicSha256"],betaShellSha256=pinfo["shellSha256"],betaOptionalSkipped=pinfo["optionalSkipped"])
(B/"ui-deployment-receipt.json").write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))

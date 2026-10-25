"""build_public.py: RealiSIM product-only beta build (owned by the public-presentation worker).

build_public(core_code) -> (public_code, info)
  Takes the canonical assembled main UI (ui2.jsx / apps/photo2grasp/app/code) UNCHANGED and derives the beta code:
  * renames the core entry `function UserDiv() {` -> `function RealiSimCore() {` (exactly one required),
  * renames `function HfConnect(` -> `function HfConnectCore(` (exactly one required); the shell's HfConnect is owner-only,
  * applies optional visitor-copy substitutions (skipped and reported if the source text moved),
  * appends public-shell.jsx, which defines the beta UserDiv (top-layer product view).
  Raises BuildError before anything is written if a required anchor is missing, so unwrapped code can never reach beta.
compile_check(code, label) runs esbuild on the code (same check deploy-ui.py uses for main).
deploy_beta_only() rebuilds beta from the CURRENTLY DEPLOYED main code in Firebase without touching main.
"""
from pathlib import Path
import hashlib, json, subprocess, time

B = Path("/workspace/photo2grasp")
SHELL_FILE = B / "public-shell.jsx"
ESBUILD = B / "tools/node_modules/.bin/esbuild"


class BuildError(RuntimeError):
    pass


REQUIRED = [
    ("function UserDiv() {", "function RealiSimCore() {"),
    ("function HfConnect(", "function HfConnectCore("),
]
# Visitor-facing copy that would otherwise ask a visitor for their own token. Owner sees the same panels unchanged
# because the expressions are evaluated at render time.
OPTIONAL = [
    ('<Panel title="Connect Hugging Face, then retry">',
     '<Panel title={rsIsOwner() ? "Connect Hugging Face, then retry" : "Hosted GPU quota reached"}>'),
    ('<Panel title="Hugging Face access (mesh generation)">',
     '<Panel title={rsIsOwner() ? "Hugging Face access (mesh generation)" : "Mesh generation"}>'),
    ("Connect Hugging Face for mesh generation</div>",
     "{rsIsOwner() ? 'Connect Hugging Face for mesh generation' : 'Mesh generation'}</div>"),
]
SHELL_NAMES = ("RS_SHELL_VERSION", "RS_FOCUSABLE", "RS_ISOLATED_EVENTS", "rsIsOwner", "rsPopoverSupported", "rsManageParam",
               "rsFocusables", "rsTrapScope", "rsInertOutside", "RsUnsupported", "RsProductLayer", "RealiSimCore", "HfConnectCore")


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def build_public(core):
    for name in SHELL_NAMES:
        if name in core:
            raise BuildError(f"core already defines {name}; rename the shell identifier in public-shell.jsx")
    out = core
    for old, new in REQUIRED:
        n = out.count(old)
        if n != 1:
            raise BuildError(f"public build anchor {old!r} found {n} times (need exactly 1); update build_public.py/public-shell.jsx")
        out = out.replace(old, new)
    applied, skipped = [], []
    for old, new in OPTIONAL:
        n = out.count(old)
        if n >= 1:
            out = out.replace(old, new); applied.append(old)
        else:
            skipped.append(old)
    shell = SHELL_FILE.read_text()
    out = out.rstrip() + "\n\n" + shell
    if out.count("function UserDiv(") != 1 or "data-realisim-product" not in out:
        raise BuildError("public build did not produce exactly one shell UserDiv")
    if "useManaRouter(" in out:
        raise BuildError("public build must not use useManaRouter")
    return out, {"baseSha256": sha(core), "publicSha256": sha(out), "shellSha256": sha(shell),
                 "optionalApplied": applied, "optionalSkipped": skipped}


def compile_check(code, label):
    src = Path(f"/tmp/realisim-{label}-check.jsx")
    src.write_text(code)
    r = subprocess.run([str(ESBUILD), str(src), "--loader:.jsx=jsx", "--format=esm", f"--outfile=/tmp/realisim-{label}-checked.js"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"{label} build does not compile: {r.stderr[-1500:]}")


def beta_patch(public, info, now):
    return {"beta/app/code": public, "beta/app/metadata/updatedAt": now,
            "beta/app/metadata/sourceSha256": info["baseSha256"], "beta/app/metadata/publicSha256": info["publicSha256"],
            "beta/app/metadata/shell": "public-shell.jsx",
            "beta/app/distribution": {"builtAt": now, "sha256": info["publicSha256"], "baseSha256": info["baseSha256"],
                                      "shellSha256": info["shellSha256"], "source": "apps/photo2grasp/app/code",
                                      "build": "build_public.py + public-shell.jsx"}}


def deploy_beta_only():
    from firebase_helper import firebase
    from env import get_user_id
    app = f"{get_user_id()}/file-system/apps/photo2grasp/app"
    if firebase.read(app + "/beta/app/metadata/type") != "user_div":
        raise BuildError("beta surface missing")
    core = firebase.read(app + "/code")
    public, info = build_public(core)
    compile_check(public, "public")
    now = int(time.time() * 1000)
    firebase.update(app, beta_patch(public, info, now))
    back = firebase.read(app + "/beta/app/code")
    if sha(back) != info["publicSha256"]:
        raise BuildError("beta readback mismatch")
    info.update(at=now, mode="beta-only", privateBetaPath="apps/photo2grasp/app/beta", public=False)
    (B / "public-beta-build-receipt.json").write_text(json.dumps(info, indent=2))
    return info


if __name__ == "__main__":
    print(json.dumps(deploy_beta_only(), indent=2))

"""Introspect the photo2grasp repo so the web app follows it as it changes.

Captures the real argparse definitions of photo2grasp.py (every step) and make_asset.py without running them,
scans make_asset.py for its "[i/n] label" progress prints (the stage rail), and reads objects.json + assets/.
Prints one JSON object.
"""
import argparse, json, re, runpy, subprocess, sys
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "pipeline"))

class _Captured(Exception):
    def __init__(self, parser): self.parser = parser

def _describe(action):
    kind = type(action).__name__
    t = getattr(action, "type", None)
    return dict(dest=action.dest, flags=list(action.option_strings), help=action.help or "",
                default=action.default if isinstance(action.default, (str, int, float, bool, type(None), list)) else str(action.default),
                choices=list(action.choices) if action.choices and not isinstance(action.choices, dict) else None,
                nargs=action.nargs if isinstance(action.nargs, (int, str, type(None))) else str(action.nargs),
                required=bool(getattr(action, "required", False)), kind=kind,
                type=getattr(t, "__name__", None) if t else None,
                positional=not action.option_strings)

def capture(script):
    orig = argparse.ArgumentParser.parse_args
    def fake(self, *a, **k): raise _Captured(self)
    argparse.ArgumentParser.parse_args = fake
    old_argv = sys.argv
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except _Captured as c:
        return c.parser
    except SystemExit:
        return None
    finally:
        argparse.ArgumentParser.parse_args = orig
        sys.argv = old_argv
    return None

def parser_info(p):
    if p is None: return None
    info = dict(description=(p.description or "").strip(), args=[], steps=[])
    for a in p._actions:
        if isinstance(a, argparse._HelpAction): continue
        if isinstance(a, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in a._choices_actions}
            for name, sp in a.choices.items():
                info["steps"].append(dict(name=name, help=helps.get(name, "") or "",
                                          args=[_describe(x) for x in sp._actions if not isinstance(x, argparse._HelpAction)]))
            continue
        info["args"].append(_describe(a))
    return info

def stages(script):
    if not script.exists(): return []
    src = script.read_text()
    out = {}
    for m in re.finditer(r"""["']\[(\d+)/(\d+)\]\s*([^"'\n]*)""", src):
        i, n, label = int(m.group(1)), int(m.group(2)), m.group(3)
        label = re.sub(r"\{[^}]*\}", "…", label).replace("->", "→").strip(" .")
        out.setdefault(i, dict(index=i, total=n, label=label))
    return [out[k] for k in sorted(out)]

res = dict(ok=True, errors=[])
for key, rel in (("cli", "photo2grasp.py"), ("make_asset", "make_asset.py")):
    try:
        res[key] = parser_info(capture(REPO / rel)) if (REPO / rel).exists() else None
    except Exception as e:
        res[key] = None; res["errors"].append(f"{rel}: {type(e).__name__}: {e}")
res["stages"] = stages(REPO / "make_asset.py")
try:
    res["objects"] = json.loads((REPO / "objects.json").read_text())
except Exception as e:
    res["objects"] = {}; res["errors"].append(f"objects.json: {e}")
try:
    tracked = subprocess.run(["git", "ls-files", "assets"], cwd=REPO, capture_output=True, text=True).stdout.split()
    dirs = sorted({Path(t).parts[1] for t in tracked if len(Path(t).parts) > 2})
    res["assets"] = [d for d in dirs if (REPO / "assets" / d / f"{d}.xml").exists()]
except Exception as e:
    res["assets"] = []; res["errors"].append(f"assets: {e}")
readme = REPO / "README.md"
if readme.exists():
    txt = readme.read_text()
    title = next((l[2:].strip() for l in txt.splitlines() if l.startswith("# ")), "")
    tagline = next((l.strip("* ").strip() for l in txt.splitlines()[1:12] if l.startswith("**")), "")
    res["readme"] = dict(title=title, tagline=tagline)
print(json.dumps(res, default=str))

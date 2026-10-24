# SO-100 AprilTag lift: train in Colab without Google Drive.
# On the laptop first: python photo2grasp.py train <object> --colab   (or: python colab/build_zip.py --object ...)
# Open a scratchpad notebook (not saved to Drive): https://colab.research.google.com/notebooks/empty.ipynb
# Runtime > Change runtime type > T4 GPU. Paste this whole cell and run it.
#   New run:     pick <task>_train.zip (object, area, steps and starting model are inside it)
#   Continue:    pick <task>_train.zip AND the zip of the run to continue (a backup, or a finished
#                <task>_..._run.zip): select both files together. The run continues up to the zip's total steps.
# Every 250k steps a <task>_backup_<step>k.zip downloads to your computer: if Colab disconnects, continue from it.
import io, os, re, shutil, subprocess, zipfile
from google.colab import files

STEPS = 1_000_000       # total steps unless the zip says otherwise (~3 h on a T4)
BACKUP_EVERY = 250_000  # download a backup zip this often


def holds(f, name):
    return f.endswith(".zip") and name in zipfile.ZipFile(f).namelist()


print("Pick <task>_train.zip (and a run/backup zip to continue it)...")
uploaded = list(files.upload())
code = [f for f in uploaded if holds(f, "train_args.txt")][0]  # the training package
zipfile.ZipFile(code).extractall("/content/so100_tag")
subprocess.run("pip -q install mujoco==3.16.0", shell=True, check=True)

args_file = "/content/so100_tag/train_args.txt"
train_args = open(args_file).read().split() if os.path.exists(args_file) else []
cmd = ["python", "-u", "train_tag.py"] + train_args
if "--steps" not in train_args:
    cmd += ["--steps", str(STEPS)]
arg = lambda k, default: train_args[train_args.index(k) + 1] if k in train_args else default  # noqa: E731
task = arg("--object", "lego_tree") + (f"_from_{arg('--hand', '')}" if "--hand" in train_args else "")
total = int(cmd[cmd.index("--steps") + 1])
label = lambda n: f"{n // 1_000_000}m" if n % 1_000_000 == 0 else f"{n // 1000}k"  # noqa: E731
runs = [f for f in uploaded if f != code and holds(f, "train_state.pt")]  # an earlier run / backup to continue
if runs:
    import torch
    step_of = lambda f: torch.load(io.BytesIO(zipfile.ZipFile(f).read("train_state.pt")),  # noqa: E731
                                   map_location="cpu", weights_only=False)["step"]
    latest = max(runs, key=step_of)
    print(f"continuing {latest} (step {step_of(latest)}) up to {total} steps")
    run_dir = "/content/so100_tag/runs/tag_resumed"
    zipfile.ZipFile(latest).extractall(run_dir)
    cmd += ["--resume", run_dir]
print("running:", " ".join(cmd[2:]))

os.chdir("/content/so100_tag/sim2real")
run_dir = None
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in proc.stdout:
    print(line, end="")
    m = re.match(r"run dir (\S+)", line)
    if m:
        run_dir = m.group(1)
    m = re.match(r"step\s+(\d+) \|", line)
    if m and run_dir and int(m.group(1)) % BACKUP_EVERY == 0:
        name = f"/content/{task}_backup_{int(m.group(1)) // 1000}k"
        shutil.make_archive(name, "zip", run_dir)
        files.download(name + ".zip")
proc.wait()

shutil.make_archive(f"/content/{task}_{label(total)}_run", "zip", run_dir)
files.download(f"/content/{task}_{label(total)}_run.zip")  # in robot_sim/: python photo2grasp.py import <that zip>

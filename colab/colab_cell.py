# SO-100 AprilTag lift: train in Colab without Google Drive.
# On the laptop first: python photo2grasp.py train <object> --colab   (or: python colab/build_zip.py --object ...)
# Open a scratchpad notebook (not saved to Drive): https://colab.research.google.com/notebooks/empty.ipynb
# Runtime > Change runtime type > T4 GPU. Paste this whole cell and run it.
#   New run:     pick <object>_train.zip (object, area and starting model are inside it)
#   Continue:    pick <object>_train.zip AND your latest tag_backup_*.zip (select both files together)
# Every 250k steps a tag_backup_<step>.zip downloads to your computer: if Colab disconnects, continue from it.
import os, re, shutil, subprocess, zipfile
from google.colab import files

STEPS = 1_000_000       # total steps unless the zip says otherwise (~3 h on a T4)
BACKUP_EVERY = 250_000  # download a backup zip this often

print("Pick <object>_train.zip (and a tag_backup_*.zip to continue a run)...")
uploaded = list(files.upload())
code = [f for f in uploaded if f.endswith(".zip") and "tag_backup" not in f][0]  # the training package
zipfile.ZipFile(code).extractall("/content/so100_tag")
subprocess.run("pip -q install mujoco==3.16.0", shell=True, check=True)

args_file = "/content/so100_tag/train_args.txt"
train_args = open(args_file).read().split() if os.path.exists(args_file) else []
cmd = ["python", "-u", "train_tag.py"] + train_args
if "--steps" not in train_args:
    cmd += ["--steps", str(STEPS)]
backups = [f for f in uploaded if "tag_backup" in f]
if backups:
    run_dir = "/content/so100_tag/runs/tag_resumed"
    latest = max(backups, key=lambda f: int(re.search(r"(\d+)k", f).group(1)))
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
        name = f"/content/tag_backup_{int(m.group(1)) // 1000}k"
        shutil.make_archive(name, "zip", run_dir)
        files.download(name + ".zip")
proc.wait()

obj = train_args[train_args.index("--object") + 1] if "--object" in train_args else "lego_tree"
shutil.make_archive(f"/content/{obj}_run", "zip", run_dir)
files.download(f"/content/{obj}_run.zip")  # put it in robot_sim/: python photo2grasp.py import <object>_run.zip

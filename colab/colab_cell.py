# SO-100 AprilTag lift: train in Colab without Google Drive.
# Open a scratchpad notebook (not saved to Drive): https://colab.research.google.com/notebooks/empty.ipynb
# Runtime > Change runtime type > T4 GPU. Paste this whole cell and run it.
#   New run:     pick so100_tag_train.zip
#   Continue:    pick so100_tag_train.zip AND your latest tag_backup_*.zip (select both files together)
#   Env-camera fine-tune: pick so100_tag_train.zip AND tag_backup_2000k.zip -> continues the 2M model to 2.6M
# Every 250k steps a tag_backup_<step>.zip downloads to your computer: if Colab disconnects, continue from it.
import glob, os, re, shutil, subprocess, zipfile
from google.colab import files

STEPS = 2_600_000       # TOTAL steps: from the 2M backup that is 600k more (~2 h on a T4)
BACKUP_EVERY = 250_000  # download a backup zip this often

print("Pick so100_tag_train.zip (and a tag_backup_*.zip to continue a run)...")
uploaded = list(files.upload())
code = [f for f in uploaded if "so100_tag_train" in f][0]
zipfile.ZipFile(code).extractall("/content/so100_tag")
subprocess.run("pip -q install mujoco==3.16.0", shell=True, check=True)

cmd = ["python", "-u", "train_tag.py", "--steps", str(STEPS)]
backups = [f for f in uploaded if "tag_backup" in f]
if backups:
    run_dir = "/content/so100_tag/runs/tag_resumed"
    latest = max(backups, key=lambda f: int(re.search(r"(\d+)k", f).group(1)))
    zipfile.ZipFile(latest).extractall(run_dir)
    cmd += ["--resume", run_dir]

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

shutil.make_archive("/content/tag_run", "zip", run_dir)
files.download("/content/tag_run.zip")  # actor_best.pt + log -> unzip into robot_sim/runs/<name>/ on the laptop

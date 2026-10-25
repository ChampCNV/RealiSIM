"""Run the video pipeline in the foreground, one stage after another.

    python -m api.run inputs/scene/video.mp4 --table 180 75 74
    python -m api.run --job <id> --from-stage reference_images --reuse-meshes

Stages: reconstruct, find_objects, object_views, identify, reference_images, physics, build, render.
Gemini names each object from the frames, SerpAPI fetches product photos, and the last stage
writes MuJoCo renders of the table with the SO-100.
"""
import argparse
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import jobs


def run(job_id, from_stage, reuse_meshes=False):
    job = jobs.load(job_id)
    if job is None:
        raise SystemExit(f"no job {job_id}")
    if from_stage not in jobs.STAGES:
        raise SystemExit(f"stage must be one of {jobs.STAGES}")
    work = jobs.job_dir(job_id)
    job.update(status="running", error=None, reuse_meshes=reuse_meshes, next_stage=from_stage)
    jobs.save(job)
    first = jobs.STAGES.index(from_stage)
    for i in range(first, len(jobs.STAGES)):
        stage = jobs.STAGES[i]
        job.update(stage=stage, next_stage=stage)
        jobs.save(job)
        print(f"\n== [{job_id}] {stage}", flush=True)
        started = time.time()
        try:
            jobs.STAGE_FUNCS[stage](job, work)
        except Exception as exc:
            traceback.print_exc()
            job.update(status="failed", error=f"{stage}: {exc}")
            jobs.save(job)
            raise SystemExit(f"{stage} failed: {exc}")
        print(f"== {stage} done in {time.time() - started:.0f} s", flush=True)
        job["next_stage"] = jobs.STAGES[i + 1] if i + 1 < len(jobs.STAGES) else None
        jobs.save(job)
        if stage == jobs.REVIEW_AFTER and job["review"] and i < len(jobs.STAGES) - 1:
            job.update(status="review", stage=None)
            jobs.save(job)
            print("paused for review")
            return job
    job.update(status="done", stage=None)
    jobs.save(job)
    return job


def main():
    parser = argparse.ArgumentParser(description="Video frames -> identified objects -> web photos -> MuJoCo scene")
    parser.add_argument("video", nargs="?", help="phone video of the table")
    parser.add_argument("--table", nargs=3, type=float, metavar=("L", "W", "H"), default=[180, 75, 74],
                        help="table length, width, height in cm")
    parser.add_argument("--job", help="resume an existing job instead of starting one")
    parser.add_argument("--from-stage", default="reconstruct", choices=jobs.STAGES)
    parser.add_argument("--reuse-meshes", action="store_true", help="keep generated meshes and only repaint them")
    parser.add_argument("--review", action="store_true", help="pause after the physics lookup")
    args = parser.parse_args()

    if args.job:
        job_id = args.job
        start = args.from_stage
    else:
        if not args.video:
            parser.error("give a video, or --job to resume")
        video = Path(args.video)
        if not video.is_file():
            raise SystemExit(f"no video {video}")
        job = jobs.create(args.table, args.review, video.suffix.lower() or ".mp4")
        shutil.copy(video, jobs.job_dir(job["id"]) / job["video"])
        job_id, start = job["id"], jobs.STAGES[0]
        print(f"job {job_id}", flush=True)
    job = run(job_id, start, args.reuse_meshes)
    work = jobs.job_dir(job_id)
    print(f"\njob {job_id}: {job['status']}")
    for name in job.get("renders", []):
        print(f"  {work / name}")


if __name__ == "__main__":
    sys.exit(main())

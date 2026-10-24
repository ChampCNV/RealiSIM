"""HTTP API: upload a video of a table, get back a MuJoCo scene with every object on it, at its real place.

    .venv/bin/python -m uvicorn api.server:app --port 8000      (interactive docs at /docs)

Keys (environment or .env): GEMINI_API_KEY (required), SERPAPI_API_KEY (web photos; optional), HF_TOKEN (TRELLIS).
"""
import io
import json
import shutil
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import jobs
from .config import ROOT


@asynccontextmanager
async def lifespan(app):
    jobs.start()
    yield


app = FastAPI(title="Photo2Grasp", lifespan=lifespan,
              description="Video of a table -> identified objects -> web reference photos -> real physics -> "
                          "MuJoCo scene with the SO-100 arm. Stages: " + " -> ".join(jobs.STAGES))


def _job(job_id):
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return job


def _idle(job):
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, f"job is {job['status']} (stage {job['stage'] or job['next_stage']})")


@app.post("/jobs", status_code=201)
def create_job(video: UploadFile = File(..., description="phone video walking around the table"),
               table_length_cm: float = Form(...), table_width_cm: float = Form(...),
               table_height_cm: float = Form(..., description="sets the metric scale of the scan"),
               review: bool = Form(False, description="pause after the physics lookup so you can correct it")):
    suffix = Path(video.filename or "").suffix.lower() or ".mp4"
    job = jobs.create([table_length_cm, table_width_cm, table_height_cm], review, suffix)
    with open(jobs.job_dir(job["id"]) / job["video"], "wb") as f:
        shutil.copyfileobj(video.file, f)
    return jobs.submit(job["id"], jobs.STAGES[0])


@app.get("/jobs")
def list_jobs():
    return [dict(id=j["id"], status=j["status"], stage=j["stage"], created=j["created"],
                 objects=[o.get("identity", {}).get("label", k) for k, o in j["objects"].items()])
            for j in jobs.all_jobs()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id)


@app.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
def get_log(job_id: str, tail: int = 200):
    _job(job_id)
    path = jobs.job_dir(job_id) / "log.txt"
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    return "\n".join(lines[-tail:])


class PhysicsEdit(BaseModel):
    size_m: list[float] | None = Field(None, min_length=3, max_length=3, description="[length, width, height]")
    mass_kg: float | None = None
    material: str | None = None
    friction: float | None = Field(None, description="sliding friction coefficient")
    collision: str | None = Field(None, pattern="^(cylinder|box|mesh)$")
    source: str | None = None


class ObjectEdit(BaseModel):
    include: bool | None = None
    label: str | None = None
    slug: str | None = None
    search_query: str | None = None
    physics: PhysicsEdit | None = None


@app.patch("/jobs/{job_id}/objects/{key}")
def edit_object(job_id: str, key: str, edit: ObjectEdit):
    """Correct what an object is or its physics. Edited parts are kept when stages are rerun."""
    job = _job(job_id)
    _idle(job)
    obj = job["objects"].get(key)
    if obj is None:
        raise HTTPException(404, f"no object {key}; objects: {list(job['objects'])}")
    if edit.include is not None:
        obj["include"] = edit.include
    ident = {k: v for k, v in edit.model_dump(include={"label", "slug", "search_query"}).items() if v is not None}
    if ident:
        if "identity" not in obj:
            raise HTTPException(409, "object not identified yet; run the identify stage first")
        if "slug" in ident:
            ident["slug"] = "".join(c if c.isalnum() else "_" for c in ident["slug"].lower()).strip("_")
        obj["identity"].update(ident)
        obj["edited"] = sorted(set(obj["edited"]) | {"identity"})
    if edit.physics is not None:
        changes = {k: v for k, v in edit.physics.model_dump().items() if v is not None}
        if "friction" in changes:
            changes["friction"] = [changes["friction"], 0.005, 0.0001]
        if "source" in changes:
            changes["source"] = changes["source"].replace("--", "-")
        if "physics" not in obj and not {"size_m", "mass_kg", "friction"} <= changes.keys():
            raise HTTPException(409, "no looked-up physics yet: give at least size_m, mass_kg and friction")
        obj["physics"] = {"material": "", "collision": "mesh", "source": "set by hand",
                          **obj.get("physics", {}), **changes}
        obj["edited"] = sorted(set(obj["edited"]) | {"physics"})
    jobs.save(job)
    return obj


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str, from_stage: str = "build", reuse_meshes: bool = False):
    """(Re)run from a stage, e.g. after a review or an edit. reuse_meshes skips TRELLIS for objects that have one."""
    job = _job(job_id)
    _idle(job)
    if from_stage not in jobs.STAGES:
        raise HTTPException(400, f"stage must be one of {jobs.STAGES}")
    reached = jobs.STAGES.index(job["next_stage"]) if job["next_stage"] else len(jobs.STAGES)
    if jobs.STAGES.index(from_stage) > reached:
        raise HTTPException(400, f"stage '{job['next_stage']}' hasn't run yet; start from it or earlier")
    return jobs.submit(job_id, from_stage, reuse_meshes)


@app.get("/jobs/{job_id}/files/{path:path}")
def get_file(job_id: str, path: str):
    """Any file of the job: video crops, web photos, previews, renders."""
    _job(job_id)
    root = jobs.job_dir(job_id).resolve()
    file = (root / path).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        raise HTTPException(404, f"no file {path}")
    return FileResponse(file)


@app.get("/jobs/{job_id}/scene.zip")
def get_scene(job_id: str):
    """layout.json, renders and every object's MuJoCo asset, laid out like the repo:
    unzip at the repo root and run `python scene/view_scene.py outputs/jobs/<id>`."""
    job = _job(job_id)
    work = jobs.job_dir(job_id)
    if not (work / "layout.json").exists():
        raise HTTPException(409, "scene not built yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        base = Path("outputs") / "jobs" / job_id
        for name in ["layout.json", "info.json", "job.json", *job.get("renders", [])]:
            if (work / name).exists():
                z.write(work / name, base / name)
        for name in [o["name"] for o in json.loads((work / "layout.json").read_text())["objects"]]:
            for f in (ROOT / "assets" / name).iterdir():
                z.write(f, Path("assets") / name / f.name)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="scene_{job_id}.zip"'})

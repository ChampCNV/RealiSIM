"""SpacetimeDB gateway client for the RealiSIM workspace.

The gateway is the identity that published the module (recorded as `config.gateway` at init): only it may call the
reducers, and every call carries the end user's Firebase-verified uid, which the module checks against project
ownership / membership. Metadata reads use SQL over HTTP (owner-only on private tables); artifact bytes are read
back through the access-checked `read_chunk` procedure.
"""
import hashlib, json, math, threading, time, tomllib, uuid
from pathlib import Path
import requests

ROOT = Path("/workspace/photo2grasp/stdb")
DB = "photo2grasp"
CHUNK = 512 * 1024  # PRD §7: 512 KiB per artifact chunk row


class StdbError(Exception):
    pass


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


class Stdb:
    def __init__(self):
        cfg = tomllib.loads((ROOT / "root" / "config" / "cli.toml").read_text())
        self.token = cfg["spacetimedb_token"]
        self.base = f"http://127.0.0.1:{(ROOT / 'port').read_text().strip()}/v1/database/{DB}"
        self.local = threading.local()

    def _s(self):
        if not hasattr(self.local, "s"):
            s = requests.Session()
            s.headers["Authorization"] = f"Bearer {self.token}"
            self.local.s = s
        return self.local.s

    def call(self, reducer, **args):
        for attempt in range(3):
            try:
                r = self._s().post(f"{self.base}/call/{reducer}", data=json.dumps(args),
                                   headers={"Content-Type": "application/json"}, timeout=120)
            except requests.RequestException as e:
                if attempt == 2:
                    raise StdbError(f"database unreachable: {e}")
                time.sleep(1 + attempt)
                continue
            if r.status_code == 200:
                return r.text and json.loads(r.text) if r.text.strip() else None
            if r.status_code >= 500 and r.status_code != 530 and attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise StdbError(r.text.strip()[:500] or f"HTTP {r.status_code}")

    def sql(self, query, *, timeout=120):
        r = self._s().post(f"{self.base}/sql", data=query, timeout=timeout)
        if r.status_code != 200:
            raise StdbError(r.text.strip()[:500])
        out = []
        for res in r.json():
            names = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
            names = [n.replace("sha_256", "sha256") for n in names]  # SQL snake-cases digits
            out += [dict(zip(names, row)) for row in res["rows"]]
        return out

    # ── artifacts ─────────────────────────────────────────────────────────────
    def begin_manifest(self, uid, project_id, kind, ref_id, version, deps=None, mid=None):
        mid = mid or new_id("man")
        self.call("begin_manifest", uid=uid, id=mid, project_id=project_id, kind=kind, ref_id=ref_id, version=str(version),
                  dependency_json=json.dumps(deps or {}))
        return mid

    def store_file(self, uid, project_id, manifest_id, path, data, media_type="application/octet-stream"):
        """Write bytes as 512 KiB chunks, read every chunk back through the access-checked procedure, compare the
        reassembled SHA-256, and only then mark the file verified."""
        fid = new_id("f")
        sha = hashlib.sha256(data).hexdigest()
        n = max(1, math.ceil(len(data) / CHUNK))
        self.call("begin_file", uid=uid, id=fid, manifest_id=manifest_id, project_id=project_id, path=path,
                  media_type=media_type, size=float(len(data)), sha256=sha, chunk_size=CHUNK, chunk_count=n)
        for i in range(n):
            self.call("put_chunk", uid=uid, project_id=project_id, file_id=fid, idx=i, data=data[i * CHUNK:(i + 1) * CHUNK].hex())
        back = b"".join(self.read_chunk(uid, project_id, fid, i) for i in range(n))
        got = hashlib.sha256(back).hexdigest()
        if got != sha or len(back) != len(data):
            raise StdbError(f"read-back verification failed for {path}: {got[:12]} != {sha[:12]}")
        self.call("mark_file_verified", uid=uid, project_id=project_id, file_id=fid, sha256=sha)
        return dict(id=fid, path=path, size=len(data), sha256=sha, media_type=media_type, chunks=n)

    def read_chunk(self, uid, project_id, file_id, idx):
        r = self._s().post(f"{self.base}/call/read_chunk", headers={"Content-Type": "application/json"},
                           data=json.dumps(dict(uid=uid, project_id=project_id, file_id=file_id, idx=idx)), timeout=120)
        if r.status_code != 200:
            raise StdbError(r.text.strip()[:300])
        return bytes.fromhex(r.json())

    def read_file(self, uid, project_id, file_id):
        f = self.sql(f"SELECT * FROM artifact_file WHERE id = {q(file_id)}")
        if not f or f[0]["project_id"] != project_id:
            raise StdbError("file not found")
        return f[0], (self.read_chunk(uid, project_id, file_id, i) for i in range(int(f[0]["chunk_count"])))

    def read_bytes(self, uid, project_id, file_id):
        meta, chunks = self.read_file(uid, project_id, file_id)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise StdbError(f"stored bytes do not match their hash: {meta['path']}")
        return meta, data

    def seal(self, uid, project_id, manifest_id, files):
        self.call("seal_manifest", uid=uid, project_id=project_id, id=manifest_id,
                  files_json=json.dumps([{k: f[k] for k in ("id", "path", "size", "sha256", "media_type")} for f in files]))

    def store_dir(self, uid, project_id, kind, ref_id, version, entries, deps=None):
        """entries: list of (path, bytes, media_type). Returns (manifest_id, files). Fails the manifest on error."""
        mid = self.begin_manifest(uid, project_id, kind, ref_id, version, deps)
        files = []
        try:
            for path, data, mt in entries:
                files.append(self.store_file(uid, project_id, mid, path, data, mt))
            self.seal(uid, project_id, mid, files)
        except Exception:
            try:
                self.call("fail_manifest", uid=uid, project_id=project_id, id=mid)
            except Exception:
                pass
            raise
        return mid, files

    def manifest_files(self, manifest_id):
        return self.sql(f"SELECT id, path, size, sha_256, media_type, status, chunk_count FROM artifact_file WHERE manifest_id = {q(manifest_id)}")

    def restore_manifest(self, uid, project_id, manifest_id, dest, prefix=""):
        """Write a manifest's files (optionally only those under `prefix`) to `dest`, verifying hashes."""
        dest = Path(dest)
        n = 0
        for f in self.manifest_files(manifest_id):
            if prefix and not f["path"].startswith(prefix):
                continue
            meta, data = self.read_bytes(uid, project_id, f["id"])
            p = dest / f["path"][len(prefix):]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            n += 1
        return n

    def delete_manifest(self, uid, project_id, manifest_id):
        for f in self.manifest_files(manifest_id):
            self.call("delete_file", uid=uid, project_id=project_id, file_id=f["id"])
        self.call("delete_manifest", uid=uid, project_id=project_id, id=manifest_id)

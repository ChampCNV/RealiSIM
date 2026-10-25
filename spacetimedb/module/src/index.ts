// Photo2Grasp Web Workspace: system of record (PRD §7, FR-08).
// Every table is private (no `public: true`): rows and artifact bytes are never exposed through subscriptions.
// Only the gateway identity (the publisher, recorded at init) may call reducers/procedures; each call carries the
// Firebase-verified uid of the end user and is checked against project ownership or collaborator membership.
import { schema, table, t, SenderError } from 'spacetimedb/server';

const spacetimedb = schema({
  config: table({}, { id: t.u32().primaryKey(), gateway: t.identity(), schema_version: t.u32() }),
  project: table({}, {
    id: t.string().primaryKey(), owner_uid: t.string().index('btree'), name: t.string(), robot_profile: t.string(),
    archived: t.bool(), settings_json: t.string(), created_at: t.f64(), updated_at: t.f64(),
  }),
  project_member: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), uid: t.string().index('btree'),
    role: t.string(), added_by: t.string(), added_at: t.f64(),
  }),
  upload: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), capture_mode: t.string(), filename: t.string(),
    media_type: t.string(), size: t.f64(), sha256: t.string(), status: t.string(), chunk_size: t.u32(),
    chunks_total: t.u32(), chunks_received: t.u32(), meta_json: t.string(), error: t.string(),
    created_at: t.f64(), updated_at: t.f64(),
  }),
  pipeline_run: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), kind: t.string(), status: t.string(),
    stage: t.string(), stages_json: t.string(), progress: t.f64(), error: t.string(), input_json: t.string(),
    output_json: t.string(), log_tail: t.string(), commit: t.string(), attempt: t.u32(),
    created_at: t.f64(), started_at: t.f64(), finished_at: t.f64(), updated_at: t.f64(),
  }),
  asset_version: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), version: t.u32(), object_name: t.string(),
    status: t.string(), measurements_json: t.string(), properties_json: t.string(), params_json: t.string(),
    sources_json: t.string(), run_id: t.string(), parent_id: t.string(), manifest_id: t.string(),
    review_json: t.string(), created_at: t.f64(), updated_at: t.f64(),
  }),
  scene_version: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), version: t.u32(), asset_version_ids: t.string(),
    robot_profile: t.string(), params_json: t.string(), validation_json: t.string(), manifest_id: t.string(),
    run_id: t.string(), status: t.string(), created_at: t.f64(),
  }),
  simulation_run: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), scene_version_id: t.string().index('btree'),
    mode: t.string(), seed: t.f64(), controls_json: t.string(), metrics_json: t.string(), result: t.string(),
    manifest_id: t.string(), engine_json: t.string(), created_at: t.f64(),
  }),
  gym_export: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), scene_version_id: t.string(),
    asset_version_id: t.string(), package_version: t.string(), env_id: t.string(), status: t.string(),
    validation_json: t.string(), dependency_json: t.string(), manifest_id: t.string(), archive_file_id: t.string(),
    run_id: t.string(), created_at: t.f64(), updated_at: t.f64(),
  }),
  artifact_manifest: table({}, {
    id: t.string().primaryKey(), project_id: t.string().index('btree'), kind: t.string(), ref_id: t.string(),
    version: t.string(), status: t.string(), file_count: t.u32(), total_bytes: t.f64(), files_json: t.string(),
    dependency_json: t.string(), created_at: t.f64(), verified_at: t.f64(),
  }),
  artifact_file: table({}, {
    id: t.string().primaryKey(), manifest_id: t.string().index('btree'), project_id: t.string().index('btree'),
    path: t.string(), media_type: t.string(), size: t.f64(), sha256: t.string(), chunk_size: t.u32(),
    chunk_count: t.u32(), status: t.string(), created_at: t.f64(), verified_at: t.f64(),
  }),
  artifact_chunk: table({}, {
    id: t.string().primaryKey(), file_id: t.string().index('btree'), idx: t.u32(), data: t.array(t.u8()),
  }),
});
export default spacetimedb;

const now = (ctx: any) => Number(ctx.timestamp.microsSinceUnixEpoch / 1000n);

function gateway(ctx: any) {
  const cfg = ctx.db.config.id.find(1);
  if (!cfg) throw new SenderError('database not initialised');
  if (!cfg.gateway.isEqual(ctx.sender)) throw new SenderError('only the Photo2Grasp gateway may call this database');
}
function access(ctx: any, uid: string, projectId: string, write: boolean) {
  gateway(ctx);
  if (!uid) throw new SenderError('unauthenticated');
  const p = ctx.db.project.id.find(projectId);
  if (!p) throw new SenderError('project not found');
  if (p.owner_uid === uid) return p;
  for (const m of ctx.db.project_member.project_id.filter(projectId)) {
    if (m.uid === uid && (!write || m.role === 'editor')) return p;
  }
  throw new SenderError('no access to this project');
}
function owner(ctx: any, uid: string, projectId: string) {
  const p = access(ctx, uid, projectId, true);
  if (p.owner_uid !== uid) throw new SenderError('only the project owner may do this');
  return p;
}
function touch(ctx: any, p: any) { ctx.db.project.id.update({ ...p, updated_at: now(ctx) }); }

export const init = spacetimedb.init((ctx) => {
  ctx.db.config.insert({ id: 1, gateway: ctx.sender, schema_version: 1 });
});

// ── projects & access ───────────────────────────────────────────────────────
export const create_project = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), name: t.string(), robot_profile: t.string(), settings_json: t.string() },
  (ctx, a) => {
    gateway(ctx);
    if (!a.uid) throw new SenderError('unauthenticated');
    if (ctx.db.project.id.find(a.id)) throw new SenderError('project id already exists');
    const name = a.name.trim();
    if (!name || name.length > 120) throw new SenderError('project name must be 1-120 characters');
    ctx.db.project.insert({ id: a.id, owner_uid: a.uid, name, robot_profile: a.robot_profile, archived: false,
      settings_json: a.settings_json, created_at: now(ctx), updated_at: now(ctx) });
  });

export const update_project = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), name: t.string(), archived: t.bool(), settings_json: t.string() },
  (ctx, a) => {
    const p = access(ctx, a.uid, a.id, true);
    const name = a.name.trim();
    if (!name || name.length > 120) throw new SenderError('project name must be 1-120 characters');
    if (a.archived !== p.archived && p.owner_uid !== a.uid) throw new SenderError('only the owner may archive');
    ctx.db.project.id.update({ ...p, name, archived: a.archived, settings_json: a.settings_json, updated_at: now(ctx) });
  });

export const set_member = spacetimedb.reducer(
  { uid: t.string(), project_id: t.string(), member_uid: t.string(), role: t.string() },
  (ctx, a) => {
    const p = owner(ctx, a.uid, a.project_id);
    if (a.member_uid === p.owner_uid) throw new SenderError('the owner already has access');
    const id = a.project_id + ':' + a.member_uid;
    ctx.db.project_member.id.delete(id);
    if (a.role === 'none') return;
    if (a.role !== 'viewer' && a.role !== 'editor') throw new SenderError('role must be viewer or editor');
    ctx.db.project_member.insert({ id, project_id: a.project_id, uid: a.member_uid, role: a.role, added_by: a.uid, added_at: now(ctx) });
  });

// Deletes all metadata rows of a project. Artifact bytes are removed first, file by file, with delete_file
// (keeps each transaction small); this reducer refuses while any artifact file remains.
export const delete_project = spacetimedb.reducer({ uid: t.string(), id: t.string() }, (ctx, a) => {
  owner(ctx, a.uid, a.id);
  for (const f of ctx.db.artifact_file.project_id.filter(a.id)) throw new SenderError('delete artifact files first (' + f.id + ')');
  for (const tbl of ['artifact_manifest', 'gym_export', 'simulation_run', 'scene_version', 'asset_version', 'pipeline_run', 'upload', 'project_member']) {
    const rows = [...(ctx.db as any)[tbl].project_id.filter(a.id)];
    for (const r of rows) (ctx.db as any)[tbl].id.delete(r.id);
  }
  ctx.db.project.id.delete(a.id);
});

// ── uploads (metadata; source videos stay on the worker as pipeline inputs, PRD §10.1) ─────────────
export const put_upload = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), capture_mode: t.string(), filename: t.string(),
    media_type: t.string(), size: t.f64(), sha256: t.string(), status: t.string(), chunk_size: t.u32(),
    chunks_total: t.u32(), chunks_received: t.u32(), meta_json: t.string(), error: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const prev = ctx.db.upload.id.find(a.id);
    if (prev && prev.project_id !== a.project_id) throw new SenderError('upload belongs to another project');
    const { uid, ...row } = a;
    const full = { ...row, created_at: prev ? prev.created_at : now(ctx), updated_at: now(ctx) };
    if (prev) ctx.db.upload.id.update(full); else ctx.db.upload.insert(full);
  });

export const delete_upload = spacetimedb.reducer({ uid: t.string(), project_id: t.string(), id: t.string() }, (ctx, a) => {
  access(ctx, a.uid, a.project_id, true);
  const u = ctx.db.upload.id.find(a.id);
  if (u && u.project_id === a.project_id) ctx.db.upload.id.delete(a.id);
});

// ── pipeline runs ────────────────────────────────────────────────────────────
export const put_run = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), kind: t.string(), status: t.string(), stage: t.string(),
    stages_json: t.string(), progress: t.f64(), error: t.string(), input_json: t.string(), output_json: t.string(),
    log_tail: t.string(), commit: t.string(), attempt: t.u32(), started_at: t.f64(), finished_at: t.f64() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const prev = ctx.db.pipeline_run.id.find(a.id);
    if (prev && prev.project_id !== a.project_id) throw new SenderError('run belongs to another project');
    const { uid, ...row } = a;
    const full = { ...row, created_at: prev ? prev.created_at : now(ctx), updated_at: now(ctx) };
    if (prev) ctx.db.pipeline_run.id.update(full); else ctx.db.pipeline_run.insert(full);
  });

// ── immutable versions ───────────────────────────────────────────────────────
export const insert_asset_version = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), object_name: t.string(), status: t.string(),
    measurements_json: t.string(), properties_json: t.string(), params_json: t.string(), sources_json: t.string(),
    run_id: t.string(), parent_id: t.string(), manifest_id: t.string() },
  (ctx, a) => {
    const p = access(ctx, a.uid, a.project_id, true);
    if (ctx.db.asset_version.id.find(a.id)) throw new SenderError('asset versions are immutable');
    let version = 0;
    for (const v of ctx.db.asset_version.project_id.filter(a.project_id)) version = Math.max(version, v.version);
    const { uid, ...row } = a;
    ctx.db.asset_version.insert({ ...row, version: version + 1, review_json: '{}', created_at: now(ctx), updated_at: now(ctx) });
    touch(ctx, p);
  });

// Review outcome only (accepted / rejected / needs_review): parameters and files of a version never change.
export const review_asset_version = spacetimedb.reducer(
  { uid: t.string(), project_id: t.string(), id: t.string(), status: t.string(), review_json: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const v = ctx.db.asset_version.id.find(a.id);
    if (!v || v.project_id !== a.project_id) throw new SenderError('asset version not found');
    if (!['needs_review', 'accepted', 'rejected', 'failed'].includes(a.status)) throw new SenderError('bad status');
    ctx.db.asset_version.id.update({ ...v, status: a.status, review_json: a.review_json, updated_at: now(ctx) });
  });

export const insert_scene_version = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), asset_version_ids: t.string(), robot_profile: t.string(),
    params_json: t.string(), validation_json: t.string(), manifest_id: t.string(), run_id: t.string(), status: t.string() },
  (ctx, a) => {
    const p = access(ctx, a.uid, a.project_id, true);
    if (ctx.db.scene_version.id.find(a.id)) throw new SenderError('scene versions are immutable');
    let version = 0;
    for (const v of ctx.db.scene_version.project_id.filter(a.project_id)) version = Math.max(version, v.version);
    const { uid, ...row } = a;
    ctx.db.scene_version.insert({ ...row, version: version + 1, created_at: now(ctx) });
    touch(ctx, p);
  });

export const insert_simulation_run = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), scene_version_id: t.string(), mode: t.string(),
    seed: t.f64(), controls_json: t.string(), metrics_json: t.string(), result: t.string(), manifest_id: t.string(),
    engine_json: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const s = ctx.db.scene_version.id.find(a.scene_version_id);
    if (!s || s.project_id !== a.project_id) throw new SenderError('scene version not found');
    const { uid, ...row } = a;
    ctx.db.simulation_run.insert({ ...row, created_at: now(ctx) });
  });

export const put_export = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), scene_version_id: t.string(), asset_version_id: t.string(),
    package_version: t.string(), env_id: t.string(), status: t.string(), validation_json: t.string(),
    dependency_json: t.string(), manifest_id: t.string(), archive_file_id: t.string(), run_id: t.string() },
  (ctx, a) => {
    const p = access(ctx, a.uid, a.project_id, true);
    const prev = ctx.db.gym_export.id.find(a.id);
    if (prev && prev.project_id !== a.project_id) throw new SenderError('export belongs to another project');
    if (prev && prev.status === 'ready') throw new SenderError('a ready export is immutable');
    const { uid, ...row } = a;
    const full = { ...row, created_at: prev ? prev.created_at : now(ctx), updated_at: now(ctx) };
    if (prev) ctx.db.gym_export.id.update(full); else ctx.db.gym_export.insert(full);
    touch(ctx, p);
  });

// ── artifacts: manifests, files, 512 KiB chunks, read-back verification ───────
export const begin_manifest = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), project_id: t.string(), kind: t.string(), ref_id: t.string(), version: t.string(),
    dependency_json: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    if (ctx.db.artifact_manifest.id.find(a.id)) throw new SenderError('manifest exists');
    const { uid, ...row } = a;
    ctx.db.artifact_manifest.insert({ ...row, status: 'writing', file_count: 0, total_bytes: 0, files_json: '[]',
      created_at: now(ctx), verified_at: 0 });
  });

export const begin_file = spacetimedb.reducer(
  { uid: t.string(), id: t.string(), manifest_id: t.string(), project_id: t.string(), path: t.string(),
    media_type: t.string(), size: t.f64(), sha256: t.string(), chunk_size: t.u32(), chunk_count: t.u32() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const m = ctx.db.artifact_manifest.id.find(a.manifest_id);
    if (!m || m.project_id !== a.project_id) throw new SenderError('manifest not found');
    if (m.status !== 'writing') throw new SenderError('manifest is sealed');
    const prev = ctx.db.artifact_file.id.find(a.id);
    if (prev) {  // retry of an interrupted write: start the file over, never duplicate chunks
      for (const c of [...ctx.db.artifact_chunk.file_id.filter(a.id)]) ctx.db.artifact_chunk.id.delete(c.id);
      ctx.db.artifact_file.id.delete(a.id);
    }
    const { uid, ...row } = a;
    ctx.db.artifact_file.insert({ ...row, status: 'writing', created_at: now(ctx), verified_at: 0 });
  });

export const put_chunk = spacetimedb.reducer(
  { uid: t.string(), project_id: t.string(), file_id: t.string(), idx: t.u32(), data: t.array(t.u8()) },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const f = ctx.db.artifact_file.id.find(a.file_id);
    if (!f || f.project_id !== a.project_id) throw new SenderError('file not found');
    if (f.status !== 'writing') throw new SenderError('file is sealed');
    if (a.idx >= f.chunk_count) throw new SenderError('chunk index out of range');
    if (a.data.length > 1048576) throw new SenderError('chunk too large');
    const id = a.file_id + ':' + a.idx;
    ctx.db.artifact_chunk.id.delete(id);  // idempotent retry
    ctx.db.artifact_chunk.insert({ id, file_id: a.file_id, idx: a.idx, data: a.data });
  });

// Called by the gateway only after it has read every chunk back and the reassembled SHA-256 matched.
export const mark_file_verified = spacetimedb.reducer(
  { uid: t.string(), project_id: t.string(), file_id: t.string(), sha256: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const f = ctx.db.artifact_file.id.find(a.file_id);
    if (!f || f.project_id !== a.project_id) throw new SenderError('file not found');
    if (f.sha256 !== a.sha256) throw new SenderError('hash mismatch: file not verified');
    let n = 0;
    for (const c of ctx.db.artifact_chunk.file_id.filter(a.file_id)) n++;
    if (n !== f.chunk_count) throw new SenderError('missing chunks: ' + n + '/' + f.chunk_count);
    ctx.db.artifact_file.id.update({ ...f, status: 'verified', verified_at: now(ctx) });
  });

export const seal_manifest = spacetimedb.reducer(
  { uid: t.string(), project_id: t.string(), id: t.string(), files_json: t.string() },
  (ctx, a) => {
    access(ctx, a.uid, a.project_id, true);
    const m = ctx.db.artifact_manifest.id.find(a.id);
    if (!m || m.project_id !== a.project_id) throw new SenderError('manifest not found');
    let count = 0, bytes = 0;
    for (const f of ctx.db.artifact_file.manifest_id.filter(a.id)) {
      if (f.status !== 'verified') throw new SenderError('unverified file: ' + f.path);
      count++; bytes += f.size;
    }
    ctx.db.artifact_manifest.id.update({ ...m, status: 'verified', file_count: count, total_bytes: bytes,
      files_json: a.files_json, verified_at: now(ctx) });
  });

export const fail_manifest = spacetimedb.reducer({ uid: t.string(), project_id: t.string(), id: t.string() }, (ctx, a) => {
  access(ctx, a.uid, a.project_id, true);
  const m = ctx.db.artifact_manifest.id.find(a.id);
  if (m && m.project_id === a.project_id && m.status === 'writing') ctx.db.artifact_manifest.id.update({ ...m, status: 'failed' });
});

export const delete_file = spacetimedb.reducer({ uid: t.string(), project_id: t.string(), file_id: t.string() }, (ctx, a) => {
  access(ctx, a.uid, a.project_id, true);
  const f = ctx.db.artifact_file.id.find(a.file_id);
  if (!f || f.project_id !== a.project_id) return;
  for (const c of [...ctx.db.artifact_chunk.file_id.filter(a.file_id)]) ctx.db.artifact_chunk.id.delete(c.id);
  ctx.db.artifact_file.id.delete(a.file_id);
});

export const delete_manifest = spacetimedb.reducer({ uid: t.string(), project_id: t.string(), id: t.string() }, (ctx, a) => {
  access(ctx, a.uid, a.project_id, true);
  for (const f of ctx.db.artifact_file.manifest_id.filter(a.id)) throw new SenderError('delete its files first');
  const m = ctx.db.artifact_manifest.id.find(a.id);
  if (m && m.project_id === a.project_id) ctx.db.artifact_manifest.id.delete(a.id);
});

// Access-checked read of one chunk's bytes (the gateway streams downloads through this).
export const read_chunk = spacetimedb.procedure(
  { uid: t.string(), project_id: t.string(), file_id: t.string(), idx: t.u32() },
  t.array(t.u8()),
  (ctx, a) => ctx.withTx((tx: any) => {
    access(tx, a.uid, a.project_id, false);
    const f = tx.db.artifact_file.id.find(a.file_id);
    if (!f || f.project_id !== a.project_id) throw new SenderError('file not found');
    const c = tx.db.artifact_chunk.id.find(a.file_id + ':' + a.idx);
    if (!c) throw new SenderError('chunk not found');
    return c.data;
  }));

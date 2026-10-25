import * as THREE from 'three@0.160.0';

/* RealiSIM: capture-to-simulation pipeline. Original single-object path (Capture → Review → Simulation → Export) and the guided
   workspace-video path. Data lives in the workspace backend (ingress "photo2grasp", kept for compatibility) over SpacetimeDB;
   nothing about projects lives in Firebase. */
const FONT = '"Inter", ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif';
const MONO = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const DARK_C = {
  bg: '#0B0C0E', panel: '#111316', raised: '#15181C', sunken: '#0D0F12', hover: 'rgba(255,255,255,0.035)',
  line: 'rgba(255,255,255,0.07)', line2: 'rgba(255,255,255,0.12)', text: '#E8E9EB', text2: '#A3A7AF', text3: '#6C717A',
  accent: '#F0905F', accentInk: '#1B0F09', accentSoft: 'rgba(240,144,95,0.12)', accentLine: 'rgba(240,144,95,0.45)',
  ok: '#63BA8D', okSoft: 'rgba(99,186,141,0.11)', warn: '#E0B252', warnSoft: 'rgba(224,178,82,0.11)',
  err: '#EE7C7C', errSoft: 'rgba(238,124,124,0.11)', stageTop: '#171A1F', stageBot: '#0A0B0D', code: '#0D0F12',
};
const LIGHT_C = { bg:'#F8F8F6',panel:'#FFFFFF',raised:'#F1F1EE',sunken:'#F4F4F1',hover:'rgba(24,24,27,0.035)',line:'rgba(24,24,27,0.08)',line2:'rgba(24,24,27,0.16)',text:'#202124',text2:'#575B61',text3:'#73777F',accent:'#B94F30',accentInk:'#FFFFFF',accentSoft:'rgba(185,79,48,0.08)',accentLine:'rgba(185,79,48,0.36)',ok:'#25744B',okSoft:'rgba(37,116,75,0.08)',warn:'#8A5D0B',warnSoft:'rgba(138,93,11,0.08)',err:'#B03835',errSoft:'rgba(176,56,53,0.08)',stageTop:'#171A1F',stageBot:'#0A0B0D',code:'#F1F1EE'};
const C=Object.fromEntries(Object.keys(DARK_C).map(k=>[k,`var(--r2r-${k})`]));
const colorVars=palette=>Object.fromEntries(Object.entries(palette).map(([k,v])=>[`--r2r-${k}`,v]));

const HULLS = ['#F0905F', '#E9B872', '#D9707E', '#C9A06A', '#E07B5A', '#EDA59B', '#B98767', '#F2C08E'];
const SOURCES = [
  ['measured', 'Measured', C.ok], ['supplied', 'Supplied', C.text2], ['estimated', 'Estimated', C.warn], ['unknown', 'Unknown', C.err],
];
const SRC = Object.fromEntries(SOURCES.map(([k, l, c]) => [k, { label: l, color: c }]));
const PRD_STAGES = [
  ['validate', 'Upload validation'], ['frames', 'Frame extraction'], ['mesh', 'Mesh generation'], ['asset', 'Asset build'],
  ['review', 'Property review'], ['scene', 'Scene build'], ['simulate', 'Simulation validation'], ['export', 'Gymnasium export'],
];

const ICONS = {
  plus: 'M12 5v14M5 12h14', grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  film: 'M4 5h16v14H4zM8 5v14M16 5v14M4 9h4M4 15h4M16 9h4M16 15h4', camera: 'M4 8h3l2-3h6l2 3h3v11H4zM12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z',
  cube: 'M12 3.5 19.5 7.75v8.5L12 20.5 4.5 16.25v-8.5zM4.5 7.75 12 12l7.5-4.25M12 12v8.5',
  play: 'M8 5.5v13l10.5-6.5z', pause: 'M8 5h3v14H8zM13 5h3v14h-3z', reset: 'M4 12a8 8 0 1 0 2.4-5.7M4 4v4.5h4.5',
  focus: 'M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4M12 10v4M10 12h4', download: 'M12 4v12m0 0 4-4m-4 4-4-4M5 20h14',
  sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', x: 'M6 6l12 12M18 6 6 18', check: 'M5 12.5l4.5 4.5L19 7.5',
  alert: 'M12 8v5M12 16.5v.5M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5', layers: 'M12 4 3 9l9 5 9-5zM3 14l9 5 9-5',
  db: 'M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zM5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3',
  box: 'M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10', settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12l2-1-1-3-2 .2-1.3-1.3.2-2-3-1-1 2h-1.8l-1-2-3 1 .2 2L6 7.2 4 7l-1 3 2 1v1.8l-2 1 1 3 2-.2 1.3 1.3-.2 2 3 1 1-2h1.8l1 2 3-1-.2-2 1.3-1.3 2 .2 1-3-2-1z',
  sim: 'M5 19V9l7-5 7 5v10M9 19v-5h6v5', review: 'M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6zM12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
  upload: 'M12 16V4m0 0-4 4m4-4 4 4M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4', branch: 'M6 4v10M6 14a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 8c0 5-6 4-12 6',
  chevron: 'M9 6l6 6-6 6', back: 'M15 6l-6 6 6 6', lock: 'M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3', retry: 'M20 11a8 8 0 0 0-14.6-4.5M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5M20 20v-4h-4',
  trash: 'M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13', table: 'M3 9h18M5 9v10M19 9v10M3 9l2-4h14l2 4', user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20c1-4 4-6 8-6s7 2 8 6',
  dice: 'M5 5h14v14H5zM9 9h.01M15 15h.01M15 9h.01M9 15h.01', archive: 'M4 5h16v4H4zM5 9v10h14V9M10 13h4',
  sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z', menu: 'M4 7h16M4 12h16M4 17h16', activity: 'M3 12h4l3-7 4 14 3-7h4',
  assistant: 'M5 5h14v10H9l-4 4z', folder: 'M3 6.5h6l2 2h10V19H3z',
};
function Icon({ name, size = 16, color = 'currentColor', sw = 1.6, fill = 'none', style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill} stroke={color} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" style={{ flex: 'none', ...style }} aria-hidden="true">
      <path d={ICONS[name] || ''} />
    </svg>
  );
}

const fmtBytes = (n) => n == null ? '—' : n < 1024 ? Math.round(n) + ' B' : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';
function fmtAgo(ms) {
  if (!ms) return '—';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
const fmtDur = (s) => s == null || !isFinite(s) ? '' : s < 60 ? Math.round(s) + 's' : Math.floor(s / 60) + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's';
const short = (h, n = 10) => (h || '').slice(0, n);
const slugify = (s) => String(s || '').toLowerCase().replace(/\.[a-z0-9]+$/, '').replace(/[^a-z0-9]+/g, '_').replace(/^[^a-z]+|_+$/g, '').slice(0, 40);
const isVideoName = (n) => /\.(mp4|mov|m4v|webm|avi|mkv)$/i.test(n);

/* ── API (Firebase ID token → ingress forward-auth → X-Sender-Uid) ─────────────────────────────── */
function makeApi(base) {
  async function req(path, opts = {}) {
    if (!base) throw new Error('backend not connected');
    const token = await FS.auth.currentUser.getIdToken();
    const headers = { Authorization: 'Bearer ' + token, ...(opts.headers || {}) };
    let body = opts.body;
    if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json); }
    const r = await fetch(base + path, { method: opts.method || (body ? 'POST' : 'GET'), headers, body, signal: opts.signal, keepalive: !!opts.keepalive });
    const fail = (j) => { const e = new Error((j && j.error) || 'HTTP ' + r.status); e.status = r.status; e.code = j && j.code; e.body = j; return e; };   // callers branch on code (ack_required, maintenance_hold); never auto-retried
    if (opts.raw) { if (!r.ok) throw fail(await r.json().catch(() => ({}))); return r; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw fail(j);
    return j;
  }
  return { req, base };
}

const BLOBS = new Map(); // file id -> Promise<Blob>; artifact files are immutable (content-addressed by id + sha256)
function fileBlob(api, pid, fid) {
  if (!fid) return Promise.reject(new Error('missing file'));
  if (!BLOBS.has(fid)) {
    const p = api.req(`/api/projects/${pid}/files/${fid}`, { raw: true }).then((r) => r.blob());
    p.catch(() => BLOBS.delete(fid));
    BLOBS.set(fid, p);
  }
  return BLOBS.get(fid);
}
const URLS = new Map();
function useFileUrl(api, pid, fid) {
  const [url, setUrl] = React.useState(fid && URLS.get(fid));
  React.useEffect(() => {
    if (!fid || !api.base) return;
    if (URLS.has(fid)) { setUrl(URLS.get(fid)); return; }
    let off = false;
    fileBlob(api, pid, fid).then((b) => { const u = URL.createObjectURL(b); URLS.set(fid, u); if (!off) setUrl(u); }).catch(() => {});
    return () => { off = true; };
  }, [fid, api.base, pid]);
  return url;
}
async function saveFile(api, pid, fid, name) {
  const b = await fileBlob(api, pid, fid);
  const u = URL.createObjectURL(b);
  const a = document.createElement('a'); a.href = u; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 4000);
}
async function saveZip(api, pid, mid, prefix, name) {
  const r = await api.req(`/api/projects/${pid}/manifests/${mid}/zip?prefix=${encodeURIComponent(prefix || '')}&name=${encodeURIComponent(name)}`, { raw: true });
  const b = await r.blob();
  const u = URL.createObjectURL(b);
  const a = document.createElement('a'); a.href = u; a.download = name + '.zip'; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 4000);
}
async function sha256Hex(buf) {
  const h = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(h)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

/* Resumable chunked upload. The upload id is a fingerprint of project + file, so selecting the same file again after an
   interruption resumes from the chunks the worker already has; a retried chunk replaces itself, never duplicates. */
async function uploadFile(api, pid, file, mode, onProgress, signal) {
  const head = await file.slice(0, 65536).arrayBuffer();
  const fp = await sha256Hex(new TextEncoder().encode(file.name + '|' + file.size + '|' + file.lastModified + '|').buffer.slice(0));
  const fp2 = await sha256Hex(head);
  // Legacy id (file-only fingerprint) is sent so the server can resume an interrupted pre-fix upload in THIS project only.
  const legacyId = 'up_' + (fp.slice(0, 12) + fp2.slice(0, 8));
  // New id is scoped to the project, so the same file uploaded by different users/projects never collides.
  const fpp = await sha256Hex(new TextEncoder().encode(pid + '|' + file.name + '|' + file.size + '|' + file.lastModified + '|').buffer.slice(0));
  const newId = 'up_' + (fpp.slice(0, 12) + fp2.slice(0, 8));
  let full = '';
  if (file.size <= 400 * 1048576) { onProgress({ phase: 'hashing', frac: 0 }); full = await sha256Hex(await file.arrayBuffer()); }
  const up = await api.req(`/api/projects/${pid}/uploads`, { json: { id: newId, legacy_id: legacyId, filename: file.name, size: file.size, media_type: file.type, capture_mode: mode, sha256: full } });
  const id = up.id || newId; // always address chunks/complete by the row the server returned (may be the resumed legacy row)
  if (up.status === 'ready') { onProgress({ phase: 'done', frac: 1, row: up }); return up; }
  const have = new Set(up.received || []);
  const N = up.chunks_total, CS = up.chunk_size;
  let done = have.size;
  onProgress({ phase: 'uploading', frac: done / N, resumed: have.size > 0 });
  for (let i = 0; i < N; i++) {
    if (have.has(i)) continue;
    if (signal && signal.aborted) throw new Error('paused');
    const buf = await file.slice(i * CS, Math.min(file.size, (i + 1) * CS)).arrayBuffer();
    const h = await sha256Hex(buf);
    let err;
    for (let attempt = 0; attempt < 4; attempt++) {
      try { await api.req(`/api/projects/${pid}/uploads/${id}/chunks/${i}`, { method: 'PUT', body: buf, headers: { 'X-Chunk-Sha256': h, 'Content-Type': 'application/octet-stream' } }); err = null; break; }
      catch (e) { err = e; await new Promise((r) => setTimeout(r, 600 * (attempt + 1))); }
    }
    if (err) throw new Error(`chunk ${i + 1}/${N} failed: ${err.message}`);
    done++;
    onProgress({ phase: 'uploading', frac: done / N });
  }
  onProgress({ phase: 'validating', frac: 1 });
  const row = await api.req(`/api/projects/${pid}/uploads/${id}/complete`, { method: 'POST' });
  onProgress({ phase: row.status === 'ready' ? 'done' : 'invalid', frac: 1, row });
  return row;
}

async function grabFrames(file, n = 3, max = 768) {
  const src = URL.createObjectURL(file);
  try {
    if (!isVideoName(file.name)) {
      const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error('cannot read image')); i.src = src; });
      const s = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement('canvas'); c.width = Math.round(img.naturalWidth * s); c.height = Math.round(img.naturalHeight * s);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      return [c.toDataURL('image/jpeg', 0.82)];
    }
    const v = document.createElement('video');
    v.muted = true; v.playsInline = true; v.preload = 'auto'; v.src = src;
    await new Promise((res, rej) => { v.onloadeddata = res; v.onerror = () => rej(new Error('this browser cannot decode the video')); setTimeout(() => rej(new Error('video load timeout')), 15000); });
    const out = [];
    for (let i = 0; i < n; i++) {
      await new Promise((res) => { const d = () => { v.removeEventListener('seeked', d); res(); }; v.addEventListener('seeked', d); v.currentTime = (v.duration || 1) * (i + 1) / (n + 1); setTimeout(res, 4000); });
      const s = Math.min(1, max / Math.max(v.videoWidth || 1, v.videoHeight || 1));
      const c = document.createElement('canvas'); c.width = Math.round(v.videoWidth * s); c.height = Math.round(v.videoHeight * s);
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
      out.push(c.toDataURL('image/jpeg', 0.82));
    }
    return out;
  } finally { URL.revokeObjectURL(src); }
}
const blobToDataUrl = (b) => new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(b); });

/* ── 3D stage: renders the server-authoritative MuJoCo model (geometry bundle + simulated trajectory) ── */
function buildEnvironment(renderer) {
  const pm = new THREE.PMREMGenerator(renderer);
  const env = new THREE.Scene();
  env.add(new THREE.Mesh(new THREE.BoxGeometry(12, 12, 12), new THREE.MeshBasicMaterial({ color: new THREE.Color(0.32, 0.33, 0.35), side: THREE.BackSide })));
  const panel = (w, h, pos, k) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: new THREE.Color(k, k, k * 1.02), side: THREE.DoubleSide })); m.position.set(...pos); m.lookAt(0, 0, 0); env.add(m); };
  panel(5, 3, [0, 5.5, 0], 6); panel(3, 4, [-5.5, 2, 2], 2.6); panel(3, 4, [5.5, 2.5, -1.5], 1.8); panel(6, 1.2, [0, 1, -5.5], 1.2);
  const tex = pm.fromScene(env, 0.035).texture; pm.dispose(); return tex;
}
function gridTexture() {
  const S = 1024, N = 32, c = document.createElement('canvas'); c.width = c.height = S;
  const g = c.getContext('2d');
  for (let i = 0; i <= N; i++) {
    const p = Math.round(i * S / N) + 0.5, major = i % 5 === 0;
    g.strokeStyle = major ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.045)'; g.lineWidth = major ? 2 : 1;
    g.beginPath(); g.moveTo(p, 0); g.lineTo(p, S); g.stroke(); g.beginPath(); g.moveTo(0, p); g.lineTo(S, p); g.stroke();
  }
  g.globalCompositeOperation = 'destination-in';
  const rg = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
  rg.addColorStop(0, 'rgba(0,0,0,1)'); rg.addColorStop(0.55, 'rgba(0,0,0,0.7)'); rg.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = rg; g.fillRect(0, 0, S, S);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; return t;
}
function createOrbit(dom, camera) {
  const st = { target: new THREE.Vector3(0, 0.07, 0.13), gTarget: new THREE.Vector3(0, 0.07, 0.13), theta: 0.62, phi: 1.12, r: 0.66,
    g: { theta: 0.62, phi: 1.12, r: 0.66 }, pointers: new Map(), mode: null, last: null, pinch: null, lastInteract: 0 };
  const clampPhi = (p) => Math.min(Math.PI / 2 - 0.03, Math.max(0.1, p));
  const down = (e) => { dom.setPointerCapture(e.pointerId); st.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY }); st.mode = (e.button === 2 || e.button === 1 || e.shiftKey) ? 'pan' : 'rotate'; st.last = { x: e.clientX, y: e.clientY }; st.lastInteract = performance.now(); if (st.pointers.size === 2) { const [a, b] = [...st.pointers.values()]; st.pinch = Math.hypot(a.x - b.x, a.y - b.y); st.mode = 'pinch'; } };
  const move = (e) => {
    if (!st.pointers.has(e.pointerId)) return;
    st.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY }); st.lastInteract = performance.now();
    if (st.mode === 'pinch' && st.pointers.size === 2) { const [a, b] = [...st.pointers.values()]; const d = Math.hypot(a.x - b.x, a.y - b.y); if (st.pinch) st.g.r = Math.min(12, Math.max(0.06, st.g.r * st.pinch / d)); st.pinch = d; return; }
    const dx = e.clientX - st.last.x, dy = e.clientY - st.last.y; st.last = { x: e.clientX, y: e.clientY };
    const h = dom.clientHeight || 1;
    if (st.mode === 'rotate') { st.g.theta -= dx / h * 3.2; st.g.phi = clampPhi(st.g.phi - dy / h * 3.2); }
    else if (st.mode === 'pan') { const k = st.r * 1.1 / h; st.gTarget.addScaledVector(new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), -dx * k).addScaledVector(new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1), dy * k); }
  };
  const up = (e) => { st.pointers.delete(e.pointerId); if (st.pointers.size < 2) st.pinch = null; if (!st.pointers.size) st.mode = null; };
  const wheel = (e) => { e.preventDefault(); st.lastInteract = performance.now(); st.g.r = Math.min(12, Math.max(0.06, st.g.r * Math.exp(e.deltaY * 0.0012))); };
  const ctxm = (e) => e.preventDefault();
  dom.addEventListener('pointerdown', down); dom.addEventListener('pointermove', move); dom.addEventListener('pointerup', up); dom.addEventListener('pointercancel', up);
  dom.addEventListener('wheel', wheel, { passive: false }); dom.addEventListener('contextmenu', ctxm);
  return {
    st,
    update(dt, auto) {
      if (auto && !window.matchMedia('(prefers-reduced-motion: reduce)').matches && performance.now() - st.lastInteract > 5000 && !st.pointers.size) st.g.theta += dt * 0.1;
      const k = 1 - Math.pow(0.0008, dt);
      st.theta += (st.g.theta - st.theta) * k; st.phi += (st.g.phi - st.phi) * k; st.r += (st.g.r - st.r) * k; st.target.lerp(st.gTarget, k);
      camera.position.set(st.target.x + st.r * Math.sin(st.phi) * Math.sin(st.theta), st.target.y + st.r * Math.cos(st.phi), st.target.z + st.r * Math.sin(st.phi) * Math.cos(st.theta));
      camera.lookAt(st.target);
    },
    flyTo(t, r, theta, phi) { st.gTarget.copy(t); st.g.r = r; if (theta != null) { const d = theta - st.g.theta; st.g.theta += Math.atan2(Math.sin(d), Math.cos(d)); } if (phi != null) st.g.phi = clampPhi(phi); st.lastInteract = performance.now(); },
    dispose() { dom.removeEventListener('pointerdown', down); dom.removeEventListener('pointermove', move); dom.removeEventListener('pointerup', up); dom.removeEventListener('pointercancel', up); dom.removeEventListener('wheel', wheel); dom.removeEventListener('contextmenu', ctxm); },
  };
}
function geomGeometry(g, meshGeos) {
  const [a, b, c] = g.size;
  if (g.type === 'mesh') return meshGeos[g.mesh] || null;
  if (g.type === 'box') return new THREE.BoxGeometry(2 * a, 2 * b, 2 * c);
  if (g.type === 'sphere') return new THREE.SphereGeometry(a, 32, 16);
  if (g.type === 'ellipsoid') { const s = new THREE.SphereGeometry(1, 32, 16); s.scale(a, b, c); return s; }
  if (g.type === 'capsule') { const s = new THREE.CapsuleGeometry(a, 2 * b, 8, 24); s.rotateX(Math.PI / 2); return s; }
  if (g.type === 'cylinder') { const s = new THREE.CylinderGeometry(a, a, 2 * b, 32); s.rotateX(Math.PI / 2); return s; }
  return null;
}
const mj2three = (p) => new THREE.Vector3(p[0], p[2], -p[1]);

function Viewer({ api, pid, scene, traj, height = 520, badge, footer, compact }) {
  const host = React.useRef(null), ctxRef = React.useRef(null);
  const [status, setStatus] = React.useState('loading');
  const [err, setErr] = React.useState(null);
  const [info, setInfo] = React.useState(null);
  const [tinfo, setTinfo] = React.useState(null);
  const [layers, setLayers] = React.useState({ arm: true, visual: true, collision: false });
  const [playing, setPlaying] = React.useState(false);
  const [speed, setSpeed] = React.useState(1);
  const [t, setT] = React.useState(0);
  const [pose, setPose] = React.useState(null);
  const play = React.useRef({ playing: false, speed: 1, time: 0, scrub: false });
  const sceneKey = scene && scene['scene.json'];
  const trajKey = traj && traj['traj.bin'];
  React.useEffect(() => { play.current.playing = playing; }, [playing]);
  React.useEffect(() => { play.current.speed = speed; }, [speed]);

  React.useEffect(() => {
    if (!sceneKey || !host.current || !api.base) return;
    let disposed = false;
    const el = host.current;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.0;
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;touch-action:none;outline:none;cursor:grab';
    el.appendChild(renderer.domElement);
    const sc = new THREE.Scene();
    sc.environment = buildEnvironment(renderer);
    const camera = new THREE.PerspectiveCamera(32, 1, 0.005, 40);
    const orbit = createOrbit(renderer.domElement, camera);
    sc.add(new THREE.HemisphereLight(0xa9b0bb, 0x16171a, 0.6));
    const key = new THREE.DirectionalLight(0xfff3e8, 2.1); key.position.set(0.7, 1.4, 0.9); key.target.position.set(0, 0, 0.12);
    key.castShadow = true; key.shadow.mapSize.set(2048, 2048); Object.assign(key.shadow.camera, { left: -0.6, right: 0.6, top: 0.6, bottom: -0.6, near: 0.1, far: 4 });
    key.shadow.bias = -0.0004; key.shadow.normalBias = 0.0015; sc.add(key, key.target);
    const rim = new THREE.DirectionalLight(0xdfe7ff, 0.85); rim.position.set(-0.9, 0.6, -0.8); sc.add(rim);
    const grid = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 1.6), new THREE.MeshBasicMaterial({ map: gridTexture(), transparent: true, depthWrite: false }));
    grid.rotation.x = -Math.PI / 2; grid.position.set(0, 0.0002, 0.12); sc.add(grid);
    const shadow = new THREE.Mesh(new THREE.PlaneGeometry(3, 3), new THREE.ShadowMaterial({ opacity: 0.45 }));
    shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.0004; shadow.receiveShadow = true; sc.add(shadow);
    const root = new THREE.Group(); root.rotation.x = -Math.PI / 2; sc.add(root);
    const disposables = [];
    const ctx = { orbit, groups: [], arm: [], obj: [], hulls: [], frames: null, nf: 0, nb: 0, fps: 60, raf: 0 };
    ctxRef.current = ctx;
    const resize = () => { const w = el.clientWidth || 1, h = el.clientHeight || 1; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); };
    const ro = new ResizeObserver(resize); ro.observe(el); resize();
    (async () => {
      try {
        setStatus('loading');
        const [sj, bin] = await Promise.all([
          fileBlob(api, pid, scene['scene.json']).then((b) => b.text()).then(JSON.parse),
          fileBlob(api, pid, scene['meshes.bin']).then((b) => b.arrayBuffer()),
        ]);
        if (disposed) return;
        const texs = await Promise.all((sj.textures || []).map(async (tx) => {
          if (!tx || !scene[tx.file]) return null;
          try { const bmp = await createImageBitmap(await fileBlob(api, pid, scene[tx.file])); const T = new THREE.Texture(bmp); T.flipY = false; T.colorSpace = THREE.SRGBColorSpace; T.anisotropy = 8; T.needsUpdate = true; disposables.push(T); return T; } catch (e) { return null; }
        }));
        if (disposed) return;
        const meshGeos = sj.meshes.map((m) => {
          const geo = new THREE.BufferGeometry();
          geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(bin, m.pos[0], m.pos[1] / 4), 3));
          if (m.nrm) geo.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(bin, m.nrm[0], m.nrm[1] / 4), 3));
          if (m.uv) geo.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(bin, m.uv[0], m.uv[1] / 4), 2));
          if (m.indexed && m.idx) geo.setIndex(new THREE.BufferAttribute(new Uint32Array(bin, m.idx[0], m.idx[1] / 4), 1));
          if (!m.nrm) geo.computeVertexNormals();
          disposables.push(geo); return geo;
        });
        const objBodies = new Set((sj.objects || []).map((o) => o.body));
        sj.bodies.forEach((b, i) => { if (i && objBodies.has(b.parent)) objBodies.add(i); });
        ctx.groups = sj.bodies.map(() => { const g = new THREE.Group(); root.add(g); return g; });
        let hi = 0;
        sj.geoms.forEach((g) => {
          if (g.type === 'plane' || g.group > 3) return;
          const geo = geomGeometry(g, meshGeos); if (!geo) return;
          if (g.type !== 'mesh') disposables.push(geo);
          const isObj = objBodies.has(g.body), coll = g.group === 3;
          if (coll && !isObj) return;
          const mat = g.mat >= 0 ? sj.materials[g.mat] : null, rgba = mat ? mat.rgba : g.rgba;
          let material;
          if (coll) material = new THREE.MeshStandardMaterial({ color: HULLS[hi++ % HULLS.length], transparent: true, opacity: 0.5, roughness: 0.6, depthWrite: false, side: THREE.DoubleSide });
          else {
            const tex = mat && mat.tex >= 0 ? texs[mat.tex] : null;
            const c = new THREE.Color().setRGB(rgba[0], rgba[1], rgba[2], THREE.SRGBColorSpace);
            const lum = (rgba[0] + rgba[1] + rgba[2]) / 3;
            if (!tex && !isObj) { if (lum > 0.92) c.set('#D6D5D0'); else if (lum < 0.15) c.set('#232428'); }
            material = new THREE.MeshStandardMaterial({ color: tex ? 0xffffff : c, map: tex || null, roughness: isObj ? 0.55 : (lum < 0.15 ? 0.42 : 0.5), metalness: 0.03, envMapIntensity: isObj ? 0.9 : 0.65, transparent: rgba[3] < 1, opacity: rgba[3] });
          }
          disposables.push(material);
          const mesh = new THREE.Mesh(geo, material);
          mesh.position.set(g.pos[0], g.pos[1], g.pos[2]); mesh.quaternion.set(g.quat[1], g.quat[2], g.quat[3], g.quat[0]);
          mesh.castShadow = !coll; mesh.receiveShadow = !coll && !isObj;
          if (coll) {
            mesh.renderOrder = 2;
            const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 28), new THREE.LineBasicMaterial({ color: material.color.clone().multiplyScalar(0.75), transparent: true, opacity: 0.9 }));
            disposables.push(edges.geometry, edges.material); mesh.add(edges); ctx.hulls.push(mesh);
          } else if (isObj) ctx.obj.push(mesh); else ctx.arm.push(mesh);
          ctx.groups[g.body].add(mesh);
        });
        ctx.nb = sj.bodies.length; ctx.objectBody = (sj.objects && sj.objects[0] && sj.objects[0].body) || sj.bodies.length - 1;
        ctx.pose0 = sj.pose0 ? new Float32Array(sj.pose0.flat()) : null;
        ctx.setFrame = (arr, f) => {
          if (!arr) return;
          const o = f * ctx.nb * 7;
          for (let b = 0; b < ctx.nb; b++) { const k = o + b * 7; ctx.groups[b].position.set(arr[k], arr[k + 1], arr[k + 2]); ctx.groups[b].quaternion.set(arr[k + 4], arr[k + 5], arr[k + 6], arr[k + 3]); }
        };
        ctx.setFrame(ctx.frames || ctx.pose0, 0);
        const box = new THREE.Box3();
        ctx.obj.forEach((m) => { m.geometry.computeBoundingBox(); const bb = m.geometry.boundingBox.clone(); bb.applyMatrix4(m.matrix); box.union(bb); });
        ctx.objH = Math.max(0.02, box.isEmpty() ? 0.08 : box.max.z - box.min.z);
        const p0 = ctx.pose0 ? [ctx.pose0[ctx.objectBody * 7], ctx.pose0[ctx.objectBody * 7 + 1], 0] : [0, -0.25, 0];
        ctx.focus = () => { const a = ctx.frames || ctx.pose0; const k = ((ctx.frames ? Math.round(play.current.time * ctx.fps) : 0) * ctx.nb + ctx.objectBody) * 7; return { target: mj2three([a[k], a[k + 1], a[k + 2] + ctx.objH * 0.5]), r: Math.max(0.16, ctx.objH * 3.2) }; };
        ctx.home = { target: new THREE.Vector3(p0[0] * 0.5, 0.1, -p0[1] * 0.45), r: 0.86 };
        orbit.st.target.copy(ctx.home.target); orbit.st.gTarget.copy(ctx.home.target); orbit.st.r = 1.1; orbit.st.g.r = compact ? 0.62 : 0.86;
        if(sj.workcell){
          root.updateMatrixWorld(true);const bounds=new THREE.Box3().setFromObject(root);const size=bounds.getSize(new THREE.Vector3()),center=bounds.getCenter(new THREE.Vector3());
          if(!bounds.isEmpty()){ctx.home={target:center,r:Math.max(.7,Math.max(size.x,size.y,size.z)*2.2)};orbit.st.target.copy(center);orbit.st.gTarget.copy(center);orbit.st.r=ctx.home.r;orbit.st.g.r=ctx.home.r;}
          const table=sj.workcell.table;grid.visible=!!table;shadow.visible=!table;
          if(table){grid.scale.set(Math.max(table.length_m,table.width_m)/1.6,Math.max(table.length_m,table.width_m)/1.6,1);grid.position.set(table.center?.[0]||0,.0007,-(table.center?.[1]||0));}
          key.shadow.camera.left=-3;key.shadow.camera.right=3;key.shadow.camera.top=3;key.shadow.camera.bottom=-3;key.shadow.camera.far=12;key.shadow.camera.updateProjectionMatrix();key.position.multiplyScalar(2);
        }
        if (compact) { const f = ctx.focus(); orbit.flyTo(f.target, f.r * 1.4, 0.75, 1.05); }
        setInfo({ hulls: ctx.hulls.length, engine: sj.engine, objects: sj.objects });
        setStatus('ready');
      } catch (e) { if (!disposed) { setErr(e.message || String(e)); setStatus('error'); } }
    })();
    let last = performance.now(), lastUi = 0;
    const loop = (ts) => {
      const dt = Math.min(0.05, (ts - last) / 1000); last = ts;
      const p = play.current;
      if (ctx.frames && ctx.nf) {
        const dur = (ctx.nf - 1) / ctx.fps;
        if (p.playing && !p.scrub) { p.time += dt * p.speed; if (p.time >= dur) { p.time = dur; p.playing = false; setPlaying(false); } }
        const f = Math.min(ctx.nf - 1, Math.max(0, Math.round(p.time * ctx.fps)));
        ctx.setFrame(ctx.frames, f);
        if (ts - lastUi > 60) {
          lastUi = ts; setT(p.time);
          const k = (f * ctx.nb + ctx.objectBody) * 7, a = ctx.frames;
          const yaw = Math.atan2(2 * (a[k + 3] * a[k + 6] + a[k + 4] * a[k + 5]), 1 - 2 * (a[k + 5] * a[k + 5] + a[k + 6] * a[k + 6]));
          setPose({ x: a[k], y: a[k + 1], z: a[k + 2], yaw: yaw * 180 / Math.PI });
        }
      }
      orbit.update(dt, !p.playing);
      renderer.render(sc, camera);
      ctx.raf = requestAnimationFrame(loop);
    };
    ctx.raf = requestAnimationFrame(loop);
    return () => {
      disposed = true; cancelAnimationFrame(ctx.raf); ro.disconnect(); orbit.dispose();
      disposables.forEach((d) => { try { d.dispose(); } catch (e) {} });
      grid.geometry.dispose(); grid.material.map.dispose(); grid.material.dispose(); shadow.geometry.dispose(); shadow.material.dispose();
      if (sc.environment) sc.environment.dispose();
      renderer.dispose(); if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
      ctxRef.current = null;
    };
  }, [sceneKey, api.base]);

  React.useEffect(() => {
    const ctx = ctxRef.current;
    if (!trajKey || !traj || status !== 'ready' || !ctx) { if (!trajKey) setTinfo(null); return; }
    let off = false;
    Promise.all([fileBlob(api, pid, traj['traj.json']).then((b) => b.text()).then(JSON.parse), fileBlob(api, pid, traj['traj.bin']).then((b) => b.arrayBuffer())])
      .then(([ti, buf]) => {
        if (off || !ctxRef.current) return;
        ctx.frames = new Float32Array(buf); ctx.nf = ti.frames; ctx.fps = ti.fps || 60;
        const zs = []; for (let f = 0; f < ctx.nf; f++) zs.push(ctx.frames[(f * ctx.nb + ctx.objectBody) * 7 + 2]);
        setTinfo({ ...ti, zs, duration: (ti.frames - 1) / (ti.fps || 60) });
        play.current.time = 0; play.current.playing = true; setPlaying(true);
      }).catch((e) => setErr('trajectory: ' + e.message));
    return () => { off = true; };
  }, [trajKey, status]);

  React.useEffect(() => { const c = ctxRef.current; if (!c) return; c.arm.forEach((m) => { m.visible = layers.arm; }); c.obj.forEach((m) => { m.visible = layers.visual; }); c.hulls.forEach((m) => { m.visible = layers.collision; }); }, [layers, status]);

  const fly = (which) => { const c = ctxRef.current; if (!c || !c.home) return; if (which === 'obj') { const f = c.focus(); c.orbit.flyTo(f.target, f.r, 0.75, 1.05); } else c.orbit.flyTo(c.home.target, c.home.r, 0.62, 1.12); };
  const toggle = () => { const p = play.current; if (!tinfo) return; if (!playing && p.time >= tinfo.duration - 1e-3) p.time = 0; p.playing = !playing; setPlaying(!playing); };
  const reset = () => { play.current.time = 0; play.current.playing = false; setPlaying(false); setT(0); if (ctxRef.current) ctxRef.current.setFrame(ctxRef.current.frames || ctxRef.current.pose0, 0); };
  const scrub = (x, el) => { if (!tinfo) return; const r = el.getBoundingClientRect(); play.current.time = Math.min(1, Math.max(0, (x - r.left) / r.width)) * tinfo.duration; setT(play.current.time); };
  const chip = (on) => ({ height: 26, padding: '0 9px', borderRadius: 7, fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none',
    border: '1px solid ' + (on ? C.line2 : C.line), background: on ? 'rgba(22,25,29,0.92)' : 'rgba(14,16,19,0.6)', color: on ? C.text : C.text3, backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' });
  let trace = null;
  if (tinfo && tinfo.zs && tinfo.zs.length > 1) {
    const zs = tinfo.zs, n = zs.length, lo = Math.min(...zs), hi = Math.max(...zs), span = Math.max(1e-4, hi - lo);
    const d = zs.map((z, i) => (i ? 'L' : 'M') + (i / (n - 1) * 100).toFixed(2) + ' ' + (26 - (z - lo) / span * 22).toFixed(2)).join(' ');
    trace = { d, area: d + ' L100 30 L0 30 Z', span };
  }
  const frac = tinfo ? Math.min(1, t / Math.max(1e-6, tinfo.duration)) : 0;
  const met = tinfo && tinfo.metrics ? Object.values(tinfo.metrics)[0] : null;
  return (
    <div style={{ ...colorVars(DARK_C), position: 'relative', width: '100%', height, borderRadius: 12, overflow: 'hidden', background: `radial-gradient(120% 90% at 50% 15%, ${C.stageTop} 0%, ${C.stageBot} 100%)`, border: '1px solid ' + C.line }}>
      <div ref={host} role="img" aria-label={'3D MuJoCo scene' + (status === 'ready' ? '. Drag to orbit, shift-drag to pan, scroll or pinch to zoom.' : status === 'error' ? ' failed to load' : ' loading')} style={{ position: 'absolute', inset: 0 }} />
      {status !== 'ready' && (
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 10, color: C.text3, fontSize: 12.5, pointerEvents: 'none' }}>
          {status === 'loading' ? <><div className="p2g-spin" style={{ width: 20, height: 20, borderRadius: 99, border: '2px solid ' + C.line2, borderTopColor: C.accent }} />Loading model…</>
            : <><Icon name="alert" size={18} color={C.err} /><span style={{ maxWidth: 380, textAlign: 'center' }}>Viewer error: {err}</span></>}
        </div>
      )}
      <div style={{ position: 'absolute', top: 10, left: 10, right: 10, display: 'flex', gap: 6, flexWrap: 'wrap', pointerEvents: 'none' }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', pointerEvents: 'auto' }}>
          {[['arm', 'Robot'], ['visual', 'Visual mesh'], ['collision', 'Collision' + (info ? ' · ' + info.hulls : '')]].map(([k, l]) => (
            <span key={k} role="button" tabIndex={0} aria-pressed={!!layers[k]} style={chip(layers[k])} onClick={() => setLayers((s) => ({ ...s, [k]: !s[k] }))} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setLayers((s) => ({ ...s, [k]: !s[k] })); } }}>
              <span style={{ width: 7, height: 7, borderRadius: 2, border: '1.5px solid ' + (k === 'collision' ? C.accent : C.text2), background: layers[k] ? (k === 'collision' ? C.accent : C.text2) : 'transparent' }} />{l}
            </span>
          ))}
        </div>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', gap: 6, pointerEvents: 'auto' }}>
          <span role="button" tabIndex={0} aria-label="Focus camera on the object" style={chip(false)} onClick={() => fly('obj')} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fly('obj'); } }} hidden={!info?.objects?.length}><Icon name="focus" size={13} />Object</span>
          <span role="button" tabIndex={0} aria-label="Frame the whole scene" style={chip(false)} onClick={() => fly('home')} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fly('home'); } }}><Icon name="cube" size={13} />Scene</span>
        </div>
      </div>
      {!compact && status === 'ready' && (
        <div style={{ position: 'absolute', top: 44, right: 10, width: 196, padding: '9px 11px', borderRadius: 9, background: 'rgba(13,15,18,0.78)', border: '1px solid ' + C.line, backdropFilter: 'blur(12px)', WebkitBackdropFilter: 'blur(12px)', fontFamily: MONO, fontSize: 10.5, color: C.text2, lineHeight: 1.75, pointerEvents: 'none' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: C.text3 }}>sim time</span><span>{t.toFixed(2)} s</span></div>
          {pose && <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: C.text3 }}>object x, y</span><span>{(pose.x * 100).toFixed(1)}, {(pose.y * 100).toFixed(1)} cm</span></div>}
          {pose && <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: C.text3 }}>height · yaw</span><span>{(pose.z * 100).toFixed(1)} cm · {pose.yaw.toFixed(0)}°</span></div>}
          {met && <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: C.text3 }}>tilt at rest</span><span>{met.tilt_deg.toFixed(1)}°</span></div>}
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><span style={{ color: C.text3 }}>grid</span><span>5 cm · table z = 0</span></div>
          {badge}
        </div>
      )}
      {tinfo && (
        <div style={{ position: 'absolute', left: 10, right: 10, bottom: 10, display: 'flex', alignItems: 'center', gap: 10, padding: '7px 10px 7px 7px', borderRadius: 10, background: 'rgba(13,15,18,0.82)', border: '1px solid ' + C.line, backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)' }}>
          <button onClick={toggle} title={playing ? 'Pause' : 'Play'} aria-label={playing ? 'Pause simulation replay' : 'Play simulation replay'} style={{ width: 32, height: 32, borderRadius: 8, border: 'none', background: C.text, color: C.bg, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', flex: 'none' }}>
            <Icon name={playing ? 'pause' : 'play'} size={14} fill="currentColor" sw={0} />
          </button>
          <button onClick={reset} title="Reset to t = 0" aria-label="Reset replay to the start" style={{ width: 28, height: 28, borderRadius: 7, border: '1px solid ' + C.line2, background: 'transparent', color: C.text2, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', flex: 'none' }}><Icon name="reset" size={13} /></button>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: C.text3, marginBottom: 2, fontFamily: MONO }}>
              <span>{tinfo.mode === 'drop-test' ? 'physics drop test · object height' : tinfo.mode}{trace ? ` · ${(trace.span * 100).toFixed(1)} cm range` : ''}</span><span>{t.toFixed(2)} / {tinfo.duration.toFixed(2)} s</span>
            </div>
            <div role="slider" tabIndex={0} aria-label="Replay time" aria-valuemin={0} aria-valuemax={+tinfo.duration.toFixed(2)} aria-valuenow={+t.toFixed(2)} aria-valuetext={t.toFixed(2) + ' seconds'} style={{ position: 'relative', height: 26, cursor: 'ew-resize', touchAction: 'none' }}
              onKeyDown={(e) => { const step = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 0.1 : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -0.1 : e.key === 'Home' ? -1e9 : e.key === 'End' ? 1e9 : 0; if (!step) return; e.preventDefault(); play.current.playing = false; setPlaying(false); play.current.time = Math.min(tinfo.duration, Math.max(0, play.current.time + step)); setT(play.current.time); }}
              onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); play.current.scrub = true; play.current.playing = false; setPlaying(false); scrub(e.clientX, e.currentTarget); }}
              onPointerMove={(e) => { if (play.current.scrub) scrub(e.clientX, e.currentTarget); }} onPointerUp={() => { play.current.scrub = false; }} onPointerCancel={() => { play.current.scrub = false; }}>
              <svg viewBox="0 0 100 30" preserveAspectRatio="none" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', overflow: 'visible' }}>
                {trace && <path d={trace.area} fill={C.accentSoft} />}
                {trace && <path d={trace.d} fill="none" stroke={C.text3} strokeWidth="1.1" vectorEffect="non-scaling-stroke" />}
                {trace && <path d={trace.d} fill="none" stroke={C.accent} strokeWidth="1.7" vectorEffect="non-scaling-stroke" style={{ clipPath: `inset(0 ${100 - frac * 100}% 0 0)` }} />}
              </svg>
              <div style={{ position: 'absolute', top: -2, bottom: -2, left: `calc(${frac * 100}% - 1px)`, width: 2, borderRadius: 2, background: C.accent }} />
            </div>
          </div>
          <div style={{ display: 'flex', gap: 2, flex: 'none' }}>
            {[0.25, 0.5, 1, 2].map((s) => <button key={s} onClick={() => setSpeed(s)} aria-pressed={speed === s} aria-label={'Replay speed ' + s + ' times'} style={{ height: 24, padding: '0 7px', borderRadius: 6, fontSize: 10.5, fontFamily: MONO, cursor: 'pointer', border: '1px solid ' + (speed === s ? C.line2 : 'transparent'), background: speed === s ? C.raised : 'transparent', color: speed === s ? C.text : C.text3 }}>{s}×</button>)}
          </div>
          {met && <span style={{ flex: 'none', display: 'inline-flex', alignItems: 'center', gap: 6, height: 24, padding: '0 9px', borderRadius: 6, fontSize: 11, color: tinfo.result === 'pass' ? C.ok : C.err, background: tinfo.result === 'pass' ? C.okSoft : C.errSoft }}>
            <Icon name={tinfo.result === 'pass' ? 'check' : 'alert'} size={12} sw={2} />{met.upright ? 'Upright' : 'Tipped'} · {met.settled ? 'settled' : 'moving'}</span>}
        </div>
      )}
      {footer}
    </div>
  );
}

/* ── primitives ─────────────────────────────────────────────────────────────── */
/* `processing` marks a button that starts NEW processing; it is disabled with the reason while a maintenance hold is active
   (uploads, cancel, reads, downloads and field edits never pass it). Pass a hold object to use a fresher source. */
function Btn({ kind = 'ghost', icon, children, onClick, disabled, title, small, style, type, ariaLabel, pressed, processing }) {
  const gate = React.useContext(RunDataCtx);
  const hold = processing ? (typeof processing === 'object' ? processing : gate && gate.hold) : null;
  if (hold) { disabled = true; title = hold.label || ('Paused for maintenance' + (hold.reason ? ': ' + hold.reason : '') + '. Saved work is unaffected.'); }
  const k = {
    primary: { background: C.accent, color: C.accentInk, border: '1px solid ' + C.accent },
    solid: { background: C.text, color: C.bg, border: '1px solid ' + C.text },
    ghost: { background: 'transparent', color: C.text2, border: '1px solid ' + C.line2 },
    quiet: { background: 'transparent', color: C.text2, border: '1px solid transparent' },
    danger: { background: 'transparent', color: C.err, border: '1px solid rgba(238,124,124,0.35)' },
  }[kind];
  return (
    <button type={type || 'button'} className="p2g-btn" title={title} aria-label={ariaLabel} aria-pressed={pressed} disabled={disabled} onClick={disabled ? undefined : onClick}
      style={{ minHeight: small ? 28 : 34, height: 'auto', maxWidth: '100%', minWidth: 0, padding: small ? '4px 10px' : '6px 14px', lineHeight: 1.4, textAlign: 'center', borderRadius: 999, fontSize: small ? 12 : 13, fontWeight: 500, fontFamily: FONT,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 7, cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.42 : 1,
        whiteSpace: 'normal', transition: 'background .15s, border-color .15s, color .15s', ...k, ...style }}>
      {icon && <Icon name={icon} size={small ? 13 : 14} />}{children}
    </button>
  );
}
const inputCss = (bad, mono) => ({ height: 34, borderRadius: 8, border: '1px solid ' + (bad ? C.err : C.line2), background: C.sunken, color: C.text,
  padding: '0 10px', fontSize: 13, fontFamily: mono ? MONO : FONT, outline: 'none', width: '100%', minWidth: 0, boxSizing: 'border-box' });
function Label({ children, hint }) {
  return <div style={{ fontSize: 11.5, color: C.text2, fontWeight: 500, marginBottom: 6, display: 'flex', gap: 8 }}><span>{children}</span>{hint && <span style={{ color: C.text3, fontWeight: 400 }}>{hint}</span>}</div>;
}
function Eyebrow({ children, style }) { return <div style={{ fontSize: 10.5, color: C.text3, textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 500, ...style }}>{children}</div>; }
function Dot({ color, pulse }) { return <span className={pulse ? 'p2g-pulse' : ''} style={{ width: 6, height: 6, borderRadius: 9, background: color, flex: 'none', display: 'inline-block' }} />; }
const STATUS = {
  queued: ['Queued', C.text2], running: ['Running', C.accent], needs_review: ['Needs review', C.warn], failed: ['Failed', C.err],
  cancelled: ['Cancelled', C.text3], complete: ['Complete', C.ok], ready: ['Ready', C.ok], uploading: ['Uploading', C.accent], validating: ['Validating', C.accent],
  invalid: ['Invalid', C.err], accepted: ['Accepted', C.ok], rejected: ['Rejected', C.text3], validated: ['Validated', C.ok],
  validation_failed: ['Validation failed', C.err], building: ['Building', C.accent], pass: ['Pass', C.ok], fail: ['Fail', C.err],
};
function runDisplayStatus(r){if(r?.status==='failed' && r.error==='Cancelled')return 'cancelled';if(r?.status==='failed' && r.error==='Rejected in property review')return 'rejected';return r?.status;}
function Status({ s }) {
  const [l, c] = STATUS[s] || [s || '—', C.text3];
  return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: c, whiteSpace: 'nowrap' }}><Dot color={c} pulse={['running', 'uploading', 'validating', 'building', 'queued'].includes(s)} />{l}</span>;
}
function SourceChip({ source }) {
  const s = SRC[source] || SRC.unknown;
  return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10.5, color: s.color, padding: '2px 7px', borderRadius: 5, border: '1px solid ' + C.line, whiteSpace: 'nowrap' }}><Dot color={s.color} />{s.label}</span>;
}
function Panel({ title, aside, children, style, pad = true }) {
  return (
    <section className="p2g-panel" style={{ background: C.panel, border: '1px solid ' + C.line, borderRadius: 12, minWidth: 0, maxWidth: '100%', containerType: 'inline-size', overflowWrap: 'anywhere', ...style }}>
      {title && <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10, padding: '13px 16px', borderBottom: '1px solid ' + C.line }}>
        <h3 style={{ margin: 0, minWidth: 0, maxWidth: "100%", flex: "1 1 auto", fontSize: 13, fontWeight: 600, color: C.text }}>{title}</h3>{aside}</div>}
      <div style={{ padding: pad ? 16 : 0 }}>{children}</div>
    </section>
  );
}
function Kv({ k, v, mono = true }) {
  return <div className="p2g-kv" style={{ display: 'flex', gap: 12, padding: '6px 0', borderBottom: '1px solid ' + C.line, fontSize: 12.5 }}>
    <span style={{ color: C.text3, flex: '0 0 42%' }}>{k}</span><span style={{ color: C.text, fontFamily: mono ? MONO : FONT, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', wordBreak: 'break-word' }}>{v}</span></div>;
}
function Thumb({ api, pid, fid, size = 44, radius = 8, icon = 'film' }) {
  const url = useFileUrl(api, pid, fid);
  return <div style={{ width: size, height: size, borderRadius: radius, overflow: 'hidden', background: C.raised, border: '1px solid ' + C.line, flex: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
    {url ? <img src={url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : <Icon name={icon} size={Math.round(size * 0.36)} color={C.text3} />}</div>;
}
function Modal({ title, children, onClose, width = 520 }) {
  const panel = React.useRef(null), prev = React.useRef(null);
  React.useEffect(() => {
    prev.current = document.activeElement;
    const el = panel.current; if (el) { const f = el.querySelector('input,select,textarea,button:not([data-modal-close])'); (f || el).focus({ preventScroll: true }); }
    return () => { const p = prev.current; if (p && p.focus && document.contains(p)) p.focus({ preventScroll: true }); };
  }, []);
  const key = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); onClose(); return; }
    if (e.key !== 'Tab' || !panel.current) return;
    const items = [...panel.current.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  return (
    <div onClick={onClose} style={{ position: 'absolute', inset: 0, background: 'rgba(5,6,8,0.55)', zIndex: 40, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}>
      <div ref={panel} role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined} tabIndex={-1} onKeyDown={key} onClick={(e) => e.stopPropagation()} style={{ width: `min(${width}px, 100%)`, maxHeight: '100%', overflow: 'auto', background: C.panel, border: '1px solid ' + C.line2, borderRadius: 14, boxShadow: '0 16px 60px rgba(0,0,0,0.15)', outline: 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', padding: '14px 16px', borderBottom: '1px solid ' + C.line }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{title}</h3><div style={{ flex: 1 }} />
          <button data-modal-close aria-label="Close" onClick={onClose} style={{ border: 'none', background: 'transparent', color: C.text3, cursor: 'pointer', display: 'flex', minWidth: 32, minHeight: 32, alignItems: 'center', justifyContent: 'center', borderRadius: 8 }}><Icon name="x" size={16} /></button>
        </div>
        <div style={{ padding: 16 }}>{children}</div>
      </div>
    </div>
  );
}
function summarizeError(msg) {
  const s = String(msg || '');
  const q = s.match(/exceeded your ZeroGPU quota.*?Try again in ([0-9:]+)/);
  if (q) return `Hugging Face GPU quota for TRELLIS is used up (resets in ${q[1]}). Connect a free Hugging Face token for your own daily quota, then retry.`;
  const lines = s.split('\n').map((l) => l.trim()).filter(Boolean);
  const exc = [...lines].reverse().find((l) => /^[\w.]*(Error|Exception|Exit)\b.*:/.test(l));
  return exc ? exc.replace(/^[\w.]*\.(\w+(Error|Exception))/, '$1') : (lines.length > 3 ? lines[lines.length - 1] : s);
}
function ErrorLine({ msg }) {
  if (!msg) return null;
  const sum = summarizeError(msg);
  return <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12.5, color: C.err, padding: '9px 11px', borderRadius: 8, background: C.errSoft, lineHeight: 1.5 }}>
    <Icon name="alert" size={14} style={{ marginTop: 2 }} /><div style={{ flex: 1, minWidth: 0 }}><div>{sum}</div>
    {sum !== String(msg) && <details style={{ marginTop: 6 }}><summary style={{ cursor: 'pointer', fontSize: 11.5, color: C.text3 }}>Details</summary><pre style={{ margin: '6px 0 0', fontFamily: MONO, fontSize: 10.5, color: C.text2, whiteSpace: 'pre-wrap', maxHeight: 180, overflow: 'auto', userSelect: 'text' }}>{msg}</pre></details>}</div></div>;
}

/* ── dashboard ───────────────────────────────────────────────────────────────── */
function projectPackage(p){const w=p.settings_json?.workflow;if(w?.build?.phase==='exported'&&w.build.export)return {...w.build.export,status:'ready'};return p.export;}
/* Dashboard: see workflow-root.jsx (RealiSIM pipeline overview, real aggregate activity and resumable projects). */

function NewProject({api,system,onClose,onCreated}){
 const [name,setName]=React.useState(''),[mode,setMode]=React.useState('workspace'),[busy,setBusy]=React.useState(false),[err,setErr]=React.useState('');
 const create=async()=>{if(busy)return;setBusy(true);setErr('');try{const r=await api.req('/api/workflow/projects',{json:{name:name.trim()||(mode==='workspace'?'New workspace':'New object'),mode}});onCreated(r.id);}catch(e){setErr(e.message)}finally{setBusy(false)}};
 return <Modal title="Add project" onClose={onClose} width={620}><div style={{fontSize:14,color:C.text2,lineHeight:1.6,marginBottom:18}}>What are you converting?</div><div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(210px,1fr))',gap:12}}>{[['workspace','Workspace video','Convert a table and its objects into a scene.','layers'],['object','Single object','Create one reusable simulation asset.','cube']].map(([id,title,desc,icon])=><button key={id} onClick={()=>setMode(id)} aria-pressed={mode===id} style={{textAlign:'left',border:'1px solid '+(mode===id?C.accentLine:C.line2),background:mode===id?C.accentSoft:C.panel,color:C.text,borderRadius:12,padding:18,fontFamily:FONT,cursor:'pointer'}}><div style={{display:'flex',alignItems:'center',gap:10}}><Icon name={icon} size={20}/><span style={{marginLeft:'auto',color:C.accent}}>{mode===id&&<Icon name="check" size={16}/>}</span></div><div style={{fontSize:15,fontWeight:600,marginTop:16}}>{title}</div><p style={{fontSize:12.5,color:C.text2,lineHeight:1.6,margin:'7px 0 0'}}>{desc}</p>{id==='workspace'&&<div style={{fontSize:11,color:C.accent,marginTop:10}}>Primary workflow</div>}</button>)}</div><div style={{marginTop:20}}><Label>Project name <span style={{color:C.text3}}>(optional)</span></Label><input aria-label="Project name" value={name} maxLength={120} onChange={e=>setName(e.target.value)} onKeyDown={e=>e.key==='Enter'&&create()} placeholder={mode==='workspace'?'Assembly bench A':'Ceramic mug'} style={inputCss()}/></div><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Upload first. Claude proposes objects and properties afterwards; every value stays editable and labelled with its source before anything is built.</p><ErrorLine msg={err}/><div style={{display:'flex',gap:8,justifyContent:'flex-end',marginTop:18}}><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" icon="chevron" disabled={busy} onClick={create}>{busy?'Creating…':'Continue to upload'}</Btn></div></Modal>;
}

/* ── capture wizard ──────────────────────────────────────────────────────────── */
const OBJ_FIELDS = [
  ['length_cm', 'Length', 'cm', true], ['width_cm', 'Width', 'cm', true], ['height_cm', 'Height (standing)', 'cm', true],
  ['mass_g', 'Mass', 'g', true], ['friction', 'Friction on the table', 'μ', true], ['material', 'Material', '', false],
];
const emptyMeas = () => Object.fromEntries(OBJ_FIELDS.map(([k]) => [k, { value: '', source: 'unknown', note: '' }]));

function MeasureRow({ field, m, onChange, narrow }) {
  const [k, label, unit, req] = field;
  const text = k === 'material';
  return (
    <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : '170px 150px 1fr', gap: narrow ? 6 : 12, alignItems: 'center', padding: '10px 0', borderBottom: '1px solid ' + C.line }}>
      <div style={{ fontSize: 12.5, color: C.text }}>{label}{req && <span style={{ color: C.text3 }}> *</span>}</div>
      <div style={{ position: 'relative' }}>
        <input value={m.value} inputMode={text ? 'text' : 'decimal'} placeholder={text ? 'e.g. aluminium' : '—'}
          onChange={(e) => { const v = text ? e.target.value : e.target.value.replace(/[^0-9.]/g, ''); onChange({ ...m, value: v, source: m.source === 'unknown' && v ? 'measured' : (v ? m.source : 'unknown') }); }}
          style={{ ...inputCss(req && m.value !== '' && !text && !(parseFloat(m.value) > 0), !text), paddingRight: unit ? 34 : 10 }} />
        {unit && <span style={{ position: 'absolute', right: 10, top: 9, fontSize: 11.5, color: C.text3, fontFamily: MONO }}>{unit}</span>}
      </div>
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
        {SOURCES.map(([s, l, c]) => (
          <button key={s} onClick={() => onChange({ ...m, source: s })} title={s === 'estimated' ? 'An estimate, never treated as a measurement' : ''}
            style={{ height: 26, padding: '0 9px', borderRadius: 6, fontSize: 11.5, cursor: 'pointer', fontFamily: FONT, display: 'inline-flex', alignItems: 'center', gap: 5,
              border: '1px solid ' + (m.source === s ? C.line2 : 'transparent'), background: m.source === s ? C.raised : 'transparent', color: m.source === s ? C.text : C.text3 }}>
            <Dot color={m.source === s ? c : C.text3} />{l}</button>
        ))}
        {m.note && <details style={{ fontSize: 11, color: C.text3, marginLeft: 4, maxWidth: 260 }}><summary style={{ cursor: 'pointer' }} aria-label="Source note">Note</summary><div style={{ marginTop: 3, lineHeight: 1.5, overflowWrap: 'anywhere' }}>{m.note}</div></details>}
      </div>
    </div>
  );
}

function CaptureWizard({ api, pid, detail, system, narrow, onStarted, canSuggest = true, refreshSystem, singleObjectOnly=false }) {
  const [mode, setMode] = React.useState('object');
  const [items, setItems] = React.useState([]);      // local files being uploaded in this session
  const [sel, setSel] = React.useState([]);          // upload ids used for this run
  const [meas, setMeas] = React.useState(emptyMeas);
  const [name, setName] = React.useState('');
  const [tags, setTags] = React.useState({ front: '', back: '', height: '' });
  const [table, setTable] = React.useState({ length_cm: '', width_cm: '', height_cm: '' });
  const [drag, setDrag] = React.useState(false);
  const [ai, setAi] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const fileRef = React.useRef(null);
  const uploads = (detail && detail.upload) || [];
  const lim = (system && system.limits) || {};
  React.useEffect(() => {  // preselect captures that are already uploaded and validated for this mode
    setSel((s) => s.length ? s : uploads.filter((u) => u.status === 'ready' && u.capture_mode === mode).map((u) => u.id));
  }, [uploads.length, mode]);
  const exts = [...(lim.video_ext || []), ...(lim.image_ext || [])];

  const addFiles = (list) => {
    const arr = [...list];
    arr.forEach((file) => {
      const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
      const key = Math.random().toString(36).slice(2);
      if (exts.length && !exts.includes(ext)) { setItems((s) => [...s, { key, file, phase: 'invalid', error: `unsupported type ${ext}` }]); return; }
      if (lim.upload_bytes && file.size > lim.upload_bytes) { setItems((s) => [...s, { key, file, phase: 'invalid', error: `larger than ${fmtBytes(lim.upload_bytes)}` }]); return; }
      setItems((s) => [...s, { key, file, phase: 'queued', frac: 0 }]);
      if (!name && mode === 'object') setName((n) => n || slugify(file.name.replace(/whatsapp|video|img|vid|\d{4}-\d{2}-\d{2}|at|\d+\.\d+\.\d+/gi, '')) || 'object_1');
      run(key, file);
    });
  };
  const run = async (key, file) => {
    const upd = (p) => setItems((s) => s.map((x) => x.key === key ? { ...x, ...p } : x));
    try {
      const row = await uploadFile(api, pid, file, mode, (p) => upd({ phase: p.phase, frac: p.frac, resumed: p.resumed || undefined, row: p.row }));
      upd({ phase: row.status === 'ready' ? 'done' : 'invalid', row, error: row.error });
      if (row.status === 'ready') setSel((s) => s.includes(row.id) ? s : [...s, row.id]);
    } catch (e) { upd({ phase: 'error', error: e.message }); }
  };

  const suggest = async () => {
    const local = items.find((x) => x.file && x.phase === 'done');
    setAi({ status: 'working' }); setErr(null);
    try {
      // Fixed server-side model, prompt and quota: the browser sends only up to 3 still frames (≤600 KB each) or an upload id.
      const approxBytes = (u) => Math.floor((u.length - u.indexOf(',') - 1) * 3 / 4);
      let frames = [];
      if (local) {
        frames = await grabFrames(local.file, 3, 768);
        if (frames.some((f) => approxBytes(f) > 600000)) frames = await grabFrames(local.file, 3, 512);
        frames = frames.filter((f) => /^data:image\/(jpeg|png|webp);base64,/.test(f) && approxBytes(f) <= 600000).slice(0, 3);
      }
      const u = uploads.find((x) => sel.includes(x.id));
      if (!frames.length && !u) throw new Error('upload a video first');
      const res = await api.req(`/api/projects/${pid}/suggest-fields`, { json: frames.length ? { images: frames } : { upload_id: u.id } });
      const a = res.suggestion || {};
      const nFrames = frames.length || 1;
      const note = `${res.model ? 'Claude (' + res.model + ')' : 'Claude'} visual estimate from ${frames.length ? nFrames + ' frame' + (nFrames > 1 ? 's' : '') : 'the upload thumbnail'}: ${a.name || 'object'} (${a.confidence || 'unrated'} confidence)`;
      setMeas((m) => {
        const out = { ...m };
        for (const k of ['length_cm', 'width_cm', 'height_cm', 'mass_g', 'friction', 'material']) {
          if (a[k] == null || a[k] === '') continue;
          if (m[k].value !== '' && m[k].source !== 'estimated') continue;   // never overwrite a measured or supplied value
          out[k] = { value: typeof a[k] === 'number' ? String(+a[k].toFixed(k === 'friction' ? 2 : 1)) : String(a[k]), source: 'estimated', note };
        }
        return out;
      });
      if (!name && a.slug) setName(slugify(a.slug));
      setAi({ status: 'done', name: a.name, confidence: a.confidence, note: a.note });
    } catch (e) { setAi({ status: 'error', error: e.message }); }
  };

  const objectsKnown = []; // the repo's measured objects are deliberately not offered (fresh workspace)
  const probs = [];
  if (!sel.length) probs.push('upload at least one validated capture');
  if (mode === 'object') {
    if (!/^[a-z][a-z0-9_]{1,39}$/.test(name)) probs.push('object name: lowercase letters, digits, _ (starts with a letter)');
    for (const [k, l, , req] of OBJ_FIELDS) {
      if (!req) continue;
      const m = meas[k];
      if (m.value === '' || !(parseFloat(m.value) > 0)) probs.push(`${l.toLowerCase()} is missing`);
      else if (m.source === 'unknown') probs.push(`${l.toLowerCase()}: choose where the value comes from`);
    }
  } else if (!['length_cm', 'width_cm', 'height_cm'].every((k) => parseFloat(table[k]) > 0)) probs.push('table length, width and height are required for metric scale');
  const estimated = OBJ_FIELDS.filter(([k]) => meas[k].source === 'estimated' && meas[k].value !== '').map(([, l]) => l.toLowerCase());

  const start = async () => {
    setBusy(true); setErr(null);
    try {
      const body = mode === 'object'
        ? { kind: 'asset', upload_ids: sel, object_name: name, measurements: { ...meas, ...(tags.height ? { tag_height_cm: { value: tags.height, source: 'measured' } } : {}) }, tags: { front: tags.front, back: tags.back } }
        : { kind: 'scene_capture', upload_ids: sel, table };
      const r = await api.req(`/api/projects/${pid}/runs`, { json: body });
      onStarted(r.id);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const ready = uploads.filter((u) => u.status === 'ready' && u.capture_mode === mode);
  const step = (n, title, sub, children) => (
    <section style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : '200px 1fr', gap: narrow ? 10 : 28, padding: '24px 0', borderTop: '1px solid ' + C.line }}>
      <div><div style={{ fontFamily: MONO, fontSize: 11, color: C.text3 }}>{n}</div><div style={{ fontSize: 14, fontWeight: 600, marginTop: 4 }}>{title}</div><div style={{ fontSize: 12, color: C.text3, marginTop: 4, lineHeight: 1.5 }}>{sub}</div></div>
      <div style={{ minWidth: 0 }}>{children}</div>
    </section>
  );
  return (
    <div style={{ maxWidth: 980, margin: '0 auto' }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 20, fontWeight: 600, letterSpacing: '-0.015em' }}>New capture</h2>
      <p style={{ margin: '0 0 8px', fontSize: 13, color: C.text2 }}>Upload, add the physical facts, then processing runs in the background.</p>
      {step('01', 'Capture mode', 'What the video shows.', (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : '1fr 1fr', gap: 10 }}>
          {[['object', 'Object capture', 'One object filmed from several sides. Builds one MuJoCo asset next to the robot.', 'camera', false],
            ['scene', 'Tabletop scene', 'The table and objects on it; needs the real table size for scale. Beta: not yet validated end to end.', 'table', true]].filter(x=>!singleObjectOnly||x[0]==='object').map(([k, t, d, ic, beta]) => (
            <button key={k} onClick={() => { setMode(k); setSel([]); }} style={{ textAlign: 'left', padding: 14, borderRadius: 10, cursor: 'pointer', fontFamily: FONT,
              border: '1px solid ' + (mode === k ? C.accentLine : C.line), background: mode === k ? C.accentSoft : C.panel, color: C.text }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 600 }}><Icon name={ic} size={15} color={mode === k ? C.accent : C.text2} />{t}{beta && <span style={{ fontSize: 10, color: C.warn, border: '1px solid ' + C.line, borderRadius: 4, padding: '0 5px', fontWeight: 400 }}>beta</span>}</div>
              <div style={{ fontSize: 12, color: C.text2, marginTop: 6, lineHeight: 1.5 }}>{d}</div>
            </button>
          ))}
        </div>
      ))}
      {step('02', 'Upload', 'Resumable: select the same file again to continue an interrupted upload.', (
        <div>
          <input ref={fileRef} type="file" multiple accept={exts.join(',') || 'video/*,image/*'} style={{ display: 'none' }} onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
          <div onClick={() => fileRef.current && fileRef.current.click()} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files); }}
            style={{ padding: '22px 18px', borderRadius: 10, border: '1.5px dashed ' + (drag ? C.accent : C.line2), background: drag ? C.accentSoft : C.sunken, cursor: 'pointer', display: 'flex', gap: 16, alignItems: 'center' }}>
            <div style={{ width: 40, height: 40, borderRadius: 10, background: C.raised, display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 'none' }}><Icon name="upload" size={18} color={C.text2} /></div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13.5, fontWeight: 500 }}>Drop videos or photos, or tap to choose</div>
              <div style={{ fontSize: 11.5, color: C.text3, marginTop: 3 }}>{(lim.video_ext || []).join(' ')} · {(lim.image_ext || []).join(' ')} · up to {fmtBytes(lim.upload_bytes)} each · sent in {fmtBytes(lim.upload_chunk)} chunks with SHA-256 checks</div>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'repeat(2, 1fr)', gap: 6, margin: '12px 0', fontSize: 12, color: C.text2 }}>
            {(mode === 'object' ? ['Keep the whole object in frame', 'Move slowly and steadily', 'Circle it to show every side', 'Avoid blur and strong glare'] : ['Show the whole tabletop and its edges', 'Keep the floor visible below the table', 'Move slowly with lots of overlap', 'Measure the real table size']).map((g) => (
              <div key={g} style={{ display: 'flex', gap: 8, alignItems: 'center' }}><Icon name="check" size={13} color={C.text3} />{g}</div>
            ))}
          </div>
          {(items.length > 0 || ready.length > 0) && (
            <div style={{ border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden' }}>
              {items.filter((x) => !(x.row && x.phase === 'done')).map((x) => (
                <div key={x.key} style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '10px 12px', borderBottom: '1px solid ' + C.line }}>
                  <div style={{ width: 44, height: 44, borderRadius: 8, background: C.raised, display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 'none' }}><Icon name="film" size={16} color={C.text3} /></div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{x.file.name}</div>
                    <div style={{ fontSize: 11, color: x.error ? C.err : C.text3, marginTop: 2, fontFamily: MONO }}>{fmtBytes(x.file.size)} · {x.error || (x.phase === 'hashing' ? 'computing SHA-256…' : x.phase === 'validating' ? 'decoding frames…' : x.phase === 'uploading' ? `${Math.round((x.frac || 0) * 100)}%${x.resumed ? ' · resumed' : ''}` : x.phase)}</div>
                    {['uploading', 'hashing', 'validating'].includes(x.phase) && <div style={{ height: 3, borderRadius: 3, background: C.line, marginTop: 6, overflow: 'hidden' }}><div style={{ width: `${Math.round((x.frac || 0) * 100)}%`, height: '100%', background: C.accent, transition: 'width .2s' }} /></div>}
                  </div>
                  {(x.phase === 'error') && <Btn small icon="retry" onClick={() => run(x.key, x.file)}>Retry</Btn>}
                  {['invalid', 'error'].includes(x.phase) && <button onClick={() => setItems((s) => s.filter((y) => y.key !== x.key))} style={{ border: 'none', background: 'transparent', color: C.text3, cursor: 'pointer' }}><Icon name="x" size={14} /></button>}
                </div>
              ))}
              {uploads.filter((u) => u.capture_mode === mode).map((u) => {
                const m = u.meta_json || {};
                const on = sel.includes(u.id);
                return (
                  <div key={u.id} style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '10px 12px', borderBottom: '1px solid ' + C.line }}>
                    <Thumb api={api} pid={pid} fid={m.thumb_file_id} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{u.filename}</div>
                      <div style={{ fontSize: 11, color: C.text3, marginTop: 2, fontFamily: MONO }}>{fmtBytes(u.size)}{m.duration ? ` · ${m.duration}s` : ''}{m.width ? ` · ${m.width}×${m.height}` : ''}{m.fps ? ` · ${Math.round(m.fps)} fps` : ''}{u.sha256 ? ` · sha ${short(u.sha256, 8)}` : ''}</div>
                      {u.error && <div style={{ fontSize: 11, color: C.err, marginTop: 2 }}>{u.error}</div>}
                    </div>
                    <Status s={u.status} />
                    {u.status === 'ready' && <label style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: C.text2, cursor: 'pointer' }}><input type="checkbox" checked={on} onChange={() => setSel((s) => on ? s.filter((y) => y !== u.id) : [...s, u.id])} style={{ accentColor: C.accent }} />Use</label>}
                    {u.status === 'uploading' && <span style={{ fontSize: 11, color: C.text3 }}>select the file again to resume</span>}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}
      {mode === 'object' ? step('03', 'Physical details', 'Every value carries its source. Estimates stay estimates until you review them.', (
        <div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 6 }}>
            <div style={{ flex: '1 1 220px' }}><Label hint="becomes the MJCF model and body name">Object name</Label><input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} placeholder="celsius_can" style={inputCss(name && !/^[a-z][a-z0-9_]{1,39}$/.test(name), true)} /></div>
            <Btn icon="sparkle" onClick={suggest} disabled={!sel.length || !canSuggest || (ai && ai.status === 'working')} title={canSuggest ? 'Identify the object and suggest values, labelled as estimates' : 'Suggestions are not available for this account; enter the values manually'}>{ai && ai.status === 'working' ? 'Estimating…' : 'Suggest from video'}</Btn>
          </div>
          {ai && ai.status !== 'working' && <div style={{ fontSize: 12, color: ai.status === 'error' ? C.err : C.text2, margin: '8px 0', lineHeight: 1.5 }}>{ai.status === 'error' ? 'Suggestion failed: ' + ai.error : <>Suggested values for <b style={{ color: C.text, fontWeight: 500 }}>{ai.name}</b> ({ai.confidence} confidence) are marked <span style={{ color: C.warn }}>Estimated</span>. {ai.note} Measured or supplied values were left as they were.</>}</div>}
          <div>{OBJ_FIELDS.map((f) => <MeasureRow key={f[0]} field={f} m={meas[f[0]]} narrow={narrow} onChange={(v) => setMeas((s) => ({ ...s, [f[0]]: v }))} />)}</div>
          <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr 1fr' : 'repeat(3, 1fr)', gap: 12, marginTop: 16 }}>
            <div><Label hint="optional">Tag ID facing robot</Label><input value={tags.front} onChange={(e) => setTags((t) => ({ ...t, front: e.target.value.replace(/\D/g, '') }))} placeholder="default 0" style={inputCss(false, true)} /></div>
            <div><Label hint="optional">Tag ID facing env. camera</Label><input value={tags.back} onChange={(e) => setTags((t) => ({ ...t, back: e.target.value.replace(/\D/g, '') }))} placeholder="default 1" style={inputCss(false, true)} /></div>
            <div><Label hint="cm">Tag centre height</Label><input value={tags.height} onChange={(e) => setTags((t) => ({ ...t, height: e.target.value.replace(/[^0-9.]/g, '') }))} placeholder="auto" style={inputCss(false, true)} /></div>
          </div>
          <div style={{ fontSize: 11.5, color: C.text3, marginTop: 8, lineHeight: 1.5 }}>AprilTag 36h11 IDs visible in the video are detected automatically; these values answer the pipeline when they are not.</div>
        </div>
      )) : step('03', 'Table dimensions', 'Metric scale comes from the real table; per-object mass and friction are asked after objects are found.', (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'repeat(3, 1fr)', gap: 12 }}>
          {[['length_cm', 'Length'], ['width_cm', 'Width'], ['height_cm', 'Height (floor to top)']].map(([k, l]) => (
            <div key={k}><Label hint="cm, measured">{l}</Label><input value={table[k]} inputMode="decimal" onChange={(e) => setTable((t) => ({ ...t, [k]: e.target.value.replace(/[^0-9.]/g, '') }))} style={inputCss(false, true)} /></div>
          ))}
        </div>
      ))}
      {step('04', 'Process', 'Runs as a background job; you can leave this page.', (
        <div>
          {probs.length > 0 ? (
            <div style={{ padding: '12px 14px', borderRadius: 10, border: '1px solid ' + C.line, background: C.sunken }}>
              <div style={{ fontSize: 12.5, color: C.text, fontWeight: 500, marginBottom: 6 }}>Needed before processing</div>
              {probs.map((p) => <div key={p} style={{ fontSize: 12, color: C.text2, display: 'flex', gap: 8, alignItems: 'center', padding: '2px 0' }}><Dot color={C.warn} />{p}</div>)}
            </div>
          ) : (
            <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.6 }}>
              {mode === 'object' ? <>Processes {sel.length} capture{sel.length > 1 ? 's' : ''}, then pauses for your property review.{estimated.length > 0 && <> Estimated values ({estimated.join(', ')}) must be reviewed before the scene is built.</>}</>
                : <>Reconstructs the scene with COLMAP, finds the table plane and objects, then asks you for each object's properties.</>}
            </div>
          )}
          {mode === 'object' && system && system.huggingface && system.huggingface.status !== 'connected' && (
            <div style={{ marginTop: 14, padding: '12px 14px', borderRadius: 10, border: '1px solid rgba(224,178,82,0.35)', background: C.warnSoft }}>
              <div style={{ fontSize: 12.5, color: C.text, fontWeight: 500, marginBottom: 8 }}>Connect Hugging Face for mesh generation</div>
              <HfConnect system={system} onSaved={refreshSystem} compact />
            </div>
          )}
          {err && <div style={{ marginTop: 12 }}><ErrorLine msg={err} /></div>}
          <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center' }}>
            <Btn processing kind="primary" icon="cube" disabled={probs.length > 0 || busy} onClick={start} style={{ height: 38, padding: '0 18px' }}>{busy ? 'Starting…' : 'Start processing'}</Btn>
            <span style={{ fontSize: 12, color: C.text3 }}>Uses GPU quota.</span>
          </div>
        </div>
      ))}
    </div>
  );
}


/* Hugging Face token for TRELLIS: verified in the browser, saved to the Mana key store with apiManager (never to chat,
   Firebase or code). Pipeline runs use it only through a Mana proxy that injects it server-side. */
function HfConnect({ system, onSaved, compact }) {
  const [tok, setTok] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);
  const st = (system && system.huggingface && system.huggingface.status) || 'unknown';
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await fetch('https://huggingface.co/api/whoami-v2', { headers: { Authorization: 'Bearer ' + tok.trim() } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'Hugging Face rejected this token');
      await apiManager.save('HF_TOKEN', tok.trim());
      setTok('');
      setMsg({ ok: true, text: `Connected as ${j.name || 'your account'}${j.auth && j.auth.accessToken && j.auth.accessToken.role ? ' (' + j.auth.accessToken.role + ' token)' : ''}.` });
      onSaved && onSaved();
    } catch (e) { setMsg({ ok: false, text: e.message }); } finally { setBusy(false); }
  };
  const remove = async () => { setBusy(true); try { await apiManager.delete('HF_TOKEN'); setMsg({ ok: true, text: 'Token removed.' }); onSaved && onSaved(); } catch (e) { setMsg({ ok: false, text: e.message }); } finally { setBusy(false); } };
  return (
    <div>
      {!compact && <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.55, marginBottom: 10 }}>A free Hugging Face read token gives your runs their own daily GPU quota. Stored encrypted; never shown to the pipeline.</div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, marginBottom: 10 }}><Dot color={st === 'connected' ? C.ok : st === 'missing' ? C.warn : C.text3} /><span style={{ color: C.text }}>{st === 'connected' ? 'Token connected' : st === 'missing' ? 'No token connected (shared anonymous quota)' : 'Checking…'}</span>
        {st === 'connected' && <Btn small kind="quiet" disabled={busy} onClick={remove}>Remove</Btn>}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input type="password" autoComplete="off" value={tok} onChange={(e) => setTok(e.target.value)} placeholder={st === 'connected' ? 'Replace with a new token (hf_…)' : 'hf_…'} style={{ ...inputCss(false, true), flex: '1 1 240px' }} />
        <Btn kind={st === 'connected' ? 'ghost' : 'primary'} disabled={busy || tok.trim().length < 10} onClick={save}>{busy ? 'Verifying…' : 'Verify & save'}</Btn>
      </div>
      <div style={{ fontSize: 11.5, color: C.text3, marginTop: 8 }}>Create one at huggingface.co → Settings → Access Tokens (type “Read”).</div>
      {msg && <div style={{ fontSize: 12, color: msg.ok ? C.ok : C.err, marginTop: 8 }}>{msg.text}</div>}
    </div>
  );
}

/* ── helpers over project detail ─────────────────────────────────────────────── */
const filesOf = (detail, mid, prefix = '') => ((detail && detail.artifact_file) || []).filter((f) => f.manifest_id === mid && f.path.startsWith(prefix));
const pathMap = (files, prefix = '') => Object.fromEntries(files.map((f) => [f.path.slice(prefix.length), f.id]));
const manifestById = (detail, id) => ((detail && detail.artifact_manifest) || []).find((m) => m.id === id);

function LogPanel({ api, pid, run, height = 260 }) {
  const [text, setText] = React.useState('');
  const off = React.useRef(0), box = React.useRef(null), stick = React.useRef(true);
  const live = run && ['queued', 'running'].includes(run.status);
  React.useEffect(() => { setText(''); off.current = 0; }, [run && run.id]);
  React.useEffect(() => {
    if (!run) return;
    let stop = false, timer;
    const tick = async () => {
      try { const j = await api.req(`/api/projects/${pid}/runs/${run.id}/log?offset=${off.current}`); if (!stop && j.text) { off.current = j.offset; setText((s) => s + j.text); } else if (!stop) off.current = j.offset; } catch (e) {}
      if (live && !stop) timer = setTimeout(tick, 1500);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [run && run.id, live]);
  React.useEffect(() => { const el = box.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [text]);
  return (
    <div ref={box} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}
      style={{ height, overflow: 'auto', background: C.code, border: '1px solid ' + C.line, borderRadius: 10, padding: '10px 12px', fontFamily: MONO, fontSize: 11, lineHeight: 1.65, color: C.text2, userSelect: 'text' }}>
      {!text && <span style={{ color: C.text3 }}>{live ? 'waiting for output…' : (run && run.log_tail) || 'no log'}</span>}
      {text.split('\n').map((l, i) => <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: /^\s*\[\d+\/\d+\]/.test(l) ? C.text : /answered non-interactively/.test(l) ? C.warn : /Traceback|Error|\[failed\]/.test(l) ? C.err : undefined }}>{l || ' '}</div>)}
    </div>
  );
}

/* ── live run / stage timing (checkpoint-timing-contract.json). Clocks tick locally once a second from server timestamps;
   nothing is written back, and re-fetched details never restart a clock (it is derived from active_since_at, not mount time). ── */
const toMs = (v) => { const n = Number(v); return v != null && v !== '' && isFinite(n) && n > 0 ? (n < 1e12 ? n * 1000 : n) : null; };
const fmtClock = (ms) => { if (ms == null || !isFinite(ms)) return '—'; const s = Math.max(0, Math.floor(ms / 1000)); if (s < 60) return s + 's'; const m = Math.floor(s / 60); if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's'; return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm'; };
const fmtClockLong = (ms) => { const s = Math.max(0, Math.floor((ms || 0) / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60; return [h && h + ' hour' + (h > 1 ? 's' : ''), m && m + ' minute' + (m > 1 ? 's' : ''), (!h && (r || !m)) && r + ' second' + (r === 1 ? '' : 's')].filter(Boolean).join(' '); };
/* {baseMs, since}: accumulated active time plus the open interval start, or null when the stage has no recorded timing. */
function stageTiming(s) {
  if (!s) return null;
  if (s.restored && s.elapsed_ms == null) return null;   // satisfied from a verified checkpoint: no fabricated duration
  const since = toMs(s.active_since_at);
  if (s.elapsed_ms != null && isFinite(Number(s.elapsed_ms))) return { baseMs: Math.max(0, Number(s.elapsed_ms)), since, incomplete: !!s.elapsed_incomplete };
  if (since) return { baseMs: 0, since };
  const st = toMs(s.started_at != null ? s.started_at : s.started), fin = toMs(s.finished_at != null ? s.finished_at : s.finished);
  if (st && fin && fin >= st) return { baseMs: fin - st, since: null };
  if (st && s.status === 'running') return { baseMs: 0, since: st };
  return null;                                              // older stages without real timing stay unknown
}
function LiveClock({ baseMs = 0, since = null, label }) {
  const [, tick] = React.useState(0);
  React.useEffect(() => { if (since == null) return; const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, [since]);
  const ms = baseMs + (since != null ? Math.max(0, Date.now() - since) : 0);
  return <span role={since != null ? 'timer' : undefined} aria-label={label ? label + ': ' + fmtClockLong(ms) : undefined} style={{ fontVariantNumeric: 'tabular-nums' }}>{fmtClock(ms)}</span>;
}
function Ago({ ms }) {
  const [, tick] = React.useState(0);
  React.useEffect(() => { if (!ms) return; const t = setInterval(() => tick((x) => x + 1), 5000); return () => clearInterval(t); }, [ms]);
  if (!ms) return <>—</>;
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return <span style={{ fontVariantNumeric: 'tabular-nums' }}>{s < 5 ? 'just now' : s < 60 ? s + 's ago' : s < 3600 ? Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's ago' : fmtAgo(ms)}</span>;
}
const HEARTBEAT_STALE_MS = 90000;
const RECOVERY_LABEL = { resumed: 'Resumed from a saved checkpoint', resuming: 'Resuming from a saved checkpoint', restored: 'Restored after a worker restart', queued_restored: 'Restored to the queue after a restart', interrupted: 'Interrupted; nothing was retried automatically', needs_confirmation: 'Needs your confirmation before retrying', fresh: null, none: null };
const RunDataCtx = React.createContext(null);   // {api, pid, checkpoints, refresh, goto, canWrite} from the app root
const STAGE_RANK = { frames: 1, mesh: 2, asset: 3, scene: 4, export: 5 };
const REC_STATE = { none: null, resumed: 'Resumed after a worker restart', interrupted: 'Interrupted by a worker restart; nothing was retried automatically',
  resume_limit: 'Interrupted again after an automatic resume; continue explicitly', provider_outcome_unknown: 'A mesh request may have been in flight; its outcome is unknown' };
function lastVerifiedCheckpoint(ctx, run) {
  const list = ((ctx && ctx.checkpoints) || []).filter((c) => c.run_id === run.id && c.status === 'verified');
  return list.sort((a, b) => (STAGE_RANK[b.stage] || 0) - (STAGE_RANK[a.stage] || 0) || (toMs(b.saved_at) || 0) - (toMs(a.saved_at) || 0))[0] || null;
}
/* Resume / retry for a failed run. Shown only when the backend reports recovery.resumable; a possible repeat of a paid
   provider request (recovery.requires_ack) always needs an explicit confirmation, never an automatic retry. */
function RecoveryActions({ run, ctx }) {
  const rec = run.recovery || {};
  const [confirm, setConfirm] = React.useState(false), [busy, setBusy] = React.useState(false), [err, setErr] = React.useState(''), [done, setDone] = React.useState('');
  if (run.status !== 'failed' || !rec.resumable || !ctx || !ctx.api || ctx.canWrite === false) return null;
  const invalidCheckpoint = rec.state === 'checkpoint_invalid';
  const cpStage = !invalidCheckpoint && ['mesh', 'asset', 'scene', 'export'].includes(rec.last_checkpoint_stage) ? rec.last_checkpoint_stage : null;   // frames is a saved result, not a resume point
  const go = async (ack) => {
    setBusy(true); setErr('');
    try { const r = await ctx.api.req(`/api/projects/${ctx.pid}/runs/${run.id}/resume`, { method: 'POST', json: ack ? { acknowledge_provider_repeat: true } : {} }); setDone(r && r.resume_from ? 'Resuming from the saved ' + r.resume_from + ' checkpoint.' : 'Resume queued.'); setConfirm(false); if (ctx.refresh) ctx.refresh(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  if (done) return <div role="status" style={{ fontSize: 12, color: C.text2 }}>{done}</div>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {!confirm && <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <Btn processing small kind="primary" icon="retry" disabled={busy} onClick={() => rec.requires_ack ? setConfirm(true) : go(false)}>{busy ? 'Resuming…' : cpStage ? 'Resume from saved checkpoint' : 'Resume run'}</Btn>
        <span style={{ fontSize: 11.5, color: C.text3, lineHeight: 1.5 }}>{invalidCheckpoint ? 'The saved mesh could not be verified. No new provider request has been started.' : cpStage ? `Continues after the verified ${cpStage} checkpoint; completed paid work is reused, not repeated.` : (rec.last_checkpoint_stage ? 'The saved ' + rec.last_checkpoint_stage + ' result is kept, but it is not a resume point; ' : 'No resumable checkpoint was saved for this run; ') + 'processing starts again from its saved inputs.'}</span>
      </div>}
      {confirm && <div role="alertdialog" aria-label="Confirm repeating a paid request" style={{ padding: '10px 12px', border: '1px solid ' + C.accentLine, borderRadius: 10, background: C.accentSoft }}>
        <div style={{ fontSize: 12.5, color: C.text, lineHeight: 1.55 }}>No verified, compatible mesh is available for this attempt. Resuming may make another TRELLIS request and use GPU quota again. Existing captures and stored results are kept.</div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}><Btn processing small kind="primary" disabled={busy} onClick={() => go(true)}>{busy ? 'Resuming…' : 'Repeat the mesh request and resume'}</Btn><Btn small kind="quiet" onClick={() => setConfirm(false)}>Not now</Btn></div>
      </div>}
      {err && <ErrorLine msg={err} />}
    </div>
  );
}

const PREP_STATE = { starting: 'starting', provisioning: 'provisioning a worker', ready: 'ready', running: 'running', stopped: 'stopped', cancelled: 'cancelled', failed: 'failed' };
/* Actual isolated CPU worker status for background removal (runtime.preprocessing). Counts only what was processed;
   no percent or total, since the frame cap is an upper bound, not planned work. */
function PrepStatus({ p }) {
  if (!p || typeof p !== 'object' || !p.state) return null;
  const n = Number(p.calls) || 0, num = (v) => v != null && isFinite(Number(v)) ? fmtClock(Number(v) * 1000) : null;
  const bad = p.state === 'failed';
  const parts = [PREP_STATE[p.state] || String(p.state).replaceAll('_', ' '), n + (n === 1 ? ' cutout' : ' cutouts') + ' processed'];
  if (num(p.provision_s)) parts.push('worker ready in ' + num(p.provision_s));
  if (num(p.last_round_trip_s)) parts.push('last round trip ' + num(p.last_round_trip_s));
  if (p.cpu || p.memory_gib) parts.push([p.cpu && p.cpu + ' CPU', p.memory_gib && p.memory_gib + ' GiB'].filter(Boolean).join(' · '));
  if (p.box_stopped) parts.push('worker stopped' + (num(p.box_seconds) ? ' after ' + num(p.box_seconds) : ''));
  return <div style={{ fontSize: 11.5, color: bad ? C.err : C.text2, lineHeight: 1.5 }}><span style={{ color: bad ? C.err : C.text3 }}>Isolated image preparation</span> · {parts.join(' · ')}{p.reason && (bad || p.state === 'stopped' || p.state === 'cancelled') ? ' · ' + p.reason : ''}</div>;
}
/* Concise run status line: current stage clock, total wall time, separate progress vs heartbeat facts, attempt and last verified checkpoint. */
function RunTiming({ run }) {
  const ctx = React.useContext(RunDataCtx);
  if (!run || !/^run_/.test(run.id || '')) return null;
  const rt = { ...((run.output_json && typeof run.output_json === 'object' && run.output_json.runtime) || {}) };
  const rv = run.recovery || {};
  for (const k of ['heartbeat_at', 'progress_at', 'resumed_from_stage', 'checkpoint_id']) if (rt[k] == null && rv[k] != null) rt[k] = rv[k];
  if (!rt.recovery_state && rv.state) rt.recovery_state = rv.state;
  const stages = Array.isArray(run.stages_json) ? run.stages_json : [];
  const cur = run.status === 'running' ? (stages.find((s) => s.status === 'running') || stages.find((s) => s.status === 'waiting') || stages.find((s) => s.key === run.stage)) : null;
  const ct = cur ? stageTiming(cur) : null;
  const started = toMs(run.started_at), finished = toMs(run.finished_at), created = toMs(run.created_at);
  const live = run.status === 'running', queued = run.status === 'queued';
  const hb = toMs(rt.heartbeat_at), prog = toMs(rt.progress_at), attemptAt = toMs(rt.attempt_started_at);
  const stale = live && hb && Date.now() - hb > HEARTBEAT_STALE_MS;
  const rec = rt.recovery_state && Object.prototype.hasOwnProperty.call(REC_STATE, rt.recovery_state) ? REC_STATE[rt.recovery_state] : rt.recovery_state && Object.prototype.hasOwnProperty.call(RECOVERY_LABEL, rt.recovery_state) ? RECOVERY_LABEL[rt.recovery_state] : rt.recovery_state ? String(rt.recovery_state).replaceAll('_', ' ') : null;
  const items = [];
  if (cur) items.push(<span key="st"><span style={{ color: C.text3 }}>{cur.label || cur.key}</span> {ct ? <LiveClock baseMs={ct.baseMs} since={live ? ct.since : null} label={(cur.label || cur.key) + ' elapsed'} /> : <span style={{ color: C.text3 }}>time not recorded</span>}</span>);
  if (queued && created) items.push(<span key="q"><span style={{ color: C.text3 }}>queued</span> <LiveClock baseMs={0} since={created} label="Queued for" /></span>);
  const held = queued && rt.held && typeof rt.held === 'object' ? rt.held : null;
  if (held) items.push(<span key="held" style={{ color: C.warn }}>waiting to start: {held.code === 'memory_pressure' ? 'memory headroom' : held.code === 'maintenance_hold' ? 'maintenance hold' : (held.reason || held.code || 'held')}{toMs(held.at) ? <> · since <Ago ms={toMs(held.at)} /></> : null}</span>);
  if (started) items.push(<span key="tot"><span style={{ color: C.text3 }}>{live ? 'run' : 'total'}</span> <LiveClock baseMs={live || !finished ? 0 : finished - started} since={live ? started : null} label={live ? 'Run elapsed' : 'Run total'} /></span>);
  if (live && prog) items.push(<span key="pr"><span style={{ color: C.text3 }}>last progress</span> <Ago ms={prog} /></span>);
  if (live && hb) items.push(<span key="hb" style={{ color: stale ? C.warn : undefined }}><span style={{ color: stale ? C.warn : C.text3 }}>worker heartbeat</span> <Ago ms={hb} />{stale ? ' · delayed, reconnecting' : ''}</span>);
  if (run.attempt > 1 || rt.resumed_from_stage) items.push(<span key="at"><span style={{ color: C.text3 }}>attempt</span> {run.attempt || 1}{rt.resumed_from_stage ? ' · resumed from ' + rt.resumed_from_stage : ''}{attemptAt ? <> · started <Ago ms={attemptAt} /></> : null}</span>);
  const vc = lastVerifiedCheckpoint(ctx, run);
  const cp = vc ? { id: vc.checkpoint_id, stage: vc.stage, verified_at: vc.saved_at } : null;   // only manifests the backend reports as verified count as saved
  const showCp = cp || rec || (run.status === 'failed' && rv.resumable);
  const prep = rt.preprocessing;
  if (!items.length && !showCp && !prep) return null;
  return (
    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 5 }}>
      {items.length > 0 && <div style={{ display: 'flex', gap: '4px 16px', flexWrap: 'wrap', fontSize: 11.5, fontFamily: MONO, color: C.text2 }}>{items}</div>}
      <PrepStatus p={prep} />
      {stale && <div style={{ fontSize: 11.5, color: C.text3, lineHeight: 1.5 }}>No heartbeat for a while. Long GPU steps can be quiet; the run is not marked failed unless the worker reports it or its lease expires.</div>}
      {showCp && <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap', fontSize: 11.5, color: C.text2 }}>
        {(cp || (ctx && Array.isArray(ctx.checkpoints))) && <Icon name="db" size={12} color={cp ? C.ok : C.text3} style={{ alignSelf: 'center' }} />}
        {cp ? <span>Last verified checkpoint{cp.stage ? ': ' + cp.stage : ''}{toMs(cp.verified_at || cp.at) ? <> · saved <Ago ms={toMs(cp.verified_at || cp.at)} /></> : null} <span style={{ fontFamily: MONO, color: C.text3 }}>{String(cp.id).slice(-10)}</span></span> : ctx && Array.isArray(ctx.checkpoints) ? <span style={{ color: C.text3 }}>No verified checkpoint recorded for this run</span> : null}
        {rec && <span style={{ color: C.text3 }}>{cp || (ctx && Array.isArray(ctx.checkpoints)) ? '· ' : ''}{rec}</span>}
        {cp && ctx && ctx.goto && <button type="button" onClick={() => ctx.goto('library')} style={{ all: 'unset', cursor: 'pointer', color: C.accent, fontSize: 11.5 }}>Saved results in Files</button>}
      </div>}
      <RecoveryActions run={run} ctx={ctx} />
    </div>
  );
}

/* Failed pipeline runs offer a contextual hand-off to the Assistant (InvestigateRun renders nothing outside the app shell). */
function StageRail(props) { return <>{React.createElement(StageRailBase, props)}{!props.noTiming && <RunTiming run={props.run} />}{props.run && props.run.status === 'failed' && <InvestigateRun run={props.run} />}</>; }
function StageRailBase({ run, vertical }) {
  const st = (run && run.stages_json) || [];
  const byKey = Object.fromEntries(st.map((s) => [s.key, s]));
  const keys = run && !['asset','rebuild'].includes(run.kind) ? st.map((s) => [s.key, s.label]) : run && run.kind === 'rebuild' ? st.map((s) => [s.key, s.label]).concat([['export', 'Gymnasium export']]) : PRD_STAGES;
  return (
    <div style={{ display: 'flex', flexDirection: vertical ? 'column' : 'row', gap: vertical ? 0 : 4, flexWrap: vertical ? 'nowrap' : 'wrap' }}>
      {keys.map(([k, l], i) => {
        const s = byKey[k] || { status: k === 'export' ? 'separate' : 'pending' };
        const col = { done: C.ok, running: C.accent, waiting: C.warn, failed: C.err, skipped: C.text3 }[s.status] || C.text3;
        const tm = stageTiming(s), liveStage = tm && tm.since != null && (s.status === 'running' || s.status === 'waiting');
        const took = tm && !liveStage ? tm.baseMs / 1000 : null;
        return (
          <div key={k} style={vertical ? { display: 'flex', gap: 10, padding: '7px 0', borderBottom: i < keys.length - 1 ? '1px solid ' + C.line : 'none', alignItems: 'flex-start' }
            : { flex: '1 1 110px', minWidth: 104, padding: '8px 10px', borderRadius: 8, background: s.status === 'running' || s.status === 'waiting' ? C.raised : 'transparent', border: '1px solid ' + (s.status === 'running' ? C.accentLine : s.status === 'waiting' ? 'rgba(224,178,82,0.4)' : C.line) }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, ...(vertical ? { marginTop: 3 } : {}) }}>
              <span className={s.status === 'running' ? 'p2g-pulse' : ''} style={{ width: 8, height: 8, borderRadius: 9, background: s.status === 'pending' || s.status === 'separate' ? 'transparent' : col, border: '1.5px solid ' + col, flex: 'none' }} />
              {!vertical && <span style={{ fontFamily: MONO, fontSize: 10, color: C.text3 }}>{String(i + 1).padStart(2, '0')}</span>}
            </div>
            <div style={{ flex: 1, minWidth: 0, marginTop: vertical ? 0 : 6 }}>
              <div style={{ fontSize: 12, color: s.status === 'pending' || s.status === 'separate' ? C.text3 : C.text, fontWeight: s.status === 'running' || s.status === 'waiting' ? 600 : 400 }}>{l}</div>
              <div style={{ fontSize: 10.5, color: C.text3, marginTop: 2, fontFamily: MONO, whiteSpace: vertical ? 'normal' : 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {s.status === 'separate' ? 'on the Export screen' : <>{s.status === 'waiting' ? 'needs you' : null}{liveStage ? <><LiveClock baseMs={tm.baseMs} since={tm.since} label={l + ' elapsed'} />{s.detail ? ' · ' : ''}</> : null}{s.status !== 'waiting' && took != null ? (tm.incomplete ? 'at least ' : '') + fmtDur(took) + (s.detail ? ' · ' : '') : ''}{s.restored && took == null ? 'restored from a verified checkpoint' + (s.detail ? ' · ' : '') : ''}{s.status !== 'waiting' ? (s.detail || (s.status === 'pending' ? 'pending' : '')) : ''}</>}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Frames({ api, pid, files }) {
  return <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 4 }}>{files.map((f) => <FrameImg key={f.id} api={api} pid={pid} f={f} />)}</div>;
}
function FrameImg({ api, pid, f }) {
  const url = useFileUrl(api, pid, f.id);
  return <div style={{ height: 120, minWidth: 68, borderRadius: 8, overflow: 'hidden', border: '1px solid ' + C.line, background: C.raised, flex: 'none' }}>{url && <img src={url} alt={f.path} title={f.path} style={{ height: '100%', display: 'block' }} />}</div>;
}

function PropsTable({ av }) {
  const p = av.properties_json || {}, m = av.measurements_json || {};
  const size = p.size_m || [];
  const rows = [
    ['Length', size[0] != null ? (size[0] * 100).toFixed(1) + ' cm' : '—', m.length_cm], ['Width', size[1] != null ? (size[1] * 100).toFixed(1) + ' cm' : '—', m.width_cm],
    ['Height', size[2] != null ? (size[2] * 100).toFixed(1) + ' cm' : '—', m.height_cm], ['Mass', p.mass_kg != null ? (p.mass_kg * 1000).toFixed(1) + ' g' : '—', m.mass_g],
    ['Friction μ', p.friction ? Number(p.friction[0]).toFixed(2) : '—', m.friction], ['Material', p.material || '—', m.material],
    ['Centre of mass', p.center_of_mass ? p.center_of_mass.map((v) => (v * 1000).toFixed(1)).join(', ') + ' mm' : '—', null],
    ['Collision hulls', p.collision_pieces != null ? p.collision_pieces + ' convex pieces (CoACD)' : '—', null],
  ];
  return (
    <div>
      {rows.map(([k, v, mm]) => (
        <div key={k} style={{ display: 'grid', gridTemplateColumns: '120px 1fr auto', gap: 10, alignItems: 'center', padding: '7px 0', borderBottom: '1px solid ' + C.line, fontSize: 12.5 }}>
          <span style={{ color: C.text3 }}>{k}</span><span style={{ fontFamily: MONO, color: C.text }}>{v}</span>{mm ? <SourceChip source={mm.source} /> : <span style={{ fontSize: 10.5, color: C.text3 }}>computed</span>}
        </div>
      ))}
    </div>
  );
}

function ReviewScreen({ api, pid, detail, narrow, refresh, goto, system, refreshSystem }) {
  const runs = ((detail && detail.pipeline_run) || []).filter((r) => ['asset','rebuild','scene_capture'].includes(r.kind));
  const [runId, setRunId] = React.useState(null);
  const run = runs.find((r) => r.id === runId) || runs[0];
  const avs = (detail && detail.asset_version) || [];
  const av = run && avs.find((a) => a.id === (run.output_json || {}).asset_version_id);
  const [editing, setEditing] = React.useState(false);
  const [meas, setMeas] = React.useState(null);
  const [ack, setAck] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [ackRetry, setAckRetry] = React.useState(false);
  React.useEffect(() => { setEditing(false); setAck(false); setErr(null); }, [av && av.id]);
  React.useEffect(() => { setAckRetry(false); }, [run && run.id]);
  if (!run) return <Empty title="Nothing processed yet" sub="Start a capture to see its pipeline here." action={<Btn kind="primary" icon="plus" onClick={() => goto('capture')}>Add project</Btn>} />;
  const act = async (action, extra = {}) => {
    setBusy(true); setErr(null);
    try { await api.req(`/api/projects/${pid}/assets/${av.id}/review`, { json: { action, ...extra } }); setEditing(false); refresh(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const runAct = async (a, extra = {}) => { setBusy(true); setErr(null); try { await api.req(`/api/projects/${pid}/runs/${run.id}/${a}`, { method: 'POST', json: extra }); setAckRetry(false); refresh(); } catch (e) { setErr(e.message); } finally { setBusy(false); } };
  const pv = av ? filesOf(detail, av.manifest_id, 'preview/') : [];
  const frames = av ? filesOf(detail, av.manifest_id, 'frames/').filter((f) => /view_\d+\.jpg$/.test(f.path)) : [];
  const est = av ? Object.entries(av.measurements_json || {}).filter(([, v]) => v && v.source === 'estimated').map(([k]) => k) : [];
  const ups = ((detail && detail.upload) || []).filter((u) => ((run.input_json || {}).upload_ids || []).includes(u.id));
  const answers = av ? ((av.params_json || {}).pipeline_answers || []) : [];
  const scene = run.output_json && run.output_json.scene_version_id;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Processing & review</h2>
        <Status s={runDisplayStatus(run)} />
        <span style={{ fontSize: 11.5, color: C.text3, fontFamily: MONO }}>{run.id} · {run.kind}{run.commit ? ' · @' + run.commit : ''}{run.attempt > 1 ? ` · attempt ${run.attempt}` : ''}</span>
        <div style={{ flex: 1 }} />
        {runs.length > 1 && <select value={run.id} onChange={(e) => setRunId(e.target.value)} style={{ ...inputCss(), width: 'auto', height: 30, fontSize: 12 }}>
          {runs.map((r) => <option key={r.id} value={r.id}>{r.kind} · {fmtAgo(r.created_at)} · {(STATUS[r.status] || [r.status])[0]}</option>)}</select>}
        {['queued', 'running', 'needs_review'].includes(run.status) && <Btn small kind="ghost" icon="x" disabled={busy} onClick={() => runAct('cancel')}>Cancel</Btn>}
        {run.status === 'failed' && <Btn processing small kind="ghost" icon="retry" disabled={busy} onClick={() => run.recovery && run.recovery.requires_ack ? setAckRetry(true) : runAct('retry')}>Retry as a new run</Btn>}
      </div>
      {ackRetry && <div role="alertdialog" aria-label="Confirm repeating a paid request" style={{ padding: '10px 12px', border: '1px solid ' + C.accentLine, borderRadius: 10, background: C.accentSoft }}>
        <div style={{ fontSize: 12.5, color: C.text, lineHeight: 1.55 }}>A new run repeats the mesh request whose earlier outcome is unknown. It may use TRELLIS GPU quota again.</div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}><Btn processing small kind="primary" disabled={busy} onClick={() => runAct('retry', { acknowledge_provider_repeat: true })}>Repeat as a new run</Btn><Btn small kind="quiet" onClick={() => setAckRetry(false)}>Not now</Btn></div>
      </div>}
      <StageRail run={run} />
      {run.status === 'failed' && run.error && (['cancelled','rejected'].includes(runDisplayStatus(run)) ? <p style={{fontSize:13,color:C.text3}}>{runDisplayStatus(run)==='rejected'?'This version was rejected in review. Its files and history are preserved.':'This run was cancelled. Previous versions are unchanged.'}</p> : <ErrorLine msg={run.error} />)}
      {run.status === 'failed' && /Hugging Face GPU quota|ZeroGPU quota/.test(run.error || '') && (
        <Panel title="Connect Hugging Face, then retry"><HfConnect system={system} onSaved={refreshSystem} /></Panel>
      )}
      {err && <ErrorLine msg={err} />}
      {av ? (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'minmax(0,1.35fr) minmax(300px,1fr)', gap: 16, alignItems: 'start' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
            {pv.length ? <Viewer api={api} pid={pid} scene={pathMap(pv, 'preview/')} traj={null} height={narrow ? 340 : 440} compact /> : <div style={{ height: 300, border: '1px dashed ' + C.line2, borderRadius: 12 }} />}
            <Panel title="Selected frames" aside={<span style={{ fontSize: 11.5, color: C.text3 }}>{frames.length} of the video, chosen by the repo's frame rules → TRELLIS</span>}>
              <Frames api={api} pid={pid} files={frames} />
              <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
                {ups.map((u) => <div key={u.id} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11.5, color: C.text3 }}><Thumb api={api} pid={pid} fid={(u.meta_json || {}).thumb_file_id} size={32} radius={6} /><span><span style={{ color: C.text2 }}>{u.filename}</span><br /><span style={{ fontFamily: MONO }}>sha256 {short(u.sha256, 12)}</span></span></div>)}
              </div>
            </Panel>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
            <Panel title={<span style={{ fontFamily: MONO }}>{av.object_name} · v{av.version}</span>} aside={<Status s={av.status} />}>
              <PropsTable av={av} />
              <div style={{ fontSize: 11.5, color: C.text3, marginTop: 10, lineHeight: 1.5 }}>{(av.properties_json || {}).source}</div>
              {av.parent_id && <div style={{ fontSize: 11.5, color: C.text3, marginTop: 6 }}>Rebuilt from v{(avs.find((x) => x.id === av.parent_id) || {}).version} with revised measurements (same mesh).</div>}
            </Panel>
            {answers.length > 0 && (
              <Panel title="Assumptions made by the pipeline">
                {answers.map((a, i) => <div key={i} style={{ fontSize: 12, padding: '6px 0', borderBottom: i < answers.length - 1 ? '1px solid ' + C.line : 'none' }}><div style={{ color: C.text2, fontFamily: MONO, fontSize: 11 }}>{a.question}</div><div style={{ color: C.text, marginTop: 2 }}>→ {a.answer} <span style={{ color: C.text3 }}>({a.source})</span></div></div>)}
              </Panel>
            )}
            {av.status === 'needs_review' && !editing && (
              <Panel title="Your review">
                <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.55, marginBottom: 12 }}>Check the shape, the collision hulls and the numbers. Accepting builds the final scene and runs simulation validation.</div>
                {est.length > 0 && <label style={{ display: 'flex', gap: 9, fontSize: 12.5, color: C.text, marginBottom: 12, cursor: 'pointer', lineHeight: 1.5 }}><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} style={{ accentColor: C.accent, marginTop: 3 }} /><span>I reviewed the estimated values ({est.join(', ')}). They stay labelled as estimates, not measurements.</span></label>}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <Btn processing kind="primary" icon="check" disabled={busy || (est.length > 0 && !ack)} onClick={() => act('accept', { estimates_reviewed: ack })}>Accept & build scene</Btn>
                  <Btn icon="settings" disabled={busy} onClick={() => { setMeas(JSON.parse(JSON.stringify({ ...emptyMeas(), ...(av.measurements_json || {}) }))); setEditing(true); }}>Correct values</Btn>
                  <Btn kind="danger" disabled={busy} onClick={() => act('reject')}>Reject</Btn>
                </div>
              </Panel>
            )}
            {editing && meas && (
              <Panel title="Correct values → new version" aside={<Btn small kind="quiet" onClick={() => setEditing(false)}>Cancel</Btn>}>
                <div style={{ fontSize: 12, color: C.text3, marginBottom: 6 }}>Rebuilds from the stored mesh (no new TRELLIS run) as v{(Math.max(...avs.map((x) => x.version)) || 0) + 1}. This version stays unchanged.</div>
                {OBJ_FIELDS.map((f) => <MeasureRow key={f[0]} field={f} m={meas[f[0]] || { value: '', source: 'unknown' }} narrow onChange={(v) => setMeas((s) => ({ ...s, [f[0]]: v }))} />)}
                <div style={{ marginTop: 12 }}><Btn processing kind="primary" icon="retry" disabled={busy} onClick={() => act('revise', { measurements: meas, estimates_reviewed: false })}>Rebuild as new version</Btn></div>
              </Panel>
            )}
            {av.status === 'accepted' && (
              <Panel title="Scene">
                {scene ? <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><span style={{ fontSize: 12.5, color: C.text2, flex: 1 }}>Scene built and validated.</span><Btn small kind="solid" icon="sim" onClick={() => goto('simulation')}>Open simulation</Btn></div>
                  : <div style={{ fontSize: 12.5, color: C.text2 }}>{run.status === 'failed' ? 'Scene build stopped; see the error above.' : 'Building the scene and running the drop test…'}</div>}
              </Panel>
            )}
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'minmax(0,1fr) 300px', gap: 16 }}>
          <LogPanel api={api} pid={pid} run={run} height={narrow ? 280 : 400} />
          <details style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.6 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12, color: C.text3 }}>How this run works</summary>
            <div style={{ marginTop: 8 }}>Frames are picked by fixed rules, backgrounds removed, then TRELLIS makes the mesh on a GPU. It is cleaned, scaled to your measurements and split into collision hulls. Any default the pipeline falls back to is listed for review.</div>
          </details>
        </div>
      )}
      {av && <details><summary style={{ cursor: 'pointer', fontSize: 12, color: C.text3, padding: '4px 0' }}>Run log</summary><div style={{ marginTop: 8 }}><LogPanel api={api} pid={pid} run={run} /></div></details>}
    </div>
  );
}

function Empty({ title, sub, action }) {
  return <div style={{ border: '1px dashed ' + C.line2, borderRadius: 12, padding: '40px 20px', textAlign: 'center', maxWidth: 640, margin: '20px auto' }}>
    <div style={{ fontSize: 14, fontWeight: 500 }}>{title}</div><div style={{ fontSize: 12.5, color: C.text3, marginTop: 6, lineHeight: 1.5 }}>{sub}</div>{action && <div style={{ marginTop: 14 }}>{action}</div>}</div>;
}

function LegacySimulationScreen({ api, pid, detail, system, narrow, refresh, goto }) {
  const svs = (detail && detail.scene_version) || [];
  const [svId, setSvId] = React.useState(null);
  const sv = svs.find((s) => s.id === svId) || svs.find((s) => s.status === 'validated') || svs[0];
  const sims = ((detail && detail.simulation_run) || []).filter((s) => sv && s.scene_version_id === sv.id);
  const [simId, setSimId] = React.useState(null);
  const sim = sims.find((s) => s.id === simId) || sims[0];
  const av = sv && ((detail && detail.asset_version) || []).find((a) => a.id === (sv.asset_version_ids || [])[0]);
  const place = sv && ((sv.params_json || {}).places || {})[av && av.object_name];
  const [seed, setSeed] = React.useState(1);
  const [pose, setPose] = React.useState({ x: 0, y: -25, yaw: 0 });
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  React.useEffect(() => { if (place) setPose({ x: +(place[0] * 100).toFixed(1), y: +(place[1] * 100).toFixed(1), yaw: 0 }); }, [sv && sv.id]);
  if (!sv) return <Empty title="No scene yet" sub="Accept an asset version in review; the scene is built and validated automatically." action={<Btn onClick={() => goto('review')}>Go to review</Btn>} />;
  const runSim = async (body) => {
    setBusy(true); setErr(null);
    try { const r = await api.req(`/api/projects/${pid}/sims`, { json: { scene_version_id: sv.id, ...body } }); await refresh(); setSimId(r.id); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const sceneFiles = pathMap(filesOf(detail, sv.manifest_id));
  const trajFiles = sim ? pathMap(filesOf(detail, sim.manifest_id)) : null;
  const val = sv.validation_json || {};
  const eng = val.engine || {};
  const slider = (k, label, min, max, step, unit) => (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, color: C.text2, marginBottom: 4 }}><span>{label}</span><span style={{ fontFamily: MONO, color: C.text }}>{pose[k]} {unit}</span></div>
      <input type="range" min={min} max={max} step={step} value={pose[k]} onChange={(e) => setPose((p) => ({ ...p, [k]: parseFloat(e.target.value) }))} style={{ width: '100%', accentColor: C.accent }} />
    </div>
  );
  return (
    <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'minmax(0,1fr) 300px', gap: 14, alignItems: 'start' }}>
      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Simulation</h2>
          <span style={{ fontSize: 11.5, color: C.warn, border: '1px solid ' + C.line, borderRadius: 6, padding: '2px 8px' }}>Physics drop test · no trained policy for this asset</span>
          <div style={{ flex: 1 }} />
          {svs.length > 1 && <select value={sv.id} onChange={(e) => { setSvId(e.target.value); setSimId(null); }} style={{ ...inputCss(), width: 'auto', height: 30, fontSize: 12 }}>{svs.map((s) => <option key={s.id} value={s.id}>scene v{s.version} · {(STATUS[s.status] || [s.status])[0]}</option>)}</select>}
        </div>
        <Viewer api={api} pid={pid} scene={sceneFiles} traj={trajFiles} height={narrow ? 380 : 'clamp(420px, 62vh, 640px)'} />
        {sv.status !== 'validated' && <ErrorLine msg={`Simulation validation failed for this scene version (${val.result}). It can be inspected but not exported.`} />}
        <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr 1fr' : 'repeat(5, 1fr)', border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden', background: C.panel }}>
          {[['Asset', av ? `${av.object_name} v${av.version}` : '—'], ['Scene', `v${sv.version}`], ['MuJoCo', eng.mujoco || (system && system.repo && system.repo.mujoco) || '—'],
            ['Model hash', short(val.model_hash, 12)], ['Robot', 'SO-100 · sim profile']].map(([k, v]) => (
            <div key={k} style={{ padding: '10px 12px', borderRight: '1px solid ' + C.line }}><Eyebrow>{k}</Eyebrow><div style={{ fontFamily: MONO, fontSize: 12, marginTop: 5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{v}</div></div>
          ))}
        </div>
        <div style={{ fontSize: 11.5, color: C.text3, lineHeight: 1.5 }}>Every run is computed by MuJoCo {eng.mujoco} on the server from the stored asset files ({eng.builder === 'repo:make_scene.build_scene' ? "scene built by the repository's make_scene.py" : eng.builder}); the browser only renders the recorded trajectory. {system && system.robot_note}</div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Panel title="Run a simulation">
          <Label>Seed</Label>
          <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
            <input value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ''))} style={inputCss(false, true)} />
            <Btn icon="dice" onClick={() => setSeed(Math.floor(Math.random() * 1e6))} title="Random seed" />
          </div>
          <Btn processing kind="solid" icon="play" disabled={busy} onClick={() => runSim({ seed: parseInt(seed || '0', 10) })} style={{ width: '100%' }}>{busy ? 'Simulating…' : 'Run with seeded pose'}</Btn>
          <div style={{ fontSize: 11, color: C.text3, margin: '6px 0 14px' }}>Seeded: ±4 cm and ±30° around the default spot.</div>
          <Label>Initial object pose</Label>
          {slider('x', 'Sideways x', -20, 20, 0.5, 'cm')}{slider('y', 'In front y', -40, -12, 0.5, 'cm')}{slider('yaw', 'Yaw', -180, 180, 5, '°')}
          <Btn processing disabled={busy} icon="play" onClick={() => runSim({ seed: parseInt(seed || '0', 10), x: pose.x / 100, y: pose.y / 100, yaw: pose.yaw })} style={{ width: '100%' }}>Run from this pose</Btn>
          {err && <div style={{ marginTop: 10 }}><ErrorLine msg={err} /></div>}
        </Panel>
        <Panel title="Runs" pad={false} aside={<span style={{ fontSize: 11, color: C.text3 }}>{sims.length}</span>}>
          <div style={{ maxHeight: 280, overflow: 'auto' }}>
            {sims.map((s) => { const c = s.controls_json || {}; return (
              <div key={s.id} role="button" tabIndex={0} onClick={() => setSimId(s.id)} className="p2g-row" style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '9px 14px', borderTop: '1px solid ' + C.line, cursor: 'pointer', background: sim && sim.id === s.id ? C.raised : 'transparent' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontFamily: MONO, color: C.text }}>{c.source === 'validation' ? 'validation' : 'seed ' + s.seed} · {c.x != null ? `${(c.x * 100).toFixed(1)}, ${(c.y * 100).toFixed(1)} cm, ${Math.round(c.yaw_deg != null ? c.yaw_deg : c.yaw || 0)}°` : ''}</div>
                  <div style={{ fontSize: 10.5, color: C.text3, marginTop: 2 }}>{fmtAgo(s.created_at)} · {s.mode}</div>
                </div>
                <Status s={s.result} />
              </div>); })}
          </div>
        </Panel>
        {sv.status === 'validated' && <Btn kind="primary" icon="box" onClick={() => goto('export')}>Export Gymnasium package</Btn>}
      </div>
    </div>
  );
}

function LegacyExportScreen({ api, pid, detail, system, narrow, refresh, goto }) {
  const svs = ((detail && detail.scene_version) || []).filter((s) => s.status === 'validated');
  const [svId, setSvId] = React.useState(null);
  const sv = svs.find((s) => s.id === svId) || svs[0];
  const av = sv && ((detail && detail.asset_version) || []).find((a) => a.id === (sv.asset_version_ids || [])[0]);
  const exports = (detail && detail.gym_export) || [];
  const runs = ((detail && detail.pipeline_run) || []).filter((r) => r.kind === 'export');
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [open, setOpen] = React.useState(null);
  if (!sv) return <Empty title="No validated scene yet" sub="An export needs an accepted asset version and a scene version that passed simulation validation." action={<Btn onClick={() => goto('review')}>Go to review</Btn>} />;
  const next = exports.filter((e) => e.scene_version_id === sv.id).length + 1;
  const latestEnv = exports.filter((e) => e.env_id).sort((x, y) => (y.scene_version_id === sv.id) - (x.scene_version_id === sv.id) || (y.created_at || 0) - (x.created_at || 0))[0];
  const envId = latestEnv ? latestEnv.env_id : `RealiSIM/PickLift-${av.object_name}-v${sv.version}`;
  const envIdNote = latestEnv ? 'from the latest package; the server assigns the ID for new requests' : 'expected for a new request; the server assigns the final ID';
  const go = async () => { setBusy(true); setErr(null); try { await api.req(`/api/projects/${pid}/exports`, { json: { scene_version_id: sv.id } }); refresh(); } catch (e) { setErr(e.message); } finally { setBusy(false); } };
  const active = runs.find((r) => ['queued', 'running'].includes(r.status));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1080, margin: '0 auto' }}>
      <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Gymnasium export</h2>
      <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'minmax(0,1fr) 340px', gap: 16, alignItems: 'start' }}>
        <Panel title="Environment" aside={svs.length > 1 && <select value={sv.id} onChange={(e) => setSvId(e.target.value)} style={{ ...inputCss(), width: 'auto', height: 28, fontSize: 12 }}>{svs.map((s) => <option key={s.id} value={s.id}>scene v{s.version}</option>)}</select>}>
          <Kv k="Environment ID" v={envId} />
          <div style={{ fontSize: 11, color: C.text3, margin: '2px 0 4px' }}>{envIdNote}. Existing packages keep their original identifiers.</div>
          <Kv k="Next package version" v={`1.${sv.version}.${next}`} />
          <Kv k="Task" v="Single-object pick-and-lift (repository TagLiftEnv)" mono={false} />
          <Kv k="Object" v={`${av.object_name} · asset v${av.version}`} />
          <Kv k="Observation" v="Box(20) actor state: joints, targets, AprilTag pose" mono={false} />
          <Kv k="Privileged state" v="info['privileged_state'] (13), critic only" />
          <Kv k="Action" v="Box(-1, 1, 6) joint-target deltas" />
          <Kv k="Robot" v="SO-100 (MuJoCo Menagerie), simulation profile" mono={false} />
          <div style={{ fontSize: 11.5, color: C.text3, marginTop: 10, lineHeight: 1.5 }}>Download unlocks after a clean install and Gymnasium check.</div>
        </Panel>
        <Panel title="Build">
          {active ? <StageRail run={active} vertical /> : <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.55, marginBottom: 12 }}>Builds, installs, checks and verifies the package.</div>}
          {err && <div style={{ margin: '10px 0' }}><ErrorLine msg={err} /></div>}
          {!active && <Btn processing kind="primary" icon="box" disabled={busy} onClick={go} style={{ width: '100%', marginTop: 4 }}>{busy ? 'Starting…' : 'Generate package'}</Btn>}
        </Panel>
      </div>
      <Eyebrow>Packages</Eyebrow>
      {exports.length === 0 && <div style={{ fontSize: 12.5, color: C.text3 }}>No packages yet.</div>}
      {exports.map((e) => {
        const v = e.validation_json || {}, s = v.smoke || {}, d = e.dependency_json || {};
        const legacyObj = ((detail.asset_version || []).find((a) => a.id === e.asset_version_id) || {}).object_name;
        const archiveName = d.archive_name ? (/\.zip$/i.test(d.archive_name) ? d.archive_name : d.archive_name + '.zip') : `photo2grasp_env-${legacyObj}-${e.package_version}.zip`;
        const files = e.manifest_id ? filesOf(detail, e.manifest_id) : [];
        const size = files.filter((f) => f.path.startsWith('archive/')).reduce((a, f) => a + f.size, 0);
        const run = runs.find((r) => r.id === e.run_id);
        const isOpen = open === e.id;
        return (
          <Panel key={e.id} title={<span style={{ fontFamily: MONO }}>{e.env_id} · {e.package_version}</span>} aside={<Status s={e.status} />}>
            <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr' : '1fr 1fr 1fr', gap: 18 }}>
              <div>
                <Eyebrow style={{ marginBottom: 6 }}>Validation</Eyebrow>
                {[['Installs in a clean venv', v.install && v.install.ok, v.install && `${v.install.seconds}s · py ${v.install.python}`],
                  ['Gymnasium env checker', s.check_env === 'passed', s.gymnasium && `gymnasium ${s.gymnasium}`],
                  ['Spaces respected', s.ok, s.steps && `${s.steps} steps · ${s.step_ms} ms/step`],
                  ['Seeded reset reproducible', s.seeded_reset_reproducible, null],
                  ['Stored + read back', v.persistence && v.persistence.ok, v.persistence && `${v.persistence.manifest_lines} hashes match`]].map(([k, ok, sub]) => (
                  <div key={k} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', padding: '4px 0', fontSize: 12.5 }}>
                    <Icon name={ok ? 'check' : (e.status === 'failed' ? 'x' : 'reset')} size={13} color={ok ? C.ok : e.status === 'failed' ? C.err : C.text3} sw={2} style={{ marginTop: 3 }} />
                    <span style={{ color: ok ? C.text : C.text3 }}>{k}{sub && <span style={{ display: 'block', fontFamily: MONO, fontSize: 10.5, color: C.text3 }}>{sub}</span>}</span>
                  </div>
                ))}
                {v.error && <div style={{ marginTop: 8 }}><ErrorLine msg={v.error} /></div>}
              </div>
              <div>
                <Eyebrow style={{ marginBottom: 6 }}>Versions</Eyebrow>
                <Kv k="Package" v={e.package_version} /><Kv k="Asset" v={'v' + ((detail.asset_version || []).find((a) => a.id === e.asset_version_id) || {}).version} />
                <Kv k="Scene" v={'v' + ((detail.scene_version || []).find((x) => x.id === e.scene_version_id) || {}).version} /><Kv k="Files" v={files.length ? `${files.length - 1} + archive` : '—'} />
                <Kv k="Archive" v={fmtBytes(size || null)} />
              </div>
              <div>
                <Eyebrow style={{ marginBottom: 6 }}>Pinned dependencies</Eyebrow>
                {d.module_name && <Kv k="Python module" v={d.module_name} />}
                {Object.entries(d).filter(([k]) => !['optional', 'module_name', 'archive_root', 'archive_name'].includes(k)).map(([k, val]) => <Kv key={k} k={k} v={String(val)} />)}
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap', alignItems: 'center' }}>
              <Btn kind="primary" icon="download" disabled={e.status !== 'ready'} onClick={() => saveFile(api, pid, e.archive_file_id, archiveName)}>
                {e.status === 'ready' ? 'Download package' : 'Available after validation'}</Btn>
              {files.length > 0 && <Btn kind="ghost" onClick={() => setOpen(isOpen ? null : e.id)}>{isOpen ? 'Hide files' : 'Browse files'}</Btn>}
              <span style={{ flex: 1 }} />
              <span style={{ fontSize: 11, color: C.text3, fontFamily: MONO }}>{fmtAgo(e.created_at)}</span>
            </div>
            {isOpen && <div style={{ marginTop: 12 }}><FileTable api={api} pid={pid} files={files} /><Readme api={api} pid={pid} files={files} root={d.archive_root} /></div>}
            {run && run.id !== active?.id && ['queued', 'running'].includes(run.status) && <div style={{ marginTop: 12 }}><StageRail run={run} /></div>}
            {run && run.status === 'failed' && <details style={{ marginTop: 10 }}><summary style={{ cursor: 'pointer', fontSize: 12, color: C.text3 }}>Build log</summary><div style={{ marginTop: 8 }}><LogPanel api={api} pid={pid} run={run} height={220} /></div></details>}
          </Panel>
        );
      })}
    </div>
  );
}

function Readme({ api, pid, files, root }) {
  const f = (root && files.find((x) => x.path === String(root).replace(/\/+$/, '') + '/README.md')) || files.find((x) => x.path === 'photo2grasp_env/README.md');
  const [txt, setTxt] = React.useState(null);
  React.useEffect(() => { if (f) fileBlob(api, pid, f.id).then((b) => b.text()).then(setTxt).catch(() => {}); }, [f && f.id]);
  if (!f) return null;
  return <div style={{ marginTop: 12 }}><Eyebrow style={{ marginBottom: 6 }}>README.md</Eyebrow>
    <div style={{ background: C.code, border: '1px solid ' + C.line, borderRadius: 10, padding: '4px 16px', fontSize: 12.5, lineHeight: 1.6, color: C.text2, maxHeight: 360, overflow: 'auto' }} className="p2g-md">
      {txt ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{txt}</ReactMarkdown> : 'Loading…'}</div></div>;
}

function FileTable({ api, pid, files }) {
  return (
    <div style={{ border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden', maxHeight: 320, overflowY: 'auto' }}>
      {files.slice().sort((a, b) => a.path.localeCompare(b.path)).map((f, i) => (
        <div key={f.id} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 80px 110px 28px', gap: 10, alignItems: 'center', padding: '6px 12px', borderTop: i ? '1px solid ' + C.line : 'none', fontSize: 11.5 }}>
          <span style={{ fontFamily: MONO, color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.path}>{f.path}</span>
          <span style={{ fontFamily: MONO, color: C.text3, textAlign: 'right' }}>{fmtBytes(f.size)}</span>
          <span style={{ fontFamily: MONO, color: C.text3 }} title={f.sha256}>{short(f.sha256, 12)}</span>
          <button onClick={() => saveFile(api, pid, f.id, f.path.split('/').pop())} title="Download" style={{ border: 'none', background: 'transparent', color: C.text3, cursor: 'pointer', display: 'flex' }}><Icon name="download" size={13} /></button>
        </div>
      ))}
    </div>
  );
}

function LibraryScreen({ api, pid, detail, narrow }) {
  const [open, setOpen] = React.useState(null);
  const man = (detail && detail.artifact_manifest) || [];
  const av = (detail && detail.asset_version) || [], sv = (detail && detail.scene_version) || [], sims = (detail && detail.simulation_run) || [], ex = (detail && detail.gym_export) || [];
  const groups = [
    ['Asset versions', av.map((a) => ({ id: a.manifest_id, title: `${a.object_name} · v${a.version}`, status: a.status, when: a.created_at, zip: `${a.object_name}_v${a.version}`, prefix: 'asset/' }))],
    ['Scene versions', sv.map((s) => ({ id: s.manifest_id, title: `scene v${s.version}`, status: s.status, when: s.created_at, zip: `scene_v${s.version}` }))],
    ['Gymnasium packages', ex.filter((e) => e.manifest_id).map((e) => ({ id: e.manifest_id, title: `${e.env_id} · ${e.package_version}`, status: e.status, when: e.created_at, zip: `package_${e.package_version}` }))],
    ['Simulation runs', sims.slice(0, 25).map((s) => ({ id: s.manifest_id, title: `seed ${s.seed} · ${s.mode}`, status: s.result, when: s.created_at, zip: `sim_${s.id}` }))],
    ['Run logs', man.filter((m) => m.kind === 'log').map((m) => ({ id: m.id, title: m.ref_id, status: m.status, when: m.created_at, zip: `log_${m.ref_id}` }))],
  ];
  const uploads = (detail && detail.upload) || [];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, maxWidth: 1080, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Library</h2>
        <span style={{ fontSize: 12, color: C.text3 }}><span title="Every file is read back and SHA-256 verified before it is marked ready">{fmtBytes(detail && detail.storage_bytes)} stored · verified</span></span>
      </div>
      {groups.map(([title, items]) => (
        <div key={title}>
          <Eyebrow style={{ marginBottom: 8 }}>{title} · {items.length}</Eyebrow>
          {items.length === 0 ? <div style={{ fontSize: 12, color: C.text3 }}>None.</div> : (
            <div style={{ border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden', background: C.panel }}>
              {items.map((it, i) => { const m = manifestById(detail, it.id); const isOpen = open === it.id; return (
                <div key={it.id || i} style={{ borderTop: i ? '1px solid ' + C.line : 'none' }}>
                  <div style={{ display: 'grid', gridTemplateColumns: narrow ? '1fr auto' : 'minmax(0,1fr) 110px 90px 90px auto', gap: 12, alignItems: 'center', padding: '10px 14px' }}>
                    <span role="button" tabIndex={0} onClick={() => setOpen(isOpen ? null : it.id)} style={{ cursor: 'pointer', fontFamily: MONO, fontSize: 12.5, color: C.text, display: 'flex', gap: 8, alignItems: 'center' }}><Icon name="chevron" size={12} color={C.text3} style={{ transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />{it.title}</span>
                    {!narrow && <Status s={it.status} />}
                    {!narrow && <span style={{ fontSize: 11.5, color: C.text3, fontFamily: MONO }}>{m ? `${m.file_count} files` : '—'}</span>}
                    {!narrow && <span style={{ fontSize: 11.5, color: C.text3, fontFamily: MONO }}>{m ? fmtBytes(m.total_bytes) : ''}</span>}
                    {m && m.status === 'verified' ? <Btn small kind="ghost" icon="download" onClick={() => saveZip(api, pid, it.id, it.prefix || '', it.zip)}>zip</Btn> : <span style={{ fontSize: 11, color: C.text3 }}>{m ? m.status : ''}</span>}
                  </div>
                  {isOpen && <div style={{ padding: '0 14px 12px' }}><FileTable api={api} pid={pid} files={filesOf(detail, it.id)} /></div>}
                </div>); })}
            </div>
          )}
        </div>
      ))}
      <div>
        <Eyebrow style={{ marginBottom: 8 }}>Source captures · {uploads.length}</Eyebrow>
        <div style={{ fontSize: 12, color: C.text3, marginBottom: 8 }}>{system_note}</div>
        <div style={{ border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden', background: C.panel }}>
          {uploads.map((u, i) => (
            <div key={u.id} style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '9px 14px', borderTop: i ? '1px solid ' + C.line : 'none' }}>
              <Thumb api={api} pid={pid} fid={(u.meta_json || {}).thumb_file_id} size={34} radius={6} />
              <span style={{ flex: 1, minWidth: 0, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{u.filename}<span style={{ color: C.text3 }}> · {u.capture_mode}</span></span>
              <span style={{ fontSize: 11, fontFamily: MONO, color: C.text3 }}>{fmtBytes(u.size)} · {short(u.sha256, 12)}</span><Status s={u.status} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
const system_note = 'Original captures are kept with their SHA-256.';

function SettingsScreen({ api, pid, detail, system, refresh, onDeleted, refreshSystem }) {
  const p = detail.project;
  const [name, setName] = React.useState(p.name);
  const [uidIn, setUidIn] = React.useState('');
  const [role, setRole] = React.useState('viewer');
  const [confirm, setConfirm] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const [ok, setOk] = React.useState(null);
  const owner = p.role === 'owner';
  const prof = ((system && system.robot_profiles) || []).find((x) => x.id === p.robot_profile) || {};
  const call = async (fn, msg) => { setBusy(true); setErr(null); setOk(null); try { await fn(); setOk(msg); refresh(); } catch (e) { setErr(e.message); } finally { setBusy(false); } };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 760, margin: '0 auto' }}>
      <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Settings</h2>
      {err && <ErrorLine msg={err} />}{ok && <div style={{ fontSize: 12.5, color: C.ok }}>{ok}</div>}
      <Panel title="Project">
        <Label>Name</Label>
        <div style={{ display: 'flex', gap: 8 }}><input value={name} onChange={(e) => setName(e.target.value)} style={inputCss()} /><Btn disabled={busy || name.trim() === p.name || !name.trim()} onClick={() => call(() => api.req(`/api/projects/${pid}`, { method: 'PATCH', json: { name } }), 'Renamed.')}>Rename</Btn></div>
        <div style={{ marginTop: 16 }}><Kv k="Robot profile" v={prof.name || p.robot_profile} mono={false} /><Kv k="Units" v="Metric (cm, g, m in MJCF)" mono={false} /><Kv k="Project ID" v={p.id} /><Kv k="Created" v={new Date(p.created_at).toLocaleString()} mono={false} /></div>
        <div style={{ fontSize: 11.5, color: C.text3, marginTop: 10, lineHeight: 1.5 }}>{system && system.robot_note}</div>
      </Panel>
      <Panel title="Hugging Face access (mesh generation)"><HfConnect system={system} onSaved={refreshSystem} /></Panel>
      <Panel title="Collaborators & privacy">
        <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.55, marginBottom: 12 }}>Only project members can access its files. Add a user ID to invite a collaborator.</div>
        {owner ? (<>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <input value={uidIn} onChange={(e) => setUidIn(e.target.value.trim())} placeholder="Mana user ID" style={{ ...inputCss(false, true), flex: '1 1 220px' }} />
            <select value={role} onChange={(e) => setRole(e.target.value)} style={{ ...inputCss(), width: 120 }}><option value="viewer">Viewer</option><option value="editor">Editor</option></select>
            <Btn disabled={busy || !uidIn} onClick={() => call(() => api.req(`/api/projects/${pid}/members`, { json: { member_uid: uidIn, role } }), 'Collaborator added.')}>Add</Btn>
          </div>
          <div style={{ marginTop: 10 }}>{(detail.members || []).map((m) => <div key={m.id} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '7px 0', borderTop: '1px solid ' + C.line, fontSize: 12.5 }}><Icon name="user" size={14} color={C.text3} /><span style={{ fontFamily: MONO, flex: 1 }}>{m.uid}</span><span style={{ color: C.text3 }}>{m.role}</span><Btn small kind="quiet" onClick={() => call(() => api.req(`/api/projects/${pid}/members`, { json: { member_uid: m.uid, role: 'none' } }), 'Removed.')}>Remove</Btn></div>)}
            {(detail.members || []).length === 0 && <div style={{ fontSize: 12, color: C.text3 }}>No collaborators.</div>}</div>
        </>) : <div style={{ fontSize: 12, color: C.text3 }}>You are a {p.role} of this project.</div>}
      </Panel>
      {owner && <Panel title="Archive or delete">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 16 }}>
          <span style={{ fontSize: 12.5, color: C.text2, flex: 1 }}>{p.archived ? 'Archived. All files are kept.' : 'Hide from the dashboard; keep all files.'}</span>
          <Btn icon="archive" disabled={busy} onClick={() => call(() => api.req(`/api/projects/${pid}`, { method: 'PATCH', json: { archived: !p.archived } }), p.archived ? 'Unarchived.' : 'Archived.')}>{p.archived ? 'Unarchive' : 'Archive'}</Btn>
        </div>
        <div style={{ fontSize: 12.5, color: C.text2, marginBottom: 8, lineHeight: 1.5 }}>Permanently deletes uploads, versions, runs and exports. Cannot be undone. Type the project name to confirm.</div>
        <div style={{ display: 'flex', gap: 8 }}><input value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Project name confirmation" placeholder={p.name} style={inputCss()} /><Btn kind="danger" icon="trash" disabled={busy || confirm !== p.name || projectHasActiveWork(detail)} title={projectHasActiveWork(detail)?'Cancel active or queued jobs first':undefined} onClick={() => call(async () => { const fresh=await api.req(`/api/projects/${pid}`); if(projectHasActiveWork(fresh))throw new Error('Cancel active or queued jobs before deleting.'); if(fresh.project.name!==confirm)throw new Error('Project name changed. Confirm the current name.'); await api.req(`/api/projects/${pid}`, { method: 'DELETE' }); onDeleted(); }, 'Deleted.')}>Delete project</Btn></div>
      </Panel>}
    </div>
  );
}

/* ── app ─────────────────────────────────────────────────────────────────────── */
function readUrl() { try { const u = new URLSearchParams(window.location.search); return { pid: u.get('p') || null, tab: u.get('tab') || null, step: u.get('step') || null, assist: u.get('assistMode') || null, helpRun: u.get('helpRun') || null }; } catch (e) { return { pid: null, tab: null, step: null, assist: null, helpRun: null }; } }


function WorkcellOverview({api,pid,detail,system,narrow,goto}) {
 const assets=detail.asset_version||[], scenes=detail.scene_version||[], exports=detail.gym_export||[], uploads=detail.upload||[];
 const accepted=assets.filter(a=>a.status==='accepted'), ready=exports.filter(e=>e.status==='ready');
 const current=scenes.find(s=>s.status==='validated'), robot=detail.project.settings_json?.robot;
 const twin=(detail.pipeline_run||[]).find(r=>r.kind==='workcell'&&r.status==='complete')?.output_json?.workcell;
 const genericReady=(detail.pipeline_run||[]).filter(r=>r.kind==='workcell_export'&&r.status==='complete'&&r.output_json?.export).length;
 const runs=(detail.pipeline_run||[]).filter(r=>r.kind!=='copilot'), active=runs.find(r=>['queued','running'].includes(r.status));
 const steps=[['capture','01','Capture',`${uploads.filter(u=>u.status==='ready').length} validated inputs`,uploads.some(u=>u.status==='ready')],['robot','02','Identify robot',robot?.name||'Confirm the exact model',!!robot?.manifest_id],['review','03','Review assets',`${accepted.length} accepted versions`,!!accepted.length],['workcell','04','Assemble workcell','Measured table + reviewed placements',!!twin],['export','05','Train your way',`${ready.length+genericReady} portable packages`,!!(ready.length+genericReady)]];
 const next=steps.find(s=>!s[4])||steps[4];
 return <div style={{maxWidth:1100,margin:'0 auto',display:'flex',flexDirection:'column',gap:22}}>
 <header style={{display:'flex',gap:16,alignItems:'flex-end',flexWrap:'wrap'}}><div style={{flex:1,minWidth:220}}><h1 style={{margin:'0 0 8px',fontSize:26,fontWeight:600,letterSpacing:'-.025em'}}>{detail.project.name}</h1><p style={{margin:0,color:C.text2,fontSize:14,lineHeight:1.6}}>A versioned workcell, not a single task. Reconstruct the objects, confirm the robot, and build an environment for your own policies.</p></div><Btn icon="sparkle" onClick={()=>goto('setup')}>Set up with Claude</Btn></header>
 <div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'repeat(5,minmax(0,1fr))',border:'1px solid '+C.line,borderRadius:12,overflow:'hidden',background:C.panel}}>{steps.map(([tab,num,title,sub,done])=><button key={tab} onClick={()=>goto(tab)} style={{padding:16,textAlign:'left',border:0,borderRight:'1px solid '+C.line,borderBottom:narrow?'1px solid '+C.line:0,background:'transparent',cursor:'pointer',color:C.text,fontFamily:FONT}}><div style={{display:'flex',justifyContent:'space-between',color:done?C.ok:C.text3,fontFamily:MONO,fontSize:11}}>{num}{done&&<Icon name="check" size={14}/>}</div><div style={{fontSize:14,fontWeight:600,marginTop:16}}>{title}</div><div style={{fontSize:12,lineHeight:1.5,color:C.text3,marginTop:5}}>{sub}</div></button>)}</div>
 {active?<Panel title="Live job" aside={<Status s={active.status}/>}><StageRail run={active}/><div style={{fontSize:12,color:C.text3,marginTop:10}}>Job {active.id}. The previous version remains available while this runs.</div></Panel>:<div style={{display:'flex',alignItems:'center',gap:14,padding:'16px 0',borderBottom:'1px solid '+C.line,flexWrap:'wrap'}}><div style={{flex:1}}><Eyebrow>Next step</Eyebrow><div style={{fontSize:15,marginTop:4}}>{next[2]}</div></div><Btn kind="primary" icon="chevron" onClick={()=>goto(next[0])}>Continue</Btn></div>}
 <div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1.4fr 1fr',gap:18}}><div style={{minWidth:0}}>{(twin||current)?<Viewer api={api} pid={pid} scene={twin?twin.preview:pathMap(filesOf(detail,current.manifest_id))} traj={null} height={narrow?300:380}/>:<Panel title="Your workcell starts here"><p style={{fontSize:14,lineHeight:1.7,color:C.text2}}>Film the robot, table and objects from several viewpoints. Include a known scale reference. Add close-up captures for each object you need to interact with.</p><div style={{display:'flex',gap:8,flexWrap:'wrap'}}><Btn kind="primary" icon="upload" onClick={()=>goto('capture')}>Upload captures</Btn><Btn onClick={()=>goto('robot')}>Choose robot model</Btn></div></Panel>}</div><Panel title="Evidence, not assumptions"><Kv k="Robot" v={robot?.name||'Not confirmed for workcell'} mono={false}/><Kv k="Accepted assets" v={accepted.length}/><Kv k="Scene versions" v={scenes.length}/><Kv k="Validated packages" v={ready.length+genericReady}/><Kv k="Stored artifacts" v={fmtBytes(detail.storage_bytes)}/><p style={{fontSize:12,color:C.text3,lineHeight:1.65,margin:'14px 0 0'}}>A model that compiles is not a calibrated robot. A stable drop test is not a precision certificate. Record measurements and validate sensor/control mappings before hardware deployment.</p><Btn style={{marginTop:14}} onClick={()=>goto('deploy')}>Deployment readiness</Btn></Panel></div>
 <div style={{display:'flex',gap:16,flexWrap:'wrap',fontSize:11.5,color:C.text3,borderTop:'1px solid '+C.line,paddingTop:14}}><span>SpacetimeDB · versioned state and files</span><span>Claude · reviewed setup proposals</span><span>Microsoft TRELLIS · asset reconstruction</span></div>
 </div>;
}
function DeploymentReadiness({detail,goto}) {
 const robot=detail.project.settings_json?.robot, exported=(detail.gym_export||[]).filter(x=>x.status==='ready');
 const requirements=[['Robot identity','Confirm manufacturer, exact revision, end effector, joint order, limits and control modes.'],['Frame alignment','Measure table and object poses in the robot base frame. Report held-out landmark errors; do not use training points as validation.'],['Sensor parity','Generic workcell exports expose simulator state. Replace it with hardware-observable features or a validated state estimator before policy transfer.'],['Control parity','Map units, action frequency, latency, actuator type and joint limits. Calibration is not policy fine-tuning.'],['Safety validation','Test at low speeds with reach and collision checks, an accessible emergency stop, and a qualified operator. Browser controls never actuate hardware.'],['Transfer evidence','Record the policy hash, scene version, calibration revision, trial count, failure modes and measured position/orientation errors.']];
 return <div style={{maxWidth:1000,margin:'0 auto'}}><Eyebrow>Close the loop</Eyebrow><h2 style={{fontSize:25,margin:'8px 0'}}>Deploy with evidence</h2><p style={{fontSize:14,color:C.text2,lineHeight:1.7}}>Download your simulation and train with any compatible algorithm. Zero-shot transfer means no real-world policy fine-tuning; it does not eliminate robot calibration, safety checks or physical validation.</p><Panel title="Current package"><Kv k="Selected robot" v={robot?.name||'Not confirmed'} mono={false}/><Kv k="Ready exports" v={exported.length}/><Kv k="Hardware transfer" v="Not verified by this workspace" mono={false}/><Btn onClick={()=>goto('export')} style={{marginTop:12}}>Open exports</Btn></Panel><div style={{marginTop:20}}>{requirements.map(([title,text],i)=><div key={title} style={{display:'grid',gridTemplateColumns:'28px 1fr',gap:12,padding:'16px 0',borderBottom:'1px solid '+C.line}}><span style={{color:C.accent,fontFamily:MONO,fontSize:12}}>{String(i+1).padStart(2,'0')}</span><div><strong style={{fontSize:14}}>{title}</strong><p style={{fontSize:13,color:C.text2,lineHeight:1.65,margin:'5px 0 0'}}>{text}</p></div></div>)}</div><p style={{fontSize:12,color:C.text3,lineHeight:1.65}}>The upstream repository contains SO-100 calibration, live-twin and policy-runner tools. Their presence is not evidence that another robot or your particular workcell is validated.</p></div>;
}

function JobBox({api,pid,run}){return run?<Panel title={run.kind.replaceAll('_',' ')} aside={<Status s={runDisplayStatus(run)}/>}><StageRail run={run}/>{run.error&&<div style={{marginTop:10}}><ErrorLine msg={run.error}/></div>}<details style={{marginTop:10,fontSize:12,color:C.text3}}><summary style={{cursor:'pointer'}}>Execution log</summary><LogPanel api={api} pid={pid} run={run} height={160}/></details></Panel>:null;}
function RobotScreen({api,pid,detail,system,narrow,refresh,goto}){
 const [catalog,setCatalog]=React.useState([]),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false),[upload,setUpload]=React.useState(''),[selected,setSelected]=React.useState(''),[confirmed,setConfirmed]=React.useState(false),[importId,setImportId]=React.useState('');
 const uploads=(detail.upload||[]).filter(x=>x.status==='ready'), runs=detail.pipeline_run||[];
 const detections=runs.filter(x=>x.kind==='detect_robot'),imports=runs.filter(x=>x.kind==='import_robot');
 const detection=detections[0], result=detection?.output_json||{};
 const latestImport=imports.find(x=>x.id===importId)||imports.find(x=>x.output_json?.robot_import)||imports[0], model=latestImport?.output_json?.robot_import;
 const active=runs.find(x=>['detect_robot','import_robot'].includes(x.kind)&&['queued','running'].includes(x.status));
 const current=detail.project.settings_json?.robot, writable=['owner','editor'].includes(detail.project.role||'owner');
 React.useEffect(()=>{let off=false;api.req('/api/robots').then(x=>{if(!off)setCatalog(x.catalog||[])}).catch(e=>{if(!off)setError(e.message)});return()=>{off=true}},[api]);
 React.useEffect(()=>{setConfirmed(false)},[selected]);
 const act=async(path,body)=>{setError('');setBusy(true);try{await api.req(`/api/projects/${pid}`+path,{json:body});await refresh()}catch(e){setError(e.message)}finally{setBusy(false)}};
 const capture=uploads.find(x=>x.id===upload)||uploads.find(x=>x.capture_mode==='scene')||uploads[0];
 return <div style={{maxWidth:1100,margin:'0 auto',display:'flex',flexDirection:'column',gap:18}}><header><h2 style={{margin:'0 0 8px',fontSize:24}}>Identify. Check. Import.</h2><p style={{fontSize:14,lineHeight:1.6,color:C.text2}}>Claude compares captured frames against an allowlisted model library. Appearance is evidence, not proof: confirm the exact revision and gripper before using the model.</p></header>
 <div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1fr 1fr',gap:18}}><Panel title="Identify from a capture"><Label>Validated video or image</Label><select aria-label="Robot detection capture" value={capture?.id||''} onChange={e=>setUpload(e.target.value)} style={inputCss()}>{!uploads.length&&<option value="">No validated captures yet</option>}{uploads.map(x=><option key={x.id} value={x.id}>{x.filename}</option>)}</select><div style={{display:'flex',gap:8,marginTop:14,flexWrap:'wrap'}}><Btn processing kind="primary" icon="sparkle" disabled={!capture||busy||!!active||!writable} onClick={()=>act('/runs',{kind:'detect_robot',upload_ids:[capture.id],frames:4})}>Identify with Claude</Btn><Btn icon="upload" onClick={()=>goto('capture')}>Add capture</Btn></div><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>No model is downloaded or selected by detection. Claude may abstain. It cannot supply arbitrary executable downloads.</p></Panel><Panel title="Current workcell robot"><Kv k="Model" v={current?.name||'Not confirmed'} mono={false}/><Kv k="Source revision" v={current?.commit?String(current.commit).slice(0,12):'Not imported'}/><Kv k="License" v={current?.license||'Check before export'} mono={false}/><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Compiler validation confirms the model loads. Hardware calibration, actuator mapping and real-world accuracy are separate.</p></Panel></div>
 {error&&<ErrorLine msg={error}/>}<JobBox api={api} pid={pid} run={active||((detection?.status==='failed')?detection:null)}/>
 {detection?.status==='complete'&&<Panel title="Claude’s findings" aside={<span style={{fontSize:11,color:C.text3}}>Self-reported confidence, not a probability</span>}>{result.abstained&&<p style={{color:C.warn,fontSize:14,lineHeight:1.6}}>{result.abstain_reason||'No reliable match. Choose a model manually or upload clearer views.'}</p>}{result.unlisted_robot&&<p style={{fontSize:13,color:C.text2}}>Possible unlisted robot: {result.unlisted_robot}. Not available for import.</p>}<div style={{display:'flex',gap:10,flexWrap:'wrap',marginBottom:12}}>{Object.entries(result.frames||{}).map(([name,fid])=><Thumb key={fid} api={api} pid={pid} fid={fid} size={78}/>)}</div>{(result.candidates||[]).map(c=><button key={c.library_id} onClick={()=>setSelected(c.library_id)} style={{width:'100%',textAlign:'left',padding:'13px 0',background:'none',border:0,borderTop:'1px solid '+C.line,color:C.text,fontFamily:FONT,cursor:'pointer'}}><div style={{display:'flex',gap:8,alignItems:'center'}}><strong style={{fontSize:14}}>{c.name}</strong><span style={{fontSize:12,color:C.text3}}>{c.confidence} confidence</span><span style={{marginLeft:'auto',color:C.accent,fontSize:12}}>Review model →</span></div><p style={{fontSize:13,color:C.text2,lineHeight:1.55,margin:'6px 0 0'}}>{(c.reasons||[]).join(' ')}</p></button>)}</Panel>}
 <Panel title="Model library"><p style={{fontSize:13,color:C.text2,lineHeight:1.6,marginTop:0}}>Pinned MuJoCo Menagerie MJCF models. These are not arbitrary URDF imports. Only reviewed catalog sources are available.</p><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'repeat(2,minmax(0,1fr))',gap:10}}>{catalog.map(m=><button key={m.id} onClick={()=>setSelected(m.id)} style={{textAlign:'left',border:'1px solid '+(selected===m.id?C.accentLine:C.line),borderRadius:10,padding:14,background:selected===m.id?C.accentSoft:'transparent',color:C.text,cursor:'pointer',fontFamily:FONT}}><div style={{display:'flex',justifyContent:'space-between',gap:8,fontSize:14,fontWeight:600}}>{m.name}{selected===m.id&&<Icon name="check" color={C.accent}/>}</div><div style={{fontSize:12,color:C.text3,marginTop:7}}>{m.maker} · {m.dof} DOF · {m.license}</div><div style={{fontSize:12,color:C.text2,marginTop:6,lineHeight:1.5}}>{m.note}</div></button>)}</div>{!!selected&&<><label style={{display:'flex',gap:10,marginTop:18,fontSize:13,color:C.text2,lineHeight:1.6}}><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} style={{accentColor:C.accent,marginTop:4}}/>I checked the robot’s manufacturer, model variant and end effector against this catalog entry. Importing a simulation model does not certify hardware compatibility.</label><Btn processing kind="primary" icon="download" style={{marginTop:14}} disabled={!confirmed||busy||!!active||!writable} onClick={()=>act('/runs',{kind:'import_robot',library_id:selected,user_confirmed_model:true,detection_run_id:detection?.status==='complete'?detection.id:undefined})}>Import and verify model</Btn></>}</Panel>
 {model&&<Panel title={model.name+' · imported model'} aside={<Status s="ready"/>}><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1.4fr 1fr',gap:18}}><div style={{minWidth:0}}><Viewer api={api} pid={pid} scene={model.preview} traj={null} height={340}/></div><div><Kv k="Actuators" v={model.stats?.nu}/><Kv k="Joints" v={(model.stats?.joints||[]).join(', ')} mono={false}/><Kv k="Source revision" v={String(model.commit||'').slice(0,12)}/><Kv k="License" v={model.license}/><Kv k="Files" v={model.file_count}/><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Stored in SpacetimeDB and read back. Review this preview and select it for the workcell only if it matches your robot.</p><Btn kind="primary" icon="check" disabled={busy||!writable||current?.import_run_id===latestImport.id} onClick={()=>act('/robot',{import_run_id:latestImport.id,confirm:true})}>{current?.import_run_id===latestImport.id?'Selected for workcell':'Use this model for workcell'}</Btn></div></div>{imports.filter(r=>r.output_json?.robot_import).length>1&&<select aria-label="Imported robot version" style={{...inputCss(),marginTop:14}} value={latestImport.id} onChange={e=>setImportId(e.target.value)}>{imports.filter(r=>r.output_json?.robot_import).map(r=><option key={r.id} value={r.id}>{r.output_json.robot_import.name} · {fmtAgo(r.created_at)}</option>)}</select>}</Panel>}
 {current&&<Btn kind="primary" icon="layers" onClick={()=>goto('workcell')}>Compose the workcell</Btn>}
 </div>;
}

function WorkcellComposer({api,pid,detail,system,narrow,refresh,goto}){
 const saved=detail.project.settings_json?.workcell||{}, robot=detail.project.settings_json?.robot;
 const avs=(detail.asset_version||[]).filter(a=>a.status==='accepted');
 const groups=Object.values(avs.reduce((m,a)=>{(m[a.object_name]||(m[a.object_name]=[])).push(a);return m;},{}));
 const [chosen,setChosen]=React.useState(()=>groups.map(g=>g[0].id));
 const [table,setTable]=React.useState(()=>({length_cm:saved.table?.length_m?String(saved.table.length_m*100):'',width_cm:saved.table?.width_m?String(saved.table.width_m*100):'',height_cm:saved.table?.height_m?String(saved.table.height_m*100):'',source:saved.table?.source||'unknown',note:saved.table?.note||''}));
 const [places,setPlaces]=React.useState(saved.placements||{}),[busy,setBusy]=React.useState(false),[error,setError]=React.useState(''),[ack,setAck]=React.useState(false),[dirty,setDirty]=React.useState(false),[shown,setShown]=React.useState('');
 const sig=JSON.stringify(saved);
 React.useEffect(()=>{if(!dirty){setTable({length_cm:saved.table?.length_m?String(saved.table.length_m*100):'',width_cm:saved.table?.width_m?String(saved.table.width_m*100):'',height_cm:saved.table?.height_m?String(saved.table.height_m*100):'',source:saved.table?.source||'unknown',note:saved.table?.note||''});setPlaces(saved.placements||{});}},[sig]);
 const runs=(detail.pipeline_run||[]).filter(r=>r.kind==='workcell'),active=runs.find(r=>['queued','running'].includes(r.status));
 const run=runs.find(r=>r.id===shown)||runs.find(r=>r.output_json?.workcell)||runs[0],wc=run?.output_json?.workcell;
 const dims=['length_cm','width_cm','height_cm'].map(k=>Number(table[k])/100),validDims=dims.every((n,i)=>Number.isFinite(n)&&(i===2?n>0:(n>=.2&&n<=4)));
 const canBuild=robot?.manifest_id&&chosen.length>0&&chosen.length<=8&&validDims&&table.source!=='unknown'&&(table.source!=='estimated'||ack);
 const changeTable=(k,v)=>{setDirty(true);setAck(false);setTable(t=>({...t,[k]:v}));};
 const changePlace=(n,k,v)=>{setDirty(true);setPlaces(p=>({...p,[n]:{...(p[n]||{}),[k]:v,source:'manual'}}));};
 const normalized=()=>{const pl={};for(const a of avs.filter(x=>chosen.includes(x.id))){const p=places[a.object_name];if(!p||p.x_m==null||p.y_m==null||p.x_m===''||p.y_m==='')continue;const x=Number(p.x_m),y=Number(p.y_m),yaw=Number(p.yaw_deg||0);if(![x,y,yaw].every(Number.isFinite))throw new Error('Object positions must be finite numbers');pl[a.object_name]={x_m:x,y_m:y,yaw_deg:yaw,source:p.source||'manual'};}return {table:{length_m:dims[0],width_m:dims[1],height_m:dims[2],source:table.source,note:table.note},placements:pl};};
 const save=async(build)=>{setBusy(true);setError('');try{const workcell=normalized();await api.req(`/api/projects/${pid}`,{method:'PATCH',json:{settings:{workcell}}});if(build)await api.req(`/api/projects/${pid}/runs`,{json:{kind:'workcell',asset_version_ids:chosen,table:workcell.table,placements:workcell.placements,seed:0}});setDirty(false);await refresh();}catch(e){setError(e.message)}finally{setBusy(false)}};
 return <div style={{maxWidth:1100,margin:'0 auto',display:'flex',flexDirection:'column',gap:18}}><header><Eyebrow>Robot + table + objects</Eyebrow><h2 style={{fontSize:24,margin:'8px 0'}}>Compose the workcell</h2><p style={{fontSize:14,lineHeight:1.6,color:C.text2}}>Build a scene from a confirmed robot and accepted object assets. Enter measured placements or use a clearly labelled generated layout. Automatic reconstruction from a whole-workcell video remains a separate beta path.</p></header>
 {!robot?.manifest_id&&<Panel title="Confirm a robot first"><Btn kind="primary" onClick={()=>goto('robot')}>Identify or select robot</Btn></Panel>}
 <div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1fr 1fr',gap:18}}><Panel title="Table dimensions"><div style={{display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:10}}>{[['length_cm','Length'],['width_cm','Width'],['height_cm','Height']].map(([k,l])=><div key={k}><Label>{l} · cm</Label><input type="number" aria-label={'Table '+l} value={table[k]} onChange={e=>changeTable(k,e.target.value)} min={20} max={k==='height_cm'?150:400} step="any" style={inputCss(false,true)}/></div>)}</div><div style={{marginTop:14}}><Label>Where the dimensions came from</Label><select aria-label="Table dimensions source" value={table.source} onChange={e=>changeTable('source',e.target.value)} style={inputCss()}>{SOURCES.map(([v,l])=><option key={v} value={v}>{l}</option>)}</select></div><div style={{marginTop:12}}><Label>Measurement note</Label><input value={table.note} onChange={e=>changeTable('note',e.target.value)} style={inputCss()} placeholder="Tape measure, table edge to edge"/></div>{table.source==='estimated'&&<label style={{display:'flex',gap:8,marginTop:12,fontSize:12,color:C.warn,lineHeight:1.6}}><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/>I reviewed these estimates. They remain estimates, not calibrated dimensions.</label>}<p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Robot base origin is on the tabletop, z = 0. Table height places the floor below it. Verify the base location and orientation on your physical workcell.</p></Panel>
 <Panel title="Accepted object versions" aside={<Btn small icon="plus" onClick={()=>goto('capture')}>Add object</Btn>}>{!groups.length?<p style={{fontSize:13,color:C.text3}}>Create and accept at least one object in this project. Existing projects and rejected versions are not automatically mixed into this scene.</p>:groups.map(g=><div key={g[0].object_name} style={{padding:'11px 0',borderBottom:'1px solid '+C.line}}><label style={{display:'flex',alignItems:'center',gap:9,fontSize:13}}><input type="checkbox" checked={g.some(a=>chosen.includes(a.id))} onChange={e=>setChosen(s=>e.target.checked?[...s.filter(id=>!g.some(a=>a.id===id)),g[0].id]:s.filter(id=>!g.some(a=>a.id===id)))} style={{accentColor:C.accent}}/><strong>{g[0].object_name}</strong></label><select aria-label={'Version for '+g[0].object_name} style={{...inputCss(),marginTop:8}} value={g.find(a=>chosen.includes(a.id))?.id||g[0].id} onChange={e=>setChosen(s=>[...s.filter(id=>!g.some(a=>a.id===id)),e.target.value])}>{g.map(a=><option key={a.id} value={a.id}>Asset v{a.version} · {fmtAgo(a.created_at)}</option>)}</select></div>)}<p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Up to 8 distinct object names. A scene pins each exact asset version.</p></Panel></div>
 {avs.some(a=>chosen.includes(a.id))&&<Panel title="Object placement in the robot base frame"><p style={{fontSize:12,color:C.text3,lineHeight:1.6,marginTop:0}}>Blank x/y uses a generated row in front of the robot. This is not a recovered real-world pose. Enter measured x/y and yaw to align your cell.</p>{avs.filter(a=>chosen.includes(a.id)).map(a=>{const n=a.object_name,p=places[n]||{};return <div key={a.id} style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1fr repeat(3,minmax(80px,1fr))',gap:12,padding:'12px 0',borderTop:'1px solid '+C.line,alignItems:'end'}}><div><strong style={{fontSize:13}}>{n}</strong><div style={{fontSize:11,color:C.text3,marginTop:5}}>{p.source||'generated layout if blank'}</div></div>{[['x_m','x · m'],['y_m','y · m'],['yaw_deg','yaw · degrees']].map(([k,l])=><div key={k}><Label>{l}</Label><input aria-label={n+' '+l} type="number" step="any" value={p[k]??''} onChange={e=>changePlace(n,k,e.target.value)} style={inputCss(false,true)} placeholder={k==='yaw_deg'?'0':'automatic'}/></div>)}</div>})}</Panel>}
 {error&&<ErrorLine msg={error}/>}<div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}><Btn processing kind="primary" icon="layers" disabled={!canBuild||busy||!!active} onClick={()=>save(true)}>{busy?'Saving…':'Build and check workcell'}</Btn><Btn disabled={!validDims||table.source==='unknown'||busy} onClick={()=>save(false)}>Save setup</Btn><span style={{fontSize:12,color:C.text3}}>{dirty?'Unsaved edits':'Setup is unchanged'}</span></div>
 <JobBox api={api} pid={pid} run={active||(run?.status==='failed'?run:null)}/>
 {wc&&<Panel title={'Workcell · '+(run.id||'').slice(-8)} aside={<Status s={wc.result}/>}><Viewer api={api} pid={pid} scene={wc.preview} traj={wc.traj} height={narrow?340:480}/><div style={{display:'flex',gap:16,flexWrap:'wrap',fontSize:12,color:C.text2,marginTop:12}}><span>{(wc.objects||[]).length} objects</span><span>{wc.library_id}</span><span>Scene hash {String(wc.model_hash||'').slice(0,12)}</span></div>{(wc.warnings||[]).map((w,i)=><p key={i} style={{fontSize:12,color:C.warn,lineHeight:1.6}}>{w}</p>)}{Object.entries(wc.metrics||{}).map(([n,m])=><Kv key={n} k={n} v={`${m.tilt_deg}° tilt · ${m.settled?'settled':'moving'} · ${m.on_table?'on table':'off table'}`} mono={false}/>)}<p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>This checks simulated stability. It does not validate robot recognition, dimensional accuracy, collision behavior against real materials, or policy transfer.</p>{runs.filter(r=>r.output_json?.workcell).length>1&&<select aria-label="Workcell version" style={inputCss()} value={run.id} onChange={e=>setShown(e.target.value)}>{runs.filter(r=>r.output_json?.workcell).map(r=><option key={r.id} value={r.id}>{r.id.slice(-8)} · {r.output_json.workcell.result} · {fmtAgo(r.created_at)}</option>)}</select>}<Btn kind="primary" icon="box" style={{marginTop:14}} onClick={()=>goto('export')}>Export and train</Btn></Panel>}
 </div>;
}
function WorkcellExports({api,pid,detail,narrow,refresh,goto}){
 const runs=detail.pipeline_run||[], builds=runs.filter(r=>r.kind==='workcell'&&r.status==='complete'&&r.output_json?.workcell?.result==='pass'), exps=runs.filter(r=>r.kind==='workcell_export');
 const [id,setId]=React.useState(''),[busy,setBusy]=React.useState(false),[error,setError]=React.useState('');
 const build=builds.find(r=>r.id===id)||builds[0],active=exps.find(r=>['queued','running'].includes(r.status));
 const go=async()=>{if(!build)return;setBusy(true);setError('');try{await api.req(`/api/projects/${pid}/runs`,{json:{kind:'workcell_export',workcell_run_id:build.id}});await refresh()}catch(e){setError(e.message)}finally{setBusy(false)}};
 return <div style={{display:'flex',flexDirection:'column',gap:16}}><div><Eyebrow>Bring your own policy</Eyebrow><h2 style={{fontSize:24,margin:'8px 0'}}>Portable workcell environment</h2><p style={{fontSize:14,color:C.text2,lineHeight:1.65}}>Robot, table, objects and asset files in one MuJoCo scene, plus a Gymnasium interface with custom reward hooks. Compilation, clean installation and file-integrity checks are separate from real-world fidelity.</p></div><Panel title="Build a generic environment">{build?<><Label>Checked workcell</Label><select aria-label="Workcell to export" value={build.id} onChange={e=>setId(e.target.value)} style={inputCss()}>{builds.map(r=><option key={r.id} value={r.id}>{r.id.slice(-8)} · {r.output_json.workcell.library_id} · {r.output_json.workcell.objects.length} objects</option>)}</select><p style={{fontSize:12,color:C.warn,lineHeight:1.6}}>Generic observations include simulator object state. They are not directly available on hardware; implement a matching sensor/state-estimation interface before transfer.</p><Btn processing kind="primary" icon="box" disabled={busy||!!active} onClick={go}>Generate workcell package</Btn></>:<><p style={{fontSize:13,color:C.text3}}>Compose a workcell and pass its simulated stability check first.</p><Btn onClick={()=>goto('workcell')}>Compose workcell</Btn></>}{error&&<ErrorLine msg={error}/>}</Panel>
 {active&&<JobBox api={api} pid={pid} run={active}/>}{exps.filter(r=>r.status!=='queued'&&r.status!=='running').map(r=>{const e=r.output_json?.export,v=e?.validation||{};if(!e)return <JobBox key={r.id} api={api} pid={pid} run={r}/>;return <Panel key={r.id} title={e.env_id} aside={<Status s={r.status==='complete'?'ready':r.status}/>}><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1fr 1fr',gap:16}}><div><Kv k="Package" v={e.module}/><Kv k="Version" v={e.package_version}/><Kv k="Archive" v={fmtBytes(e.bytes)}/></div><div>{[['Fresh install',v.install?.ok],['Gymnasium checker',v.smoke?.ok],['Seeded reset',v.smoke?.seeded_reset_reproducible],['Read-back integrity',v.persistence?.ok]].map(([l,ok])=><div key={l} style={{display:'flex',gap:8,fontSize:13,padding:'6px 0',color:ok?C.ok:C.text3}}><Icon name={ok?'check':'reset'} size={14}/>{l}</div>)}</div></div><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>{e.observation_note} {e.fidelity_note}</p><Btn kind="primary" icon="download" disabled={r.status!=='complete'||!v.persistence?.ok} onClick={()=>saveFile(api,pid,e.archive_file_id,e.archive_name)}>Download workcell package</Btn><details style={{marginTop:14,fontSize:12,color:C.text2}}><summary>Files and provenance</summary><FileTable api={api} pid={pid} files={filesOf(detail,e.manifest_id)}/></details></Panel>})}</div>;
}
function ExportScreen(props){const legacy=(props.detail.scene_version||[]).some(s=>s.status==='validated');return <div style={{maxWidth:1100,margin:'0 auto',display:'flex',flexDirection:'column',gap:28}}><WorkcellExports {...props}/>{legacy&&<details style={{borderTop:'1px solid '+C.line,paddingTop:20}}><summary style={{cursor:'pointer',fontSize:14,fontWeight:600,marginBottom:20}}>Existing SO-100 reference-task exports ({(props.detail.gym_export||[]).length})</summary><LegacyExportScreen {...props}/></details>}</div>;}

function SimulationScreen(props){
 const {api,pid,detail,narrow,refresh,goto}=props;
 const runs=(detail.pipeline_run||[]).filter(r=>r.kind==='workcell'&&r.output_json?.workcell),[id,setId]=React.useState(''),[seed,setSeed]=React.useState('1'),[busy,setBusy]=React.useState(false),[error,setError]=React.useState('');
 const selected=runs.find(r=>r.id===id)||runs[0],wc=selected?.output_json?.workcell,active=(detail.pipeline_run||[]).find(r=>r.kind==='workcell'&&['queued','running'].includes(r.status));
 if(!wc)return <LegacySimulationScreen {...props}/>;
 const simulate=async()=>{setBusy(true);setError('');try{await api.req(`/api/projects/${pid}/runs`,{json:{kind:'workcell',asset_version_ids:wc.asset_version_ids,table:detail.project.settings_json?.workcell?.table,placements:detail.project.settings_json?.workcell?.placements,seed:Math.max(0,Math.floor(Number(seed)||0))}});await refresh()}catch(e){setError(e.message)}finally{setBusy(false)}};
 return <div style={{display:'flex',flexDirection:'column',gap:16,maxWidth:1100,margin:'0 auto'}}><header style={{display:'flex',gap:12,alignItems:'center',flexWrap:'wrap'}}><h2 style={{fontSize:24,margin:0}}>Workcell simulation</h2><Status s={wc.result}/><select aria-label="Simulation workcell version" value={selected.id} onChange={e=>setId(e.target.value)} style={{...inputCss(),width:'auto',marginLeft:'auto'}}>{runs.map(r=><option key={r.id} value={r.id}>{r.id.slice(-8)} · seed {r.output_json.workcell.seed} · {r.output_json.workcell.result}</option>)}</select></header><Viewer api={api} pid={pid} scene={wc.preview} traj={wc.traj} height={narrow?360:500}/><p style={{fontSize:13,lineHeight:1.6,color:C.text2}}>Server-computed MuJoCo dynamics replayed in the browser. This is a drop/settle stability test, not trained-policy playback or proof of real-world transfer.</p><div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}><Label>Seed</Label><input type="number" min="0" value={seed} onChange={e=>setSeed(e.target.value)} aria-label="Workcell simulation seed" style={{...inputCss(false,true),width:100}}/><Btn processing icon="play" kind="primary" disabled={busy||!!active} onClick={simulate}>Test current setup with this seed</Btn><Btn onClick={()=>goto('workcell')}>Edit placements</Btn><Btn onClick={()=>goto('export')}>Export</Btn></div><p style={{fontSize:12,color:C.text3}}>A new test creates a new workcell run using the current confirmed robot and saved table/placement settings. Earlier trajectories remain unchanged.</p>{error&&<ErrorLine msg={error}/>}<JobBox api={api} pid={pid} run={active}/><Panel title="Object results">{Object.entries(wc.metrics||{}).map(([n,m])=><Kv key={n} k={n} v={`${m.tilt_deg}° tilt · ${m.settled?'settled':'moving'} · ${m.on_table?'on table':'off table'}`} mono={false}/>)}</Panel>{(detail.scene_version||[]).length>0&&<details><summary style={{cursor:'pointer',fontSize:13,color:C.text2}}>Earlier single-object scenes</summary><div style={{marginTop:16}}><LegacySimulationScreen {...props}/></div></details>}</div>;
}

/* Setup assistant (typed Claude copilot). Concatenated before UserDiv; uses ui2.jsx primitives
   C, FONT, MONO, Btn, Panel, Icon, ErrorLine, SourceChip, Eyebrow, fmtAgo. Backend: backend/copilot.py.
   Claude only proposes; every change is applied by an explicit approval here, re-validated server-side. */
const CP_STATUS = {
  pending: ['Awaiting your approval', C.accent], applied: ['Applied', C.ok], rejected: ['Dismissed', C.text3],
  stale: ['Out of date', C.warn], failed: ['Failed', C.err],
};
const cpFmt = (v, unit) => v == null || v === '' ? '—' : (typeof v === 'number' ? String(+v.toFixed(4)) : String(v)) + (unit && unit !== '' ? ' ' + unit : '');
const CP_STARTERS = ['What is blocking this project?', 'Help me enter the table measurements', 'Check my object measurements'];

function CopilotProposal({ api, pid, p, onDecided, canWrite }) {
  const [confirming, setConfirming] = React.useState(false);
  const [ack, setAck] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);
  const hasEst = p.changes.some((c) => (c.basis || c.source) === 'estimated');
  const [label, color] = CP_STATUS[p.status] || CP_STATUS.pending;
  const act = async (action) => {
    setBusy(true); setErr(null);
    try {
      await api.req(`/api/projects/${pid}/copilot/proposals/${p.id}/${action}`, { json: action === 'approve' ? { confirm: true, acknowledge_estimates: hasEst ? ack : undefined } : {} });
      setConfirming(false); onDecided();
    } catch (e) { setErr(e.message); if (/out of date|changed after/i.test(e.message)) onDecided(); }
    finally { setBusy(false); }
  };
  const d = p.decision || {};
  return (
    <div style={{ border: '1px solid ' + (p.status === 'pending' ? C.line2 : C.line), borderRadius: 10, background: C.raised, padding: '11px 12px', marginTop: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Icon name={p.kind === 'table' ? 'table' : p.kind === 'robot_hint' ? 'cube' : p.kind === 'placement' ? 'focus' : 'review'} size={14} color={C.text2} />
        <span style={{ fontSize: 13, fontWeight: 500, color: C.text }}>{p.title}</span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color }}>{label}</span>
      </div>
      <div style={{ marginTop: 8, display: 'grid', gridTemplateColumns: 'minmax(90px,1fr) auto auto', columnGap: 12, rowGap: 5, alignItems: 'center', fontSize: 12 }}>
        {p.changes.map((c) => (
          <React.Fragment key={c.field}>
            <span style={{ color: C.text2 }}>{c.label}{c.note && <span style={{ color: C.warn }} title={c.note}> ·</span>}</span>
            <span style={{ fontFamily: MONO, color: C.text, whiteSpace: 'nowrap' }}><span style={{ color: C.text3 }}>{cpFmt(c.current, c.unit)} → </span>{cpFmt(c.proposed, c.unit)}</span>
            <SourceChip source={c.basis || c.source} />
          </React.Fragment>
        ))}
      </div>
      {p.rationale && <div style={{ fontSize: 11.5, color: C.text3, marginTop: 8, lineHeight: 1.5 }}>{p.rationale}</div>}
      {p.changes.some((c) => c.note) && <div style={{ fontSize: 11, color: C.warn, marginTop: 4 }}>Some numbers were not in your own words, so they are labelled Estimated.</div>}
      {p.status === 'applied' && p.kind === 'robot_hint' && p.mode !== 'workflow' && <div style={{ fontSize: 11.5, color: C.text2, marginTop: 8, lineHeight: 1.5 }}>Saved as a hint only. The robot is confirmed when you import and confirm <span style={{ fontFamily: MONO }}>{(d.next && d.next.library_id) || ''}</span> in robot setup.</div>}
      {p.status === 'applied' && d.after && d.after.rebuild_run_id && <div style={{ fontSize: 11.5, color: C.text2, marginTop: 8 }}>A rebuild run was started with the revised values; review it when it finishes.</div>}
      {(p.status === 'stale' || p.status === 'failed') && d.error && <div style={{ fontSize: 11.5, color: p.status === 'stale' ? C.warn : C.err, marginTop: 8 }}>{d.error}</div>}
      {p.status === 'pending' && canWrite && !confirming && (
        <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
          <Btn small kind="solid" icon="check" onClick={() => setConfirming(true)}>Review and apply</Btn>
          <Btn small kind="quiet" disabled={busy} onClick={() => act('reject')}>Dismiss</Btn>
        </div>
      )}
      {p.status === 'pending' && confirming && (
        <div style={{ marginTop: 10, padding: '10px 11px', borderRadius: 8, border: '1px solid ' + C.accentLine, background: C.accentSoft }}>
          <div style={{ fontSize: 12, color: C.text, lineHeight: 1.5 }}>
            {p.mode === 'workflow' ? (p.kind === 'robot_hint' ? 'Selects this model in the project draft. It is imported and checked when you press Build.'
                : p.kind === 'placement' ? 'Sets a manual placement in the draft (table frame). It is used once the robot base position on the table is set in the layout step.'
                : 'Edits the project draft. Nothing is built until you press Build.')
              : p.kind === 'robot_hint' ? 'Record this model as your choice. It does not import or confirm the robot.'
              : p.kind === 'object_measurements' ? 'This saves the revised values and starts a rebuild of the asset as a new version.'
              : 'This changes the project’s workcell settings. Scenes built afterwards use these values.'} If anything changed since this was proposed, nothing is applied.
          </div>
          {hasEst && <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12, color: C.text2, marginTop: 8, cursor: 'pointer' }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} style={{ accentColor: C.accent, marginTop: 2 }} />
            Some values are estimates. They stay labelled Estimated and are not treated as measurements.</label>}
          <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
            <Btn small kind="primary" disabled={busy || (hasEst && !ack)} onClick={() => act('approve')}>{busy ? 'Applying…' : 'Confirm and apply'}</Btn>
            <Btn small kind="quiet" disabled={busy} onClick={() => { setConfirming(false); setAck(false); }}>Cancel</Btn>
          </div>
        </div>
      )}
      {err && <div style={{ marginTop: 8 }}><ErrorLine msg={err} /></div>}
    </div>
  );
}

function SetupCopilot({ api, pid, detail, system, refresh, narrow }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);
  const [text, setText] = React.useState('');
  const [pending, setPending] = React.useState(null);
  const scroller = React.useRef(null);
  const load = React.useCallback(async () => {
    try { const j = await api.req(`/api/projects/${pid}/copilot`); setData(j); setErr(null); }
    catch (e) { setErr(e.message); }
  }, [api, pid]);
  React.useEffect(() => { setData(null); if (api && api.base && pid) load(); }, [pid, api && api.base]);
  const turns = (data && data.turns) || [];
  React.useEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight; }, [turns.length, pending]);
  const send = async (msg) => {
    const m = (msg != null ? msg : text).trim();
    if (!m || pending) return;
    setPending(m); setText(''); setErr(null);
    try {
      const r = await api.req(`/api/projects/${pid}/copilot/turns`, { json: { message: m } });
      setData((d) => d ? { ...d, turns: [...d.turns, r.turn] } : d);
    } catch (e) { setErr(e.message); setText(m); }
    finally { setPending(null); }
  };
  const decided = async () => { await load(); if (refresh) refresh(); };
  const wc = (data && data.workcell) || {};
  const t = wc.table;
  const robot = data && data.robot;
  const canSend = data && data.can_use_model;
  const lr = data && data.grounding && data.grounding.latest_run;
  return (
    <Panel title="Setup assistant" aside={<span style={{ fontSize: 11, color: C.text3 }}>Claude · proposes, you approve</span>} pad={false}>
      <div style={{ padding: '10px 16px', borderBottom: '1px solid ' + C.line, display: 'flex', gap: narrow ? 8 : 18, flexWrap: 'wrap', fontSize: 11.5, color: C.text3 }}>
        <span>Table <span style={{ fontFamily: MONO, color: t ? C.text2 : C.text3 }}>{t ? `${cpFmt(t.length_m)} × ${cpFmt(t.width_m)} × ${cpFmt(t.height_m)} m` : 'not set'}</span></span>
        <span>Placements <span style={{ fontFamily: MONO, color: C.text2 }}>{Object.keys(wc.placements || {}).length}</span></span>
        <span>Robot <span style={{ color: C.text2 }}>{robot && robot.confirmed ? (robot.confirmed.name || robot.confirmed.library_id) : robot && robot.selected ? `${robot.selected} (selected)` : robot && robot.hint ? `hint: ${robot.hint.library_id}` : 'not confirmed'}</span></span>
        {data && data.workflow && <span>Draft <span style={{ fontFamily: MONO, color: C.text2 }}>rev {data.workflow.revision}</span>{data.workflow.blockers ? <span style={{ color: C.warn }}> · {data.workflow.blockers} to resolve</span> : null}</span>}
        {lr && <span>Latest run <span style={{ color: C.text2 }}>{lr.kind} · {String(lr.status).replace('_', ' ')}{lr.stage ? ' at ' + lr.stage : ''}</span></span>}
      </div>
      <div ref={scroller} style={{ maxHeight: narrow ? 380 : 460, overflowY: 'auto', padding: '6px 16px 12px' }}>
        {!data && !err && <div style={{ fontSize: 12.5, color: C.text3, padding: '14px 0' }}>Reading project state…</div>}
        {data && !turns.length && !pending && (
          <div style={{ padding: '14px 0 4px' }}>
            <div style={{ fontSize: 13, color: C.text, lineHeight: 1.55 }}>Ask what blocks this project, or tell me measurements as you take them.</div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
              {CP_STARTERS.map((s) => <Btn key={s} small kind="ghost" disabled={!canSend} onClick={() => send(s)}>{s}</Btn>)}
            </div>
          </div>
        )}
        {turns.map((tn) => (
          <div key={tn.id} style={{ padding: '12px 0', borderBottom: '1px solid ' + C.line }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
              <span style={{ fontSize: 12.5, color: C.text2, flex: 1, whiteSpace: 'pre-wrap', userSelect: 'text' }}>{tn.message}</span>
              <span style={{ fontSize: 10.5, color: C.text3, flex: 'none' }}>{fmtAgo(tn.at)}</span>
            </div>
            <div style={{ fontSize: 13, color: C.text, lineHeight: 1.6, marginTop: 7, whiteSpace: 'pre-wrap', userSelect: 'text' }}>{tn.reply}</div>
            {tn.blockers && tn.blockers.length > 0 && (
              <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {tn.blockers.map((b, i) => <div key={i} style={{ display: 'flex', gap: 7, fontSize: 12, lineHeight: 1.45 }}>
                  <Icon name="alert" size={13} color={C.warn} style={{ marginTop: 2 }} /><span><span style={{ color: C.text }}>{b.title}</span>{b.detail && <span style={{ color: C.text3 }}> · {b.detail}</span>}</span></div>)}
              </div>
            )}
            {(tn.proposals || []).map((p) => <CopilotProposal key={p.id} api={api} pid={pid} p={p} onDecided={decided} canWrite={data.can_write} />)}
            {(tn.rejected_suggestions || []).length > 0 && <div style={{ fontSize: 11, color: C.text3, marginTop: 6 }}>Not offered: {tn.rejected_suggestions.map((r) => r.reason).join('; ')}</div>}
          </div>
        ))}
        {pending && (
          <div style={{ padding: '12px 0' }}>
            <div style={{ fontSize: 12.5, color: C.text2, whiteSpace: 'pre-wrap' }}>{pending}</div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: C.text3, marginTop: 8 }}>
              <div className="p2g-spin" style={{ width: 12, height: 12, borderRadius: 99, border: '2px solid ' + C.line2, borderTopColor: C.accent }} />Reading project state and thinking…</div>
          </div>
        )}
      </div>
      <div style={{ padding: 12, borderTop: '1px solid ' + C.line }}>
        {err && <div style={{ marginBottom: 8 }}><ErrorLine msg={err} /></div>}
        {data && !canSend && <div style={{ fontSize: 11.5, color: C.text3, marginBottom: 8 }}>{data.can_write ? 'The assistant runs on the workspace owner’s credits and is available on their projects only.' : 'You have view access: the conversation is read-only.'}</div>}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <textarea value={text} rows={2} disabled={!canSend} maxLength={2000} placeholder="e.g. The table is 120 by 60 cm, 75 cm high"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
            style={{ flex: 1, minWidth: 0, resize: 'vertical', minHeight: 40, maxHeight: 160, borderRadius: 8, border: '1px solid ' + C.line2, background: C.sunken, color: C.text, padding: '9px 10px', fontSize: 13, fontFamily: FONT, outline: 'none', lineHeight: 1.45, boxSizing: 'border-box' }} />
          <Btn kind="primary" disabled={!canSend || !text.trim() || !!pending} onClick={() => send()}>{pending ? 'Thinking…' : 'Send'}</Btn>
        </div>
        <div style={{ fontSize: 10.5, color: C.text3, marginTop: 6 }}>Guesses are labelled Estimated. Nothing changes until you approve.</div>
      </div>
    </Panel>
  );
}



function WorkflowSourceVideo({api,pid,fid}){const url=useFileUrl(api,pid,fid);return <div style={{border:'1px solid '+C.line,borderRadius:12,background:C.panel,overflow:'hidden'}}><div style={{padding:'11px 13px',fontSize:12,fontWeight:550,borderBottom:'1px solid '+C.line}}>Source capture</div>{url?<video controls playsInline preload="metadata" src={url} style={{width:'100%',maxHeight:430,display:'block',background:'#18181b'}}/>:<div style={{height:180,display:'grid',placeItems:'center',fontSize:12,color:C.text3}}>Loading saved source video…</div>}<p style={{fontSize:11.5,color:C.text3,lineHeight:1.6,padding:'0 13px'}}>Original capture. Not synced with the replay.</p></div>}

const FLOW_STEPS=[['capture','Upload'],['inputs','Review inputs'],['assets','Build assets'],['scene','Align & simulate'],['export','Approve & export']];
function WorkflowProgress({step,onStep,mode}){const ix=FLOW_STEPS.findIndex(s=>s[0]===step);return <nav aria-label="Project progress" style={{display:'flex',gap:0,borderBottom:'1px solid '+C.line,overflowX:'auto',paddingBottom:0,marginBottom:24}}>{FLOW_STEPS.filter(s=>mode!=='object'||s[0]!=='scene').map(([k,l],i)=><button key={k} onClick={()=>onStep(k)} aria-current={k===step?'step':undefined} style={{flex:'1 0 130px',display:'flex',gap:8,alignItems:'center',padding:'13px 10px',border:0,borderBottom:'2px solid '+(k===step?C.accent:'transparent'),background:'none',color:k===step?C.text:C.text3,fontFamily:FONT,fontSize:12.5,fontWeight:k===step?600:400,cursor:'pointer'}}><span style={{fontFamily:MONO,fontSize:10,color:k===step?C.accent:C.text3}}>{String(i+1).padStart(2,'0')}</span>{mode==='object'&&k==='export'?'Approve & export asset':l}</button>)}</nav>}
function WorkflowUpload({api,pid,detail,system,mode,selected,onSelect,onNext,refresh}){
 const [items,setItems]=React.useState([]),[drag,setDrag]=React.useState(false),[error,setError]=React.useState('');
 const input=React.useRef(null),aborts=React.useRef(new Map()),localUrls=React.useRef(new Map());
 const uploads=(detail.upload||[]).filter(x=>x.status==='ready'&&!x.meta_json?.derived_from&&(mode!=='workspace'||isVideoName(x.filename)));
 React.useEffect(()=>()=>{for(const a of aborts.current.values())a.abort();for(const u of localUrls.current.values())URL.revokeObjectURL(u)},[]);
 const start=async(file,key)=>{const control=new AbortController();aborts.current.set(key,control);const upd=x=>setItems(a=>a.map(i=>i.key===key?{...i,...x}:i));try{const row=await uploadFile(api,pid,file,mode==='workspace'?'scene':'object',p=>upd(p),control.signal);upd({phase:row.status==='ready'?'done':'invalid',row,error:row.error});if(row.status==='ready'){onSelect(mode==='workspace'?[row.id]:[...new Set([...selected,row.id])]);await refresh();}}catch(e){if(e.name!=='AbortError')upd({phase:'error',error:e.message})}finally{aborts.current.delete(key)}};
 const add=list=>{setError('');for(const file of Array.from(list)){if(mode==='workspace'&&!isVideoName(file.name)){setError('Use a video for workspace conversion. Single-object mode also accepts images.');continue;}const limit=system?.limits?.upload_bytes;if(limit&&file.size>limit){setError('File exceeds '+fmtBytes(limit));continue;}const key=crypto.randomUUID();setItems(a=>[...a,{key,file,phase:'queued',frac:0}]);localUrls.current.set(key,URL.createObjectURL(file));start(file,key);}};
 const pending=items.filter(x=>!['done','error','invalid'].includes(x.phase)),chosen=uploads.filter(u=>selected.includes(u.id));
 return <div style={{display:'flex',flexDirection:'column',gap:20}}><div><h2 style={{fontSize:27,letterSpacing:'-.025em',fontWeight:550,margin:'0 0 8px'}}>Start with what’s real.</h2><p style={{fontSize:14,color:C.text2,lineHeight:1.7,maxWidth:700}}>{mode==='workspace'?'Film the robot, table and nearby objects from several angles.':'Circle one object with the camera.'}</p></div>
 <div role="button" tabIndex={0} onClick={()=>input.current?.click()} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();input.current?.click()}}} onDragOver={e=>{e.preventDefault();setDrag(true)}} onDragLeave={()=>setDrag(false)} onDrop={e=>{e.preventDefault();setDrag(false);add(e.dataTransfer.files)}} style={{border:'1px dashed '+(drag?C.accent:C.line2),borderRadius:16,background:drag?C.accentSoft:C.panel,padding:'36px 20px',textAlign:'center',cursor:'pointer'}}><Icon name="film" size={30} color={C.accent}/><div style={{fontSize:16,fontWeight:550,marginTop:13}}>Drop your {mode==='workspace'?'workspace video':'video or photo'} here</div><div style={{fontSize:12.5,color:C.text3,marginTop:8}}>or click to choose</div><input ref={input} type="file" accept={mode==='workspace'?'video/*':'video/*,image/*'} multiple={false} style={{display:'none'}} onChange={e=>{add(e.target.files);e.target.value=''}}/></div>
 {error&&<ErrorLine msg={error}/>}{items.filter(i=>i.phase!=='done').map(i=><div key={i.key} style={{padding:'12px 0',borderBottom:'1px solid '+C.line}}><div style={{display:'flex',alignItems:'center',gap:10,fontSize:13}}><Icon name="film"/><span style={{flex:1,minWidth:0,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{i.file.name}</span><span style={{fontSize:12,color:C.text3}}>{i.phase==='uploading'?Math.round(i.frac*100)+'%':i.phase}</span></div>{!['error','invalid'].includes(i.phase)&&<div style={{height:3,background:C.line,marginTop:10}}><div style={{height:'100%',width:Math.max(4,(i.frac||0)*100)+'%',background:C.accent}}/></div>}{i.error&&<div style={{marginTop:8}}><ErrorLine msg={i.error}/><Btn small onClick={()=>start(i.file,i.key)}>Retry upload</Btn></div>}</div>)}
 {!!uploads.length&&<div><Eyebrow>Available captures</Eyebrow>{uploads.map(u=><label key={u.id} style={{display:'flex',alignItems:'center',gap:12,padding:'12px 0',borderBottom:'1px solid '+C.line,cursor:'pointer'}}><input type={mode==='workspace'?'radio':'checkbox'} name="workflow-capture" checked={selected.includes(u.id)} onChange={e=>onSelect(mode==='workspace'?[u.id]:e.target.checked?[...new Set([...selected,u.id])]:selected.filter(x=>x!==u.id))} style={{accentColor:C.accent}}/><Thumb api={api} pid={pid} fid={u.meta_json?.thumb_file_id} size={56}/><div style={{flex:1,minWidth:0}}><div style={{fontSize:13,wordBreak:'break-word'}}>{u.filename}</div><div style={{fontSize:11.5,color:C.text3,marginTop:4}}>{fmtBytes(u.size)}{u.meta_json?.duration?' · '+Number(u.meta_json.duration).toFixed(1)+' s':''} · {u.meta_json?.width}×{u.meta_json?.height}</div></div><Status s={u.status}/></label>)}</div>}
 <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:14,flexWrap:'wrap'}}><p style={{fontSize:12,color:C.text3,lineHeight:1.65,maxWidth:640,margin:0}}>{mode==='workspace'?'Include a known scale, table edges, the robot base and the floor.':'Use several clear views. Shiny or transparent surfaces may need more.'}</p><Btn kind="primary" icon="chevron" disabled={!chosen.length||pending.length>0} onClick={onNext}>Continue to inputs</Btn></div>
 </div>;
}
function WorkflowField({label,unit,entry,onChange,type='number',required=false}){
 const v=entry||{value:'',source:'unknown',note:''};return <div style={{minWidth:0}}><Label>{label}{unit?' · '+unit:''}{required?' *':''}</Label><input aria-label={label} type={type} step={type==='number'?'any':undefined} value={v.value??''} onChange={e=>onChange({...v,value:e.target.value,source:v.source==='unknown'?'supplied':v.source,note:v.note||''})} style={inputCss(v.value==null||v.value===''?false:false,type==='number')}/><select aria-label={label+' source'} value={v.source||'unknown'} onChange={e=>onChange({...v,source:e.target.value})} style={{...inputCss(),height:25,fontSize:10.5,padding:'0 5px',marginTop:5,color:C.text3,background:'transparent'}}>{SOURCES.map(([k,l])=><option key={k} value={k}>{l}</option>)}</select>{v.note&&<details style={{fontSize:10.5,color:C.text3,marginTop:4}}><summary aria-label={label+' source note'} style={{cursor:'pointer',color:v.source==='estimated'?C.warn:C.text3}}>{v.source==='estimated'?'Estimated · why':'Note'}</summary><div style={{marginTop:3,lineHeight:1.5,overflowWrap:'anywhere'}}>{v.note}</div></details>}</div>;
}

function WorkflowEvidence({api,pid,fid,bbox,label}){
 const url=useFileUrl(api,pid,fid);if(!fid)return null;
 const b=Array.isArray(bbox)&&bbox.length===4?bbox:null;
 return <figure style={{margin:0}}><div style={{position:'relative',display:'inline-block',width:'100%',maxWidth:240,background:C.sunken,borderRadius:10,overflow:'hidden'}}>{url?<img src={url} alt={label||'Capture evidence'} style={{display:'block',width:'100%',height:'auto'}}/>:<div style={{height:150,display:'grid',placeItems:'center',fontSize:12,color:C.text3}}>Loading evidence frame…</div>}{b&&url&&<div style={{position:'absolute',left:(b[0]*100)+'%',top:(b[1]*100)+'%',width:((b[2]-b[0])*100)+'%',height:((b[3]-b[1])*100)+'%',border:'2px solid #F0905F',boxSizing:'border-box',pointerEvents:'none'}}/>}</div><figcaption style={{fontSize:10.5,color:C.text3,marginTop:5}}>Capture evidence · {label||'selected frame'}</figcaption></figure>;
}
function WorkflowObject({obj,onChange,index,api,pid,frames,narrow,onAttach}){
 const fileInput=React.useRef(null),[uploading,setUploading]=React.useState(false),[uploadError,setUploadError]=React.useState('');
 const crop=(obj.crops||[]).find(c=>(obj.selected_crop_ids||[]).includes(c.id))||(obj.crops||[])[0];obj={...obj,evidence_file_id:crop?.file_id,coverage_note:obj.coverage?.note};const fields=obj.fields||{},missing=OBJ_FIELDS.filter(([k,, ,req])=>req&&(!(Number(fields[k]?.value)>0)||fields[k]?.source==='unknown'));
 const change=(k,v)=>onChange({...obj,fields:{...fields,[k]:v}});
 return <section style={{border:'1px solid '+C.line,borderRadius:12,background:C.panel,overflow:'hidden',opacity:obj.include===false?.75:1}}><header style={{display:'flex',gap:12,alignItems:'center',padding:'15px 18px',borderBottom:'1px solid '+C.line}}><input aria-label={'Include '+obj.name} type="checkbox" checked={obj.include!==false} onChange={e=>onChange({...obj,include:e.target.checked})} style={{accentColor:C.accent}}/><div style={{flex:1,minWidth:0}}><div style={{fontSize:11,color:C.text3,marginBottom:4}}>OBJECT {String(index+1).padStart(2,'0')}</div><input aria-label={'Object '+(index+1)+' name'} value={obj.name||''} onChange={e=>onChange({...obj,name:e.target.value})} style={{...inputCss(),fontSize:14,fontWeight:550}}/></div><span style={{fontSize:11,color:missing.length?C.warn:C.ok}}>{obj.include===false?'Excluded':missing.length?missing.length+' required fields missing':'Inputs complete'}</span></header>
 <div style={{display:'grid',gridTemplateColumns:!narrow&&obj.evidence_file_id?'170px minmax(0,1fr)':'1fr',gap:18,padding:18}}>{obj.evidence_file_id&&<div><WorkflowEvidence api={api} pid={pid} fid={obj.evidence_file_id} bbox={null} label={obj.label||obj.name}/><p style={{fontSize:11,color:C.text3,lineHeight:1.6}}>{obj.coverage_note||obj.reason||'Check that the capture shows enough of this object to reconstruct it.'}</p></div>}<div><div style={{display:'grid',gridTemplateColumns:narrow?'repeat(2,minmax(0,1fr))':'repeat(3,minmax(0,1fr))',gap:'16px 12px'}}>{OBJ_FIELDS.map(([k,l,u,req])=><WorkflowField key={k} label={l} unit={u} required={req} type={k==='material'?'text':'number'} entry={fields[k]} onChange={v=>change(k,v)}/>)}</div>{obj.exclusion_reason&&<p style={{fontSize:12,color:C.warn,lineHeight:1.6,marginBottom:0}}>{obj.exclusion_reason}</p>}
 <div style={{marginTop:18,borderTop:'1px solid '+C.line,paddingTop:12}}><Label>Source views for this object</Label><div style={{display:'flex',gap:8,overflowX:'auto',paddingBottom:7}}>{(obj.crops||[]).map(c=><label key={c.id} style={{display:'flex',flexDirection:'column',gap:5,alignItems:'center',cursor:'pointer'}}><Thumb api={api} pid={pid} fid={c.file_id} size={50}/><input aria-label={'Use crop at '+c.time_s+' seconds'} type="checkbox" checked={(obj.selected_crop_ids||[]).includes(c.id)} onChange={e=>onChange({...obj,selected_crop_ids:e.target.checked?[...new Set([...(obj.selected_crop_ids||[]),c.id])]:(obj.selected_crop_ids||[]).filter(id=>id!==c.id)})} style={{accentColor:C.accent}}/></label>)}</div><Btn small icon="upload" disabled={uploading} onClick={()=>fileInput.current?.click()}>{uploading?'Uploading close-up…':'Add a close-up photo or clip'}</Btn><input ref={fileInput} type="file" accept="image/*,video/*" style={{display:'none'}} onChange={async e=>{const f=e.target.files?.[0];e.target.value='';if(!f)return;setUploading(true);setUploadError('');try{await onAttach(obj.id,f)}catch(err){setUploadError(err.message)}finally{setUploading(false)}}}/>{(obj.extra_upload_ids||[]).length>0&&<span style={{fontSize:11,color:C.text3,marginLeft:8}}>{obj.extra_upload_ids.length} attached close-up(s)</span>}{uploadError&&<ErrorLine msg={uploadError}/>}</div>
 </div></div></section>;
}

function PrimaryWorkflow({api,pid,detail,system,narrow,refresh,goto,step,onStep,autoResume,showProgress=true}){
 const [data,setData]=React.useState(null),[draft,setDraft]=React.useState(null),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(''),[dirty,setDirty]=React.useState(false),[ack,setAck]=React.useState(false),[finalAck,setFinalAck]=React.useState(false),[fallbackAck,setFallbackAck]=React.useState(false),[remote,setRemote]=React.useState(false),[repeatAsk,setRepeatAsk]=React.useState(null);
 const resumed=React.useRef(false),attaching=React.useRef(false),loadErrRef=React.useRef('');
 const dirtyRef=React.useRef(false),draftRef=React.useRef(null),dataRef=React.useRef(null),mounted=React.useRef(true);
 const ingest=x=>{dataRef.current=x;setData(x);if(!dirtyRef.current){draftRef.current=x.workflow;setDraft(x.workflow);setRemote(false)}else if(x.revision!==draftRef.current?.revision)setRemote(true);};
 const load=React.useCallback(async()=>{try{const x=await api.req(`/api/projects/${pid}/workflow`);if(mounted.current){ingest(x);if(loadErrRef.current){const le=loadErrRef.current;loadErrRef.current='';setError(e=>e===le?'':e)}}return x}catch(e){if(mounted.current){loadErrRef.current=e.message;setError(e.message)}}},[api,pid]);   // reads keep polling and clear their own error on recovery; actions are never re-sent
 React.useEffect(()=>{mounted.current=true;load();const t=setInterval(load,3000);return()=>{mounted.current=false;clearInterval(t)}},[load]);
 React.useEffect(()=>{if(data&&autoResume&&!resumed.current){resumed.current=true;const w=data.workflow;const ph=w?.build?.phase;const next=ph==='exported'?'export':w?.build?.preview?(w.mode==='object'?'export':'scene'):w?.build&&ph!=='cancelled'?'assets':w?.analysis?.status==='complete'||w?.primary_upload_id?'inputs':'capture';onStep(next,true)}},[data,autoResume]);
 React.useEffect(()=>{setFinalAck(false);setFallbackAck(false)},[data?.workflow?.build?.id,data?.workflow?.build?.preview?.run_id,data?.workflow?.build?.preview?.scene_version_id]);
 const change=next=>{dirtyRef.current=true;setDirty(true);draftRef.current=next;setDraft(next);setAck(false);setFinalAck(false)};
 const save=async()=>{const d=draftRef.current;if(!d)throw new Error('Project inputs have not loaded');if(!dirtyRef.current)return dataRef.current;const x=await api.req(`/api/projects/${pid}/workflow`,{method:'PUT',json:{base_revision:d.revision,mode:d.mode,primary_upload_id:d.primary_upload_id,table:d.table,objects:d.objects,robot:{selected_library_id:d.robot?.selected_library_id||null,arms:armsPayload(d.robot?.arms)},layout:{alignment:d.layout?.alignment||{}}}});dirtyRef.current=false;setDirty(false);ingest({...dataRef.current,...x});return x;};
 const action=async(kind,fn)=>{setBusy(kind);setError('');try{await fn()}catch(e){setError(e.message)}finally{if(mounted.current)setBusy('')}};
 const wfMaint=data?.checkpoints?.maintenance, wfHold=wfMaint&&wfMaint.hold?wfMaint:null, gateHold=wfHold||true;   // workflow GET is polled every 3 s, so it is the fresher hold source here
 const repeatInfo=data?.checkpoints?.ack||(data?.workflow?.build?.recovery?.requires_ack?data.workflow.build.recovery:null);
 const repeatNames=(o=>[...new Set((o?.objects||[]).map(x=>x.name||x.object_id).filter(Boolean))])(repeatAsk&&typeof repeatAsk==='object'&&repeatAsk.objects?repeatAsk:repeatInfo);
 const analyze=()=>action('analysis',async()=>{await save();const d=draftRef.current;await api.req(`/api/projects/${pid}/workflow/analysis`,{json:{upload_id:d.primary_upload_id,frames:8}});await load()});
 const build=(repeatOk)=>action('build',async()=>{const x=await save();try{await api.req(`/api/projects/${pid}/workflow/build`,{json:{base_revision:x.revision,confirm:true,acknowledge_estimates:ack,...(repeatOk===true?{acknowledge_provider_repeat:true}:{})}})}catch(e){if(e.code==='ack_required'){setRepeatAsk(e.body&&e.body.recovery||repeatInfo||{});await load();return}throw e}setRepeatAsk(null);await load();await refresh();onStep('assets')});
 const rebuildScene=()=>action('compose',async()=>{const x=await save();await api.req(`/api/projects/${pid}/workflow/compose`,{json:{base_revision:x.revision}});await load();await refresh()});
 if(!draft)return <div className="w-full" style={{padding:20}}>{error?<><ErrorLine msg={error}/><Btn style={{marginTop:12}} onClick={load}>Retry</Btn></>:<span style={{color:C.text3,fontSize:13}}>Loading saved workflow…</span>}</div>;
 const wf=data?.workflow||draft,mode=draft.mode||'workspace',analysis=wf.analysis,building=wf.build,analysisActive=['queued','running'].includes(analysis?.status),buildActive=['queued','building','accepting','layout','composing','exporting'].includes(building?.phase),objects=draft.objects||[],included=objects.filter(o=>o.include),arms=draft.robot?.arms||[],catalog=data?.catalog||[];
 const readyCount=included.filter(o=>OBJ_FIELDS.every(([k,,,req])=>!req||(Number(o.fields?.[k]?.value)>0&&o.fields?.[k]?.source!=='unknown'))).length;
 const est=included.some(o=>Object.values(o.fields||{}).some(f=>f?.source==='estimated'))||(mode==='workspace'&&Object.values(draft.table||{}).some(f=>f?.source==='estimated'));
 const readonly=data?.can_write===false;
 const mutateObj=(id,obj)=>change({...draft,objects:objects.map(o=>o.id===id?obj:o)});
 const setArm=(id,patch)=>{const d=draftRef.current;change({...d,robot:{...d.robot,arms:(d.robot?.arms||[]).map(a=>a.id===id?{...a,...patch}:a)}})};
 const changeTable=(k,v)=>change({...draft,table:{...draft.table,[k]:v}});
 const attachObject=async(id,file)=>{if(attaching.current)throw new Error('Wait for the current close-up upload to finish.');attaching.current=true;setBusy('upload');try{const row=await uploadFile(api,pid,file,'object',()=>{});if(row.status!=='ready')throw new Error(row.error||'Close-up could not be validated');const d=draftRef.current;change({...d,objects:d.objects.map(o=>o.id===id?{...o,extra_upload_ids:[...new Set([...(o.extra_upload_ids||[]),row.id])]}:o)});await refresh();}finally{attaching.current=false;setBusy('');}};
 const saveButton=<Btn disabled={!dirty||!!busy||readonly} onClick={()=>action('save',save)}>{busy==='save'?'Saving…':'Save inputs'}</Btn>;
 const bstate=building?{kind:'workflow',stages_json:building.steps||[],status:buildActive?'running':building.phase,error:building.error,id:building.id}:null;
 const sv=detail.scene_version?.find(s=>s.id===building?.preview?.scene_version_id),wcr=detail.pipeline_run?.find(r=>r.id===building?.preview?.run_id),wc=wcr?.output_json?.workcell;
 const previewScene=wc?.preview||(sv?pathMap(filesOf(detail,sv.manifest_id)):building?.preview?.files?.scene||building?.preview?.files),previewTraj=wc?.traj||building?.preview?.files?.traj||null;
 const latestExport=building?.export,exportRun=detail.pipeline_run?.find(r=>r.id===latestExport?.run_id),exportData=exportRun?.output_json?.export;
 const fieldTable=draft.table||{},layout=building?.layout||{},manual=layout.status==='manual';
 const approve=()=>action('approve',async()=>{if(dirtyRef.current)throw new Error('Save and re-compose the preview after editing inputs before approving export.');await api.req(`/api/projects/${pid}/workflow/approve`,{json:{confirm:true,build_id:building.id,preview_ref:building.preview.run_id||building.preview.scene_version_id,accept_provisional_layout:fallbackAck}});await load();await refresh()});
 return <div className="w-full">{showProgress&&<WorkflowProgress step={step} onStep={onStep} mode={mode}/>}
 {(dirty||data?.stored===false||analysisActive||buildActive)&&<div role="status" style={{display:'flex',justifyContent:'space-between',gap:10,marginBottom:16,fontSize:11.5,color:C.text3,flexWrap:'wrap'}}><span>{dirty?'Unsaved changes':data?.stored===false?'Not saved yet':''}</span><span>{analysisActive?'Claude is analyzing':''}{buildActive?(analysisActive?' · ':'')+building.phase.replaceAll('_',' '):''}</span></div>}
 {!building&&(detail.gym_export||[]).length>0&&<div style={{display:'flex',gap:12,alignItems:'center',justifyContent:'space-between',padding:'11px 14px',border:'1px solid '+C.line,borderRadius:10,marginBottom:16,fontSize:12,color:C.text2,flexWrap:'wrap'}}><span>{(detail.gym_export||[]).length} earlier package(s), unchanged.</span><Btn small onClick={()=>goto('export')}>Open previous exports</Btn></div>}
 {error&&<div style={{marginBottom:16}}><ErrorLine msg={error}/></div>}{remote&&<div style={{padding:12,border:'1px solid '+C.accentLine,borderRadius:10,marginBottom:16,fontSize:12,color:C.text2}}>A newer version is on the server. Your edits are kept; saving won’t overwrite it. <Btn small onClick={()=>{dirtyRef.current=false;setDirty(false);ingest(dataRef.current)}}>Load server version and discard local edits</Btn></div>}
 {step==='capture'&&<WorkflowUpload api={api} pid={pid} detail={detail} system={system} mode={mode} selected={draft.primary_upload_id?[draft.primary_upload_id]:[]} onSelect={ids=>change({...draft,primary_upload_id:ids[0]||null})} onNext={()=>action('save',async()=>{await save();onStep('inputs')})} refresh={refresh}/>}
 {step==='inputs'&&<div style={{display:'flex',flexDirection:'column',gap:20}}><header style={{display:'flex',gap:14,alignItems:'center',flexWrap:'wrap'}}><div style={{flex:1,minWidth:220}}><h2 style={{fontSize:25,fontWeight:550,margin:0}}>What’s in your {mode==='workspace'?'workspace':'capture'}?</h2></div>{analysis?.status==='complete'&&<Btn processing={gateHold} small icon="sparkle" disabled={!draft.primary_upload_id||!!busy||analysisActive||!data?.can_analyze||readonly} onClick={analyze}>{analysisActive?'Analyzing…':'Analyze again'}</Btn>}</header>
 {!draft.primary_upload_id&&<div style={{padding:'28px 22px',border:'1px dashed '+C.line2,borderRadius:14,display:'flex',flexDirection:'column',alignItems:'flex-start',gap:10}}><div style={{fontSize:16,fontWeight:600,color:C.text}}>Upload your video first</div><div style={{fontSize:13,color:C.text2}}>Claude fills in the objects, sizes and physics from it.</div><Btn kind="primary" style={{minHeight:46,padding:'10px 24px',fontSize:15,fontWeight:600,marginTop:4}} onClick={()=>onStep('capture')}>Upload video</Btn></div>}
 {!!draft.primary_upload_id&&analysis?.status!=='complete'&&<div style={{padding:'20px 22px',border:'1px solid '+C.accentLine,background:C.accentSoft,borderRadius:14,display:'flex',gap:16,alignItems:'center',flexWrap:'wrap'}}><div style={{flex:1,minWidth:220}}><div style={{fontSize:16,fontWeight:600,color:C.text}}>Let Claude fill this in</div><div style={{fontSize:13,color:C.text2,marginTop:4}}>Finds the objects and estimates sizes, mass and friction. You review everything.</div></div><Btn processing={gateHold} kind="primary" icon="sparkle" style={{minHeight:52,padding:'12px 30px',fontSize:16,fontWeight:600}} disabled={!!busy||analysisActive||!data?.can_analyze||readonly} onClick={analyze}>{analysisActive?'Analyzing video…':'Autofill with Claude'}</Btn></div>}
 {!!draft.primary_upload_id&&<>
 {analysis&&<div style={{padding:'12px 0',borderTop:'1px solid '+C.line,borderBottom:'1px solid '+C.line}}><div style={{display:'flex',alignItems:'center',gap:8}}><Status s={analysis.status}/><span style={{fontSize:12,color:C.text3}}>{analysis.cached?'Reused analysis of this exact upload':'Claude analysis'}{analysis.finished_at?' · '+fmtAgo(analysis.finished_at):''}</span></div>{analysis.summary&&<p style={{fontSize:13,lineHeight:1.65,color:C.text2}}>{analysis.summary}</p>}{analysis.error&&<ErrorLine msg={analysis.error}/>}<div style={{display:'flex',gap:8,overflowX:'auto',marginTop:10}}>{(analysis.frames||[]).slice(0,8).map(f=><Thumb key={f.file_id} api={api} pid={pid} fid={f.file_id} size={64}/>)}</div></div>}
  {analysis&&<ItemWebMatch api={api} pid={pid} data={data} draft={draft} change={change} readonly={readonly} load={load}/>}
 {!!objects.length&&<div style={{display:'flex',gap:24,flexWrap:'wrap'}}>{[['Detected',objects.length],['Included',included.length],['Dimensions & physics complete',readyCount]].map(([l,v])=><div key={l}><span style={{fontSize:24,fontFamily:MONO}}>{v}</span><div style={{fontSize:11,color:C.text3,marginTop:4}}>{l}</div></div>)}</div>}
 {mode==='workspace'&&<Panel title="Robots" aside={<span style={{fontSize:12,color:C.text3,fontFamily:MONO}}>{arms.length?arms.filter(a=>a.decision==='use').length+' of '+arms.length+' included':'none detected'}</span>}><Label>Default model</Label><select aria-label="Default simulation model" value={draft.robot?.selected_library_id||''} onChange={e=>change({...draft,robot:{...draft.robot,selected_library_id:e.target.value||null}})} style={inputCss()} disabled={readonly}><option value="">{arms.length?'Choose a default model':'Use the SO-101 default when no robot is detected'}</option>{catalog.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select>{draft.robot?.selected_origin==='assumed'&&<p style={{fontSize:12,color:C.warn,lineHeight:1.6}}>Assumed: {draft.robot.assumption||'SO-101 simulation model; not identified in the video.'} Stays labelled as an assumption.</p>}{draft.robot?.selected_origin==='user'&&<p style={{fontSize:11.5,color:C.text3,lineHeight:1.6}}>Chosen by you. Arms without their own model use it.</p>}{draft.robot?.selected_origin==='analysis'&&<p style={{fontSize:11.5,color:C.text3,lineHeight:1.6}}>Suggested by analysis; confirm before building.</p>}{draft.robot?.suggestion?.reason&&<p style={{fontSize:11.5,color:C.text3,lineHeight:1.6}}>{draft.robot.suggestion.reason}</p>}{arms.map((r,i)=><ArmRow key={r.id} arm={r} index={i} robot={draft.robot} catalog={catalog} readonly={readonly} onChange={patch=>setArm(r.id,patch)} onRemove={r._new?()=>change({...draft,robot:{...draft.robot,arms:arms.filter(a=>a.id!==r.id)}}):null}/>)}<div style={{marginTop:12}}><Btn small icon="plus" disabled={readonly} onClick={()=>change({...draft,robot:{...draft.robot,arms:[...arms,{id:'local_arm_'+crypto.randomUUID().replaceAll('-',''),_new:true,label:'Robot arm '+(arms.length+1),decision:'use',library_id:null,origin:'user',candidates:[]}]}})}>Add arm</Btn></div></Panel>}
 {mode==='workspace'&&<Panel title="Table / scale reference"><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'repeat(3,minmax(0,1fr))',gap:16}}>{[['length_m','Table length'],['width_m','Table width'],['height_m','Table height above floor']].map(([k,l])=><WorkflowField key={k} label={l} unit="m" required entry={fieldTable[k]} onChange={v=>changeTable(k,v)}/>)}</div><p style={{fontSize:12,color:C.text3,lineHeight:1.6}}>Measure where possible. Values read from pixels stay estimates.</p></Panel>}
 {objects.map((o,i)=><WorkflowObject key={o.id} obj={o} index={i} api={api} pid={pid} narrow={narrow} onChange={x=>mutateObj(o.id,x)} onAttach={attachObject}/>)}
 {!objects.length&&<div style={{padding:'26px 0',fontSize:13,color:C.text3}}>No objects yet. Use Autofill with Claude above.</div>}<div><Btn icon="plus" disabled={readonly||objects.length>=24} onClick={()=>change({...draft,objects:[...objects,{id:'local_'+crypto.randomUUID().replaceAll('-',''),name:'object_'+(objects.length+1),label:'New object',include:true,fields:emptyMeas(),selected_crop_ids:[],crops:[],extra_upload_ids:[],origin:'user',coverage:{adequate:true,note:'User-added; attach an object close-up.'}}]})}>Add an object manually</Btn></div>
 {!!analysis?.excluded?.length&&<details style={{fontSize:12.5,color:C.text3}}><summary style={{cursor:'pointer'}}>Not selected from the video ({analysis.excluded.length})</summary>{analysis.excluded.map((x,i)=><p key={i}><strong>{x.label}:</strong> {x.reason}</p>)}</details>}
 <div style={{padding:16,border:'1px solid '+C.line,borderRadius:12}}><h3 style={{fontSize:14,margin:'0 0 8px'}}>Before building</h3><p style={{fontSize:12.5,color:C.text2,lineHeight:1.65}}>Included objects need required fields and a source crop.</p>{est&&<label style={{display:'flex',gap:9,fontSize:12.5,color:C.warn,lineHeight:1.6}}><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)} style={{accentColor:C.accent,marginTop:3}}/>I accept the estimated values for this provisional simulation. They stay labelled as estimates.</label>}{!dirty&&!!data?.readiness?.blockers?.length&&<ul style={{fontSize:12,color:C.text3,paddingLeft:18,lineHeight:1.6}}>{data.readiness.blockers.map((b,i)=><li key={i}>{b.message}</li>)}</ul>}<div style={{display:'flex',gap:8,marginTop:14,flexWrap:'wrap'}}>{saveButton}<Btn processing={gateHold} kind="primary" icon="layers" disabled={!included.length||readyCount!==included.length||!!busy||analysisActive||buildActive||readonly||(est&&!ack)||!!repeatAsk} onClick={()=>repeatInfo?setRepeatAsk(repeatInfo):build(false)}>{busy==='build'?'Starting…':'Build with reviewed inputs'}</Btn></div>{repeatAsk&&<div role="alertdialog" aria-label="Confirm repeating uncertain mesh work" style={{marginTop:12,padding:'10px 12px',border:'1px solid '+C.accentLine,borderRadius:10,background:C.accentSoft}}><div style={{fontSize:12.5,color:C.text,lineHeight:1.55}}>The worker restarted while mesh generation was in flight{repeatNames.length?<> for <b>{repeatNames.join(', ')}</b></>:''}. Its outcome is unknown and no mesh was saved, so building again may repeat that TRELLIS GPU work and use quota again. Objects with a verified mesh checkpoint are reused.</div><div style={{display:'flex',gap:8,marginTop:10,flexWrap:'wrap'}}><Btn processing={gateHold} small kind="primary" disabled={!!busy} onClick={()=>build(true)}>{busy==='build'?'Starting…':'Repeat uncertain mesh work and build'}</Btn><Btn small kind="quiet" onClick={()=>setRepeatAsk(null)}>Not now</Btn></div></div>}</div></>}
 </div>}
 {step==='assets'&&<div style={{display:'flex',flexDirection:'column',gap:18}}><header><h2 style={{fontSize:25,fontWeight:550,margin:0}}>Build and inspect</h2></header>{building?<><Panel title="Build progress" aside={<span style={{fontSize:12,color:C.text3}}>{building.phase.replaceAll('_',' ')}</span>}><StageRail run={bstate}/>{mode==='workspace'&&<BuildRobots building={building}/>}{building.error&&<div style={{marginTop:12}}><ErrorLine msg={building.error}/><InvestigateRun generic label="Investigate this build" run={Object.values(building.objects||{}).map(o=>detail.pipeline_run?.find(r=>r.id===o.run_id)).find(r=>r&&r.status==='failed')||null}/>{repeatInfo&&!buildActive&&<div style={{marginTop:10,display:'flex',gap:10,alignItems:'center',flexWrap:'wrap',fontSize:12,color:C.text2,lineHeight:1.5}}><span style={{flex:'1 1 260px'}}>Mesh outcome unknown{repeatNames.length?' for '+repeatNames.join(', '):''}. Inputs, crops and any verified checkpoints are saved; building again needs your confirmation because it may repeat GPU work.</span><Btn processing={gateHold} small onClick={()=>{setRepeatAsk(repeatInfo);onStep('inputs')}}>Review and build again</Btn></div>}</div>}{building.action&&<p style={{fontSize:12.5,color:C.text2,lineHeight:1.6}}>{building.action}</p>}{buildActive&&<Btn small style={{marginTop:14}} onClick={()=>action('cancel',async()=>{await api.req(`/api/projects/${pid}/workflow/build/cancel`,{method:'POST',json:{}});await load()})}>Cancel build</Btn>}</Panel>{Object.entries(building.objects||{}).map(([id,o])=>{const av=detail.asset_version?.find(a=>a.id===o.asset_version_id),files=av?filesOf(detail,av.manifest_id,'preview/'):[],r=detail.pipeline_run?.find(x=>x.id===o.run_id);return <Panel key={id} title={o.name} aside={<Status s={av?.status||r?.status||'queued'}/>}><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'minmax(0,1.4fr) minmax(200px,1fr)',gap:18}}><div style={{minWidth:0}}>{files.length?<Viewer api={api} pid={pid} scene={pathMap(files,'preview/')} traj={null} height={narrow?300:360} compact/>:<div style={{height:160,display:'grid',placeItems:'center',color:C.text3,fontSize:12,background:C.sunken,borderRadius:10}}>Mesh preview appears after asset generation</div>}</div><div>{av&&Object.entries(av.measurements_json||{}).map(([k,v])=><Kv key={k} k={k.replaceAll('_',' ')} v={`${v.value} · ${v.source}`} mono={false}/>)}{o.reused&&o.reused!=='none'&&<p style={{fontSize:12,color:C.text3}}>Reused {o.reused}; not regenerated.</p>}{r?.error&&<ErrorLine msg={r.error}/>}{r&&<RunTiming run={r}/>}{r&&r.status==='failed'&&<InvestigateRun run={r}/>}</div></div></Panel>})}<div style={{display:'flex',gap:8,flexWrap:'wrap'}}><Btn onClick={()=>onStep('inputs')}>Edit inputs</Btn><Btn kind="primary" icon="chevron" disabled={!building.preview||buildActive} onClick={()=>onStep(mode==='workspace'?'scene':'export')}>{mode==='workspace'?'Inspect scene layout':'Review and export'}</Btn></div></>:<Empty title="No build yet" sub="Review inputs, then build." action={<Btn onClick={()=>onStep('inputs')}>Review inputs</Btn>}/>}</div>}
 {step==='scene'&&<WorkflowSceneReview {...{api,pid,detail,system,narrow,draft,change,building,layout,previewScene,previewTraj,rebuildScene,busy,dirty,onStep,buildActive,readonly}} sourceFileId={(data.uploads||[]).find(u=>u.id===draft.primary_upload_id)?.source_file_id}/>}
 {step==='export'&&<div style={{display:'flex',flexDirection:'column',gap:18}}><header><h2 style={{fontSize:25,fontWeight:550,margin:0}}>Approve and export</h2></header>{building?.preview?<><div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1.5fr 1fr',gap:18}}><div style={{minWidth:0}}>{previewScene&&<Viewer api={api} pid={pid} scene={previewScene} traj={previewTraj} height={350}/>}</div><Panel title="Pinned build"><Kv k="Mode" v={mode==='workspace'?'Workspace':'Single object'} mono={false}/><Kv k="Objects" v={Object.values(building.objects||{}).map(o=>o.name).join(', ')} mono={false}/><Kv k={mode==='workspace'&&buildRobotList(building).length>1?'Robots':'Robot'} v={mode==='workspace'?(buildRobotList(building).map(r=>(buildRobotList(building).length>1&&(r.label||r.name)?(r.label||r.name)+': ':'')+(r.library_id||'?')).join(', ')||'Unknown'):'Reference scene only'} mono={false}/><Kv k="Layout" v={mode==='workspace'?layout.status||'Not recovered':'Not applicable'} mono={false}/><Kv k="Physics check" v={building.preview.result||'Inspect preview'} mono={false}/><p style={{fontSize:11.5,color:C.text3,lineHeight:1.6}}>Observations include simulator state; map them to real sensors before hardware use. Passing checks is not physical accuracy.</p></Panel></div>
 {building.phase!=='exported'&&<Panel title="Approval"><label style={{display:'flex',gap:10,fontSize:13,lineHeight:1.6,color:C.text2}}><input type="checkbox" checked={finalAck} onChange={e=>setFinalAck(e.target.checked)} style={{marginTop:4,accentColor:C.accent}}/>I checked object sizes, robot models and layout, and approve this preview for export.</label>{mode==='workspace'&&layout.provisional!==false&&<label style={{display:'flex',gap:10,fontSize:12.5,lineHeight:1.6,color:C.warn,marginTop:14}}><input type="checkbox" checked={fallbackAck} onChange={e=>setFallbackAck(e.target.checked)} style={{marginTop:4,accentColor:C.accent}}/>Layout not fully recovered. I accept this provisional arrangement; it is not a reconstruction of the real cell.</label>}<div style={{display:'flex',gap:8,marginTop:16,flexWrap:'wrap'}}><Btn onClick={()=>onStep(mode==='workspace'?'scene':'assets')}>Back to inspect</Btn><Btn processing={gateHold} kind="primary" icon="box" disabled={!finalAck||dirty||!!busy||building.phase!=='awaiting_approval'||(mode==='workspace'&&layout.provisional!==false&&!fallbackAck)} onClick={approve}>Approve this build & generate export</Btn></div></Panel>}
 {building.phase==='exporting'&&<Panel title="Validating export"><StageRail run={bstate}/></Panel>}{building.error&&<ErrorLine msg={building.error}/>}
 {building.phase==='exported'&&latestExport&&<Panel title="Export ready" aside={<Status s="ready"/>}><Kv k="Environment" v={latestExport.env_id||exportData?.env_id}/><Kv k="Version" v={latestExport.package_version||exportData?.package_version}/><p style={{fontSize:12.5,color:C.text2,lineHeight:1.6}}>Passed build and install checks.</p><Btn kind="primary" icon="download" onClick={()=>saveFile(api,pid,latestExport.archive_file_id||exportData.archive_file_id,exportData?.archive_name||'workcell-environment.zip')}>Download environment</Btn>{mode==='object'&&Object.values(building.objects||{}).map(o=>{const a=detail.asset_version?.find(x=>x.id===o.asset_version_id);return a?<Btn key={a.id} style={{marginLeft:8}} icon="download" onClick={()=>saveZip(api,pid,a.manifest_id,'asset/',a.object_name+'-asset-v'+a.version)}>Download standalone asset</Btn>:null})}<Btn style={{marginLeft:8}} onClick={()=>goto('library')}>Files & history</Btn></Panel>}
 </>:<Empty title="Build and inspect first" sub="Export approval is available after the generated preview is ready." action={<Btn onClick={()=>onStep('assets')}>Open build</Btn>}/>}
 {(detail.gym_export||[]).length>0&&<details style={{fontSize:13,color:C.text2}}><summary style={{cursor:'pointer'}}>Previously generated packages</summary><div style={{marginTop:14}}><LegacyExportScreen api={api} pid={pid} detail={detail} system={system} narrow={narrow} refresh={refresh} goto={goto}/></div></details>}
 </div>}
 </div>;
}

function WorkflowSceneReview({api,pid,detail,system,narrow,draft,change,building,layout,previewScene,previewTraj,rebuildScene,busy,dirty,onStep,buildActive,sourceFileId,readonly}){
 const gateHold=true;   // Btn reads the maintenance hold from RunDataCtx (same semantics as the studio's gate)
 const align=draft.layout?.alignment||{},robotsAl=align.robots||{};
 const inc=(draft.robot?.arms||[]).filter(a=>a.decision==='use'&&!a._new&&a.name);
 // No included arm (no robot detected → default model): keep the historical single key so earlier anchors still resolve.
 const poseArms=inc.length?inc:[{name:align.primary_robot||'robot_1',label:'Simulation robot',pseudo:true}];
 const refName=poseArms.some(a=>a.name===align.primary_robot)?align.primary_robot:poseArms[0].name;
 const objs=(draft.objects||[]).filter(o=>o.include),places=align.object_overrides||{};
 const blank=v=>v===''||v==null;
 // Cleared entries are removed rather than saved half-empty (the server rejects partial table-frame coordinates). Tag/cluster anchors are never overwritten here.
 const editPose=(name,k,v)=>{const cur=robotsAl[name]||{};if(cur.method&&cur.method!=='table_measured')return;const next={...cur,method:'table_measured',[k]:v},robots={...robotsAl};if(['x_m','y_m','yaw_deg','z_m'].every(f=>blank(next[f])))delete robots[name];else robots[name]=next;change({...draft,layout:{...draft.layout,alignment:{...align,primary_robot:refName,robots}}})};
 const setRef=name=>change({...draft,layout:{...draft.layout,alignment:{...align,primary_robot:name}}});
 const editPlacement=(name,k,v)=>{const next={...(places[name]||{}),[k]:v},ov={...places};if(['x_m','y_m','yaw_deg'].every(f=>blank(next[f])))delete ov[name];else ov[name]=next;change({...draft,layout:{...draft.layout,alignment:{...align,object_overrides:ov}}})};
 const isPartial=e=>!!e&&e.method==='table_measured'&&(blank(e.x_m)!==blank(e.y_m)||(blank(e.x_m)&&blank(e.y_m)&&!blank(e.yaw_deg)));
 const partialArms=poseArms.filter(a=>isPartial(robotsAl[a.name])).map(a=>a.label||a.name);
 const partialObjs=Object.entries(places).filter(([,v])=>blank(v.x_m)||blank(v.y_m)).map(([n])=>n);
 const refE=robotsAl[refName]||{},hasAnchor=refE.method==='table_measured'&&!blank(refE.x_m)&&!blank(refE.y_m);
 const alignBlocked=partialArms.length>0||partialObjs.length>0;
 if(!building)return <Empty title="Generate the assets first" sub="Scene alignment follows the reviewed input and mesh build steps." action={<Btn onClick={()=>onStep('inputs')}>Review inputs</Btn>}/>;
 const videoBased=layout.provisional===false&&layout.source==='workflow_layout';const recovered=videoBased&&layout.status==='video_complete';
 return <div style={{display:'flex',flexDirection:'column',gap:18}}><header><h2 style={{fontSize:25,fontWeight:550,margin:0}}>Match the real layout.</h2></header>
 <div style={{padding:'14px 16px',border:'1px solid '+(recovered?C.line:C.accentLine),borderRadius:10,background:recovered?C.panel:C.accentSoft}}><strong style={{fontSize:13,color:recovered?C.ok:C.warn}}>{recovered?'Video-derived positions available':videoBased?'Video-informed layout: review required':layout.source==='manual'?'Manually supplied layout':buildActive?'Recovering / composing the scene':'Provisional layout: alignment needs review'}</strong><p style={{fontSize:12.5,lineHeight:1.65,color:C.text2,margin:'6px 0 0'}}>{layout.label||layout.reason||'Check the current layout provenance before approval.'}</p>{layout.provisional&&<p style={{fontSize:12,color:C.text3,lineHeight:1.6,margin:'6px 0 0'}}>Default row: a preview arrangement, not recovered positions.</p>}{videoBased&&!recovered&&<p style={{fontSize:12,color:C.text3,lineHeight:1.6,margin:'6px 0 0'}}>Some positions, scale or orientation are estimated. Verify before approving.</p>}</div>
 {previewScene?<div style={{display:'grid',gridTemplateColumns:narrow||!sourceFileId?'1fr':'minmax(0,1fr) 250px',gap:14}}><div style={{minWidth:0}}><Viewer api={api} pid={pid} scene={previewScene} traj={previewTraj} height={narrow?360:490}/></div>{sourceFileId&&<WorkflowSourceVideo api={api} pid={pid} fid={sourceFileId}/>}</div>:<Panel title="Scene preparation"><StageRail run={{kind:'workflow',stages_json:building.steps||[]}}/>{building.error&&<ErrorLine msg={building.error}/>}<p style={{fontSize:12.5,color:C.text3,lineHeight:1.6}}>Appears once assets and the robot are ready. Earlier versions stay in Files.</p></Panel>}
 <div style={{display:'grid',gridTemplateColumns:narrow?'1fr':'1fr 1fr',gap:18}}><Panel title={poseArms.length>1?'Robot bases':'Robot base'}><p style={{fontSize:12,color:C.text2,lineHeight:1.65,marginTop:0}}>Measured from the tabletop centre: x along length, y along width.</p>{poseArms.length>1&&<div style={{marginBottom:10}}><Label>Reference arm</Label><select aria-label="Reference arm" value={refName} onChange={e=>setRef(e.target.value)} disabled={readonly} style={inputCss()}>{poseArms.map(a=><option key={a.name} value={a.name}>{a.label||a.name}</option>)}</select><p style={{fontSize:11.5,color:C.text3,lineHeight:1.5,margin:'4px 0 0'}}>Object corrections are placed relative to this arm.</p></div>}{poseArms.map(a=>{const e=robotsAl[a.name]||{},other=e.method&&e.method!=='table_measured',measured=e.method==='table_measured'&&!blank(e.x_m)&&!blank(e.y_m);const tag=other?(e.method==='tag'?'Tag anchor':'Cluster anchor'):measured?'Measured':(layout.provisional===false&&a.name===refName?'Video estimate · review':'Not measured · provisional');return <div key={a.name} style={{padding:'10px 0',borderTop:'1px solid '+C.line}}><div style={{display:'flex',gap:8,alignItems:'baseline',flexWrap:'wrap'}}><strong style={{fontSize:12.5}}>{a.label||a.name}</strong><span style={{fontSize:11.5,color:measured||other?C.text3:C.warn}}>{tag}</span></div>{other?<p style={{fontSize:11.5,color:C.text3,margin:'6px 0 0'}}>Kept as saved.</p>:<div style={{display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:8,marginTop:8}}>{[['x_m','x · m'],['y_m','y · m'],['yaw_deg','yaw · °']].map(([k,l])=><div key={k}><Label>{l}</Label><input aria-label={(a.label||a.name)+' base '+l} type="number" step="any" value={e[k]??''} disabled={readonly} onChange={ev=>editPose(a.name,k,ev.target.value===''?'':Number(ev.target.value))} style={inputCss(false,true)}/></div>)}</div>}</div>})}<div style={{marginTop:14}}><Label>Table frame orientation</Label><select aria-label="Table orientation" disabled={readonly} value={Object.prototype.hasOwnProperty.call(align,'table_frame_flip')?(align.table_frame_flip?'flip':'keep'):''} onChange={e=>change({...draft,layout:{...draft.layout,alignment:{...align,table_frame_flip:e.target.value==='flip'}}})} style={inputCss()}><option value="" disabled>Confirm against your capture and measurements</option><option value="keep">Keep reconstructed orientation</option><option value="flip">Rotate table frame 180°</option></select></div></Panel>
 <Panel title="Position corrections"><p style={{fontSize:12,color:C.text2,lineHeight:1.65,marginTop:0}}>Optional. Table frame, metres: origin at tabletop centre, x along length, y along width. Enter both x and y; blank keeps the current value, never marked measured.</p>{objs.map(o=>{const v=places[o.name]||{};return <div key={o.id} style={{padding:'10px 0',borderTop:'1px solid '+C.line}}><strong style={{fontSize:12.5}}>{o.label||o.name}</strong><div style={{display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',gap:8,marginTop:8}}>{[['x_m','x · m'],['y_m','y · m'],['yaw_deg','yaw · °']].map(([k,l])=><div key={k}><Label>{l}</Label><input aria-label={o.name+' '+l} type="number" step="any" value={v[k]??''} disabled={readonly} onChange={e=>editPlacement(o.name,k,e.target.value===''?'':Number(e.target.value))} style={inputCss(false,true)}/></div>)}</div></div>})}</Panel></div>
 <div style={{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap'}}>{(alignBlocked||(Object.keys(places).length>0&&!hasAnchor))&&<div role="status" style={{flexBasis:'100%',fontSize:12,color:C.warn,lineHeight:1.6}}>{partialArms.length?'Enter both base x and y, or clear them, for: '+partialArms.join(', ')+'. ':''}{partialObjs.length?'Enter both x and y for: '+partialObjs.join(', ')+'. ':''}{Object.keys(places).length>0&&!hasAnchor?'Corrections need a measured base for '+((poseArms.find(a=>a.name===refName)||{}).label||refName)+'.':''}</div>}<Btn processing={gateHold} disabled={!!busy||buildActive||alignBlocked||readonly||!Object.values(building.objects||{}).every(o=>o.asset_version_id)} icon="reset" onClick={rebuildScene}>{busy==='compose'?'Updating scene…':'Apply alignment & re-compose'}</Btn><Btn onClick={()=>onStep('inputs')}>Correct dimensions</Btn><span style={{flex:1}}/><Btn kind="primary" icon="chevron" disabled={dirty||!building.preview||buildActive} onClick={()=>onStep('export')}>Review export approval</Btn></div>
 <p style={{fontSize:11.5,color:C.text3,lineHeight:1.65}}>Re-composing reuses meshes. Object yaw may be unresolved; compare with the real setup.</p></div>;
}

/* ── Multi-arm helpers. No arm-count cap anywhere: every row is rendered, saved and shown. ─────────────────────────── */
// Only fields the user actually changed leave the browser: an untouched analysis proposal stays an analysis proposal.
// `_new` / `_libDirty` are ephemeral UI markers and are never sent.
function armsPayload(arms){
  return (arms||[]).map(a=>a._new
    ?{new:true,label:String(a.label||'Robot arm').slice(0,60),library_id:a.library_id||null,decision:a.decision==='exclude'?'exclude':'use'}
    :{id:a.id,decision:a.decision,...(a._libDirty?{library_id:a.library_id||null}:{})});
}
const ARM_MODEL_ORIGIN={user:'chosen by you',inherited:'default, chosen by you',analysis:'suggested by analysis',assumed:'assumed default',default:'default'};
const armExplicit=a=>!!(a._libDirty||a.library_origin==='user'||(a.origin==='user'&&a.library_origin!=='analysis'));
// Resolution order: explicit per-arm user choice → user-chosen global default → analysis hint on the arm → global default.
function armAuto(arm,robot){
  const r=robot||{};
  if(r.selected_origin==='user'&&r.selected_library_id)return {id:r.selected_library_id,origin:'inherited'};
  if(!armExplicit(arm)&&arm.library_id)return {id:arm.library_id,origin:'analysis'};
  if(r.selected_library_id)return {id:r.selected_library_id,origin:r.selected_origin==='assumed'?'assumed':r.selected_origin==='analysis'?'analysis':'default'};
  return null;
}
function armModel(arm,robot){return armExplicit(arm)&&arm.library_id?{id:arm.library_id,origin:'user'}:armAuto(arm,robot);}
function ArmRow({arm,index,robot,catalog,readonly,onChange,onRemove}){
  const name=id=>(catalog.find(c=>c.id===id)||{}).name||id;
  const eff=armModel(arm,robot),auto=armAuto(arm,robot),explicit=armExplicit(arm)&&arm.library_id;
  const src=arm.origin==='analysis'?(arm.status==='unknown'?'Detected · model unknown':'Detected in video'):'Added by you';
  const cands=arm.candidates||[];
  return <div style={{padding:'12px 0',borderTop:'1px solid '+C.line}}>
    <div style={{display:'flex',gap:10,alignItems:'flex-end',flexWrap:'wrap'}}>
      <div style={{flex:'1 1 160px',minWidth:0}}>
        {arm._new?<><Label>Name</Label><input aria-label={'Arm '+(index+1)+' name'} value={arm.label||''} maxLength={60} disabled={readonly} onChange={e=>onChange({label:e.target.value})} style={inputCss()}/></>:<strong style={{fontSize:13,overflowWrap:'anywhere'}}>{arm.label||'Robot arm '+(index+1)}</strong>}
        <div style={{fontSize:11.5,color:C.text3,marginTop:4,lineHeight:1.5}}>{arm._new?'New · saved with inputs':src}{eff?' · '+name(eff.id)+' ('+ARM_MODEL_ORIGIN[eff.origin]+')':' · no model'}</div>
      </div>
      <div style={{flex:'1 1 170px',minWidth:0}}><Label>Model</Label><select aria-label={'Arm '+(index+1)+' simulation model'} value={explicit?arm.library_id:''} disabled={readonly} onChange={e=>onChange(arm._new?{library_id:e.target.value||null}:{library_id:e.target.value||null,_libDirty:true})} style={inputCss()}>
        <option value="">{auto?'Auto · '+name(auto.id):'Auto · choose a default model'}</option>{catalog.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></div>
      <div style={{flex:'0 1 160px',minWidth:0}}><Label>In scene</Label><select aria-label={'Include arm '+(index+1)} value={arm.decision||'undecided'} disabled={readonly} onChange={e=>onChange({decision:e.target.value})} style={inputCss()}>
        {(arm.decision||'undecided')==='undecided'&&<option value="undecided">Review</option>}<option value="use">Include</option><option value="exclude">Exclude</option></select></div>
      {onRemove&&<Btn small kind="quiet" icon="x" ariaLabel={'Remove unsaved arm '+(index+1)} disabled={readonly} onClick={onRemove}>Remove</Btn>}
    </div>
    {!arm._new&&<details style={{marginTop:8,fontSize:11.5,color:C.text3}}><summary style={{cursor:'pointer'}}>Details</summary><div style={{lineHeight:1.6,marginTop:6}}>
      <div>Candidates: {cands.length?cands.map(c=>(c.name||c.library_id)+' ('+c.confidence+')').join(', '):'none; exact model uncertain'}</div>
      {arm.unlisted_guess&&<div>Unlisted guess: {arm.unlisted_guess}</div>}
      {arm.origin==='analysis'&&arm.library_origin!=='user'&&arm.library_id&&<div>Analysis hint: {name(arm.library_id)} (not a confirmed model)</div>}
      {(arm.frame_refs||[]).length>0&&<div>Seen in {(arm.frame_refs||[]).length} sampled frame(s)</div>}
      {arm.exclusion_reason&&<div>Excluded: {arm.exclusion_reason}</div>}
      <div style={{fontFamily:MONO}}>{arm.name||arm.id}</div>
    </div></details>}
  </div>;
}
// Build-side robot instances: prefer build.robots / build.instances (array or keyed object); fall back to the single build.robot.
function buildRobotList(b){
  if(!b)return [];
  for(const x of [b.robots,b.instances]){if(!x)continue;const l=Array.isArray(x)?x:Object.entries(x).map(([k,v])=>({name:k,...(v||{})}));if(l.length)return l;}
  return b.robot?[b.robot]:[];
}
function BuildRobots({building}){
  const list=buildRobotList(building);
  if(!list.length)return null;
  const st=r=>r.error?'failed':r.confirmed?'ready':r.status?String(r.status).replaceAll('_',' '):r.reused&&r.reused!=='none'?'reusing '+r.reused:'importing';
  return <div style={{marginTop:12}}>{list.map((r,i)=><Kv key={(r.name||r.arm_name||'')+i} k={r.label||r.name||r.arm_name||(list.length>1?'Robot '+(i+1):'Robot')} v={(r.library_id||'model pending')+' · '+st(r)} mono={false}/>)}{list.some(r=>r.error)&&list.filter(r=>r.error).map((r,i)=><ErrorLine key={i} msg={(r.label||r.name||'Robot')+': '+r.error}/>)}</div>;
}


/* ── Product specs from the web: Apify evidence + Claude match (cited; values apply only to unsaved edits) ── */
const IWM_STAGE={queued:'Queued',web:'Searching the web',claude:'Matching',persist:'Saving'};
const IWM_FIELDS=[['length_cm','Length','cm'],['width_cm','Width','cm'],['height_cm','Height','cm'],['mass_g','Mass','g'],['material','Material','']];
const IWM_ERR={apify_not_connected:'Apify isn’t connected.',apify_actor_missing:'The evidence Actor isn’t set up yet.',analysis_required:'Run the video analysis first.',no_objects:'Include an object first.',billing:'Only the workspace owner’s projects can run this.',quota_exceeded:'Daily lookup limit reached.'};
const iwmHost=u=>{try{return new URL(u).hostname.replace(/^www\./,'')}catch(e){return ''}};
function ItemWebMatch({api,pid,data,draft,change,readonly,load}){
  const [busy,setBusy]=React.useState(false),[err,setErr]=React.useState(''),[applied,setApplied]=React.useState({});
  const info=data?.identification||{},job=info.job,active=['queued','running'].includes(job?.status);
  const byId=Object.fromEntries((data?.workflow?.objects||[]).map(o=>[o.id,o.identification]));
  const objs=(draft?.objects||[]).filter(o=>o.include!==false&&byId[o.id]);
  const run=async()=>{setBusy(true);setErr('');try{await api.req(`/api/projects/${pid}/workflow/identify`,{json:{max_results:3}});await load()}catch(e){setErr(IWM_ERR[e.code]||e.message)}finally{setBusy(false)}};
  const apply=(o,idn)=>{const f={...(o.fields||{})};for(const [k,s] of Object.entries(idn.specs||{})){const dom=(s.domain||iwmHost(s.source_url)).replace(/^www\./,'');f[k]={...(f[k]||{}),value:s.value,source:'supplied',note:`From ${dom}, matched by Claude using Apify web evidence. Check before building.`}}change({...draft,objects:draft.objects.map(x=>x.id===o.id?{...x,fields:f}:x)});setApplied(a=>({...a,[o.id]:idn.id}))};
  const hint=info.connected===false?(info.can_connect?'Connect APIFY_API_TOKEN in Mana to enable.':null):info.connected&&!info.actor_ready?(info.can_connect?'Evidence Actor not set up yet.':null):null;
  return <div style={{padding:'12px 0',borderBottom:'1px solid '+C.line}}>
    <div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}><div style={{flex:1,minWidth:200}}><div style={{fontSize:13,fontWeight:550}}>Product specs</div><div style={{fontSize:12,color:C.text3,marginTop:3}}>{active?(IWM_STAGE[job.stage]||'Working')+'…':job?.status==='complete'?`Checked ${job.items||objs.length} object(s) · ${fmtAgo(job.finished_at)}`:'Look up published dimensions and weight.'}</div></div>
    <Btn small icon="sparkle" disabled={readonly||busy||active||!info.can_run} onClick={run}>{active?'Looking up…':job?.status==='complete'?'Look up again':'Find product specs'}</Btn></div>
    {hint&&<div style={{fontSize:11.5,color:C.text3,marginTop:6}}>{hint}</div>}
    {(err||(['failed','interrupted'].includes(job?.status)&&job.error))&&<div style={{marginTop:8}}><ErrorLine msg={err||job.error}/></div>}
    {objs.length>0&&<div style={{display:'flex',flexDirection:'column',gap:8,marginTop:10}}>{objs.map(o=>{const idn=byId[o.id],specs=Object.entries(idn.specs||{}),hit=idn.match==='confident'||idn.match==='possible';
      return <div key={o.id} style={{fontSize:12,lineHeight:1.55}}><div style={{display:'flex',gap:8,alignItems:'baseline',flexWrap:'wrap'}}><span style={{fontWeight:550}}>{o.label||o.name}</span>
        {hit?<span style={{color:C.text2,minWidth:0}}>{idn.product_name||'Match'}{idn.brand?' · '+idn.brand:''} · <span style={{color:idn.match==='confident'?C.ok:C.warn}}>{idn.match==='confident'?'confident':'possible'}</span>{idn.domains?.length?' · '+idn.domains.join(', '):''}</span>
          :<span style={{color:C.text3}}>{idn.status==='error'?'Web lookup failed':'No confident match'}</span>}
        {idn.applicable&&specs.length>0&&<Btn small kind="quiet" disabled={readonly} onClick={()=>apply(o,idn)}>{applied[o.id]===idn.id?'Applied · save to keep':'Use these values'}</Btn>}</div>
        {(specs.length>0||idn.reasons?.length||idn.mismatches?.length||idn.dropped?.length)&&<details style={{color:C.text3,marginTop:2}}><summary style={{cursor:'pointer',fontSize:11.5}}>Details</summary>
          {specs.map(([k,s])=>{const l=IWM_FIELDS.find(x=>x[0]===k);return <div key={k} style={{marginTop:4}}><span style={{color:C.text2,fontFamily:MONO}}>{(l?l[1]:k)+' '+s.value+(l&&l[2]?' '+l[2]:'')}</span> · <a href={s.source_url} target="_blank" rel="noopener noreferrer" style={{color:C.text2}}>{s.domain}</a><div style={{fontStyle:'italic'}}>“{s.quote}”</div></div>})}
          {(idn.mismatches||[]).map((m,i)=><div key={'m'+i} style={{color:C.warn,marginTop:4}}>{m}</div>)}
          {(idn.reasons||[]).map((m,i)=><div key={'r'+i} style={{marginTop:4}}>{m}</div>)}
          {idn.dropped?.length>0&&<div style={{marginTop:4}}>{idn.dropped.length} unsupported value(s) left out.</div>}</details>}</div>})}</div>}
  </div>;
}


function LegacyObjectFlow({api,pid,detail,system,narrow,refresh,goto,refreshSystem,canSuggest,step,onStep,autoResume,showProgress=true}){
 const once=React.useRef(false);
 const stages=[['capture','Capture'],['review','Review'],['simulation','Simulation'],['export','Export']];
 const mapped={inputs:'capture',assets:'review',scene:'simulation'}[step]||step;
 const active=stages.some(s=>s[0]===mapped)?mapped:'capture';
 React.useEffect(()=>{if(autoResume&&!once.current){once.current=true;const r=(detail.pipeline_run||[]).find(r=>['asset','rebuild','scene_capture'].includes(r.kind));const next=r&&['queued','running','needs_review','failed'].includes(r.status)?'review':(detail.gym_export||[]).some(e=>e.status==='ready')?'export':(detail.scene_version||[]).length?'simulation':r?'review':'capture';onStep(next,true)}},[autoResume]);
 const go=t=>stages.some(s=>s[0]===t)?onStep(t):goto(t);
 const props={api,pid,detail,system,narrow,refresh,goto:go,refreshSystem};
 return <div className="w-full">{showProgress&&<nav aria-label="Single-object pipeline" style={{display:'flex',gap:0,borderBottom:'1px solid '+C.line,marginBottom:24,overflowX:'auto'}}>{stages.map(([k,l],i)=><button key={k} onClick={()=>onStep(k)} aria-current={active===k?'step':undefined} style={{flex:'1 0 115px',padding:'13px 10px',border:0,borderBottom:'2px solid '+(active===k?C.accent:'transparent'),background:'none',color:active===k?C.text:C.text3,fontFamily:FONT,fontSize:12.5,cursor:'pointer'}}><span style={{fontFamily:MONO,fontSize:10,marginRight:8}}>{String(i+1).padStart(2,'0')}</span>{l}</button>)}</nav>}
 {active==='capture'?<CaptureWizard {...props} singleObjectOnly canSuggest={canSuggest} onStarted={()=>{refresh();onStep('review')}}/>:active==='review'?<ReviewScreen {...props}/>:active==='simulation'?<LegacySimulationScreen {...props}/>:<LegacyExportScreen {...props}/>}</div>;
}


/* ── RealiSIM Assistant: Setup (existing Claude copilot) · Troubleshoot (Factory Droid jobs + Elastic retrieval) · History.
   Canonical cases/jobs/resolutions live in SpacetimeDB via backend/support.py; Elastic is a derived search index.
   Contract: /workspace/photo2grasp/support-contract.json (routes) + backend/support.py (response shapes). ── */
const ASSIST_MODES = [['setup', 'Setup'], ['troubleshoot', 'Troubleshoot'], ['history', 'History']];
const InvestigateCtx = React.createContext(null);
const JOB_OPEN = ['waiting_for_connection', 'queued', 'running'];
const JOB_LABEL = { waiting_for_connection: ['Waiting for Factory connection', C.warn], queued: ['Queued', C.text2], running: ['Running', C.accent], complete: ['Complete', C.ok], failed: ['Failed', C.err], interrupted: ['Interrupted', C.err] };
const OUTCOME_LABEL = { resolved: 'Resolved', workaround: 'Workaround', not_resolved: 'Not resolved' };
const KIND_LABEL = { pipeline_error: 'Failed run', pipeline_run: 'Run', asset_version: 'Asset version', scene_version: 'Scene version', simulation_run: 'Simulation', gym_export: 'Export', workflow_error: 'Workflow error', support_case: 'Support case', resolution: 'Resolution', help_doc: 'Help' };
const FAIL_TEXT = { missing_key: 'The Factory worker has no API key in its run context.', auth: 'Factory rejected the credentials.', timeout: 'The investigation hit its time limit.', sdk_error: 'The Factory Droid SDK reported an error.', tool_error: 'A support tool failed during the investigation.', policy: 'The investigation was stopped by a safety policy.', access_revoked: 'Project access changed during the investigation.', lease_expired: 'The worker stopped responding; not retried automatically.', other: 'The investigation failed.' };

/* Status-preserving request helper: support routes return {error, ...extra} (e.g. a 409 carries the open job). */
async function supportFetch(api, path, json) {
  if (!api.base) return { ok: false, status: 0, body: { error: 'backend not connected' } };
  try {
    const token = await FS.auth.currentUser.getIdToken();
    const r = await fetch(api.base + path, { method: json ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + token, ...(json ? { 'Content-Type': 'application/json' } : {}) }, body: json ? JSON.stringify(json) : undefined });
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body };
  } catch (e) { return { ok: false, status: 0, body: { error: e.message || 'network error' } }; }
}
// Route not registered yet: aiohttp's plain 404/405 (no JSON error) or a generic guard 'not found'. Real support errors carry a specific message.
const supportUnavailable = (res) => [404, 405, 501].includes(res.status) && (!res.body || !res.body.error || /^(not found|404: not found|method not allowed)$/i.test(String(res.body.error).trim()));

function useSupport(api, pid) {
  const [st, setSt] = React.useState({ status: 'loading', data: null, error: '' });
  const load = React.useCallback(async () => {
    const res = await supportFetch(api, `/api/projects/${pid}/support`);
    if (res.ok) setSt({ status: 'ready', data: res.body, error: '' });
    else if (supportUnavailable(res)) setSt({ status: 'unavailable', data: null, error: '' });
    else setSt((s) => ({ status: s.data ? 'stale' : 'error', data: s.data, error: res.body.error || 'HTTP ' + res.status }));
  }, [api, pid]);
  const open = !!(st.data && (st.data.jobs || []).some((j) => JOB_OPEN.includes(j.status)));
  React.useEffect(() => { if (!api.base || !pid) return; load(); const t = setInterval(load, open ? 6000 : 30000); return () => clearInterval(t); }, [api.base, pid, open, load]);
  return { ...st, reload: load };
}

function InvestigateRun({ run, label, generic }) {
  const go = React.useContext(InvestigateCtx);
  if (!go) return null;
  const runId = run && run.id && /^run_/.test(run.id) ? run.id : null;
  if (!runId && !generic) return null;
  if (runId && run.status !== 'failed' && !generic) return null;
  if (runId && !generic && typeof runDisplayStatus === 'function' && ['cancelled', 'rejected'].includes(runDisplayStatus(run))) return null;   // nothing to troubleshoot
  return <div style={{ marginTop: 10 }}><Btn small kind="ghost" icon="assistant" ariaLabel={runId ? 'Investigate failed run ' + runId + ' in the Assistant' : 'Open troubleshooting in the Assistant'} onClick={() => go(runId)}>{label || (runId ? 'Investigate this run' : 'Ask the assistant')}</Btn></div>;
}

function SnippetText({ text }) {
  const parts = String(text || '').split(/(⟦[^⟧]*⟧)/g);
  return <>{parts.map((p, i) => p.startsWith('⟦') ? <mark key={i} style={{ background: C.accentSoft, color: C.text, borderRadius: 3, padding: '0 2px' }}>{p.slice(1, -1)}</mark> : <React.Fragment key={i}>{p}</React.Fragment>)}</>;
}

function ProviderStatus({ support, isOwner, onConnect }) {
  const d = support.data, f = d && d.provider && d.provider.factory, e = d && d.provider && d.provider.elastic;
  const row = (name, color, text, sub) => (
    <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', minWidth: 0, flex: '1 1 260px' }}>
      <Dot color={color} /><div style={{ minWidth: 0 }}><span style={{ fontSize: 12, color: C.text, fontWeight: 500 }}>{name}</span><span style={{ fontSize: 12, color: C.text2 }}> · {text}</span>{sub && <div style={{ fontSize: 11, color: C.text3, marginTop: 2, fontFamily: MONO }}>{sub}</div>}</div>
    </div>
  );
  if (support.status === 'loading') return <div role="status" style={{ fontSize: 12, color: C.text3, padding: '8px 0' }}>Checking assistant services…</div>;
  if (support.status === 'unavailable') return <div role="status" style={{ fontSize: 12.5, color: C.text2, padding: '8px 0', lineHeight: 1.6 }}>Troubleshooting and history are not available on this backend yet. Setup suggestions still work.</div>;
  if (support.status === 'error') return <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}><div style={{ flex: 1, minWidth: 220 }}><ErrorLine msg={'Assistant services unavailable: ' + support.error} /></div><Btn small onClick={support.reload}>Retry</Btn></div>;
  const fs = f ? f.status : 'not_connected';
  const fColor = fs === 'ready' ? C.ok : fs === 'missing_key' || fs === 'not_connected' ? C.warn : C.err;
  const fSub = f && (f.sdk || f.cli) ? [f.sdk && (f.sdk.name || 'droid-sdk') + ' ' + (f.sdk.version || ''), f.cli && 'CLI ' + (f.cli.version || '')].filter(Boolean).join(' · ') + (f.last_seen ? ' · seen ' + fmtAgo(f.last_seen * (f.last_seen < 1e12 ? 1000 : 1)) : '') : null;
  const eSub = e && e.connected ? ['Elasticsearch ' + (e.version || ''), e.cluster ? 'cluster ' + e.cluster : null, typeof e.documents === 'number' ? e.documents.toLocaleString() + ' indexed records' : null].filter(Boolean).join(' · ') : null;
  return (
    <div style={{ display: 'flex', gap: '8px 20px', flexWrap: 'wrap', padding: '10px 0', borderTop: '1px solid ' + C.line, borderBottom: '1px solid ' + C.line }}>
      {row('Troubleshooting', fColor, fs === 'ready' ? 'ready' : isOwner ? ((f && f.note) || 'not connected') : 'unavailable', isOwner ? fSub : null)}
      {row('Search', e && e.connected ? C.ok : C.err, e && e.connected ? 'ready' : 'unavailable' + (isOwner && e && e.error ? ': ' + e.error : ''), isOwner ? eSub : null)}
      {support.status === 'stale' && <div style={{ flexBasis: '100%', fontSize: 11, color: C.text3 }}>Status refresh failed; showing the last known state.</div>}
      {isOwner && fs !== 'ready' && onConnect && <div style={{ flexBasis: '100%' }}>{onConnect}</div>}
    </div>
  );
}

/* Owner-only secure connection: value goes straight from a password field to Mana's credential store. */
function FactoryConnect({ factoryStatus }) {
  const [val, setVal] = React.useState(''), [saved, setSaved] = React.useState(null), [busy, setBusy] = React.useState(false), [msg, setMsg] = React.useState(''), [err, setErr] = React.useState(''), [confirmRemove, setConfirmRemove] = React.useState(false);
  const has = typeof apiManager !== 'undefined' && apiManager;
  React.useEffect(() => { if (!has) return; let off = false; Promise.resolve(apiManager.list()).then((l) => { if (!off) setSaved((l || []).some((k) => (typeof k === 'string' ? k : k && k.name) === 'FACTORY_API_KEY')); }).catch(() => { if (!off) setSaved(null); }); return () => { off = true; }; }, []);
  if (!has) return null;
  const save = async () => { const v = val.trim(); if (!v) return; setBusy(true); setErr(''); setMsg(''); try { await apiManager.save('FACTORY_API_KEY', v); setVal(''); setSaved(true); setMsg('Saved. Status turns ready after the worker’s next run.'); } catch (e) { setErr(e.message || 'Could not save the key'); } finally { setBusy(false); } };
  const remove = async () => { setBusy(true); setErr(''); try { await apiManager.delete('FACTORY_API_KEY'); setSaved(false); setMsg('Factory key removed.'); setConfirmRemove(false); } catch (e) { setErr(e.message || 'Could not remove the key'); } finally { setBusy(false); } };
  return (
    <form onSubmit={(e) => { e.preventDefault(); save(); }} style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '10px 12px', border: '1px solid ' + C.line, borderRadius: 10, background: C.panel }}>
      <div style={{ fontSize: 12.5, color: C.text, fontWeight: 500 }}>{saved ? 'Factory key saved' : 'Connect Factory'}{factoryStatus === 'missing_key' && saved ? <span style={{ color: C.text3, fontWeight: 400 }}> · waiting for the worker to pick it up</span> : null}</div>
      <div style={{ fontSize: 11.5, color: C.text3, lineHeight: 1.55 }}>Runs on your Factory account. Kept only in Mana’s encrypted credentials.</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input type="password" autoComplete="off" spellCheck={false} aria-label="Factory API key" placeholder={saved ? 'Paste a new key to rotate' : 'Factory API key'} value={val} onChange={(e) => setVal(e.target.value)} style={{ ...inputCss(false, true), flex: '1 1 220px', width: 'auto' }} />
        <Btn type="submit" kind="primary" disabled={busy || !val.trim()}>{busy ? 'Saving…' : saved ? 'Rotate key' : 'Save key'}</Btn>
        {saved && !confirmRemove && <Btn kind="quiet" disabled={busy} onClick={() => setConfirmRemove(true)}>Remove</Btn>}
        {saved && confirmRemove && <><Btn kind="danger" disabled={busy} onClick={remove}>Confirm remove</Btn><Btn kind="quiet" onClick={() => setConfirmRemove(false)}>Keep</Btn></>}
      </div>
      {msg && <div role="status" style={{ fontSize: 11.5, color: C.text2 }}>{msg}</div>}{err && <ErrorLine msg={err} />}
    </form>
  );
}

function CitationList({ items, onInvestigate, title, empty }) {
  if (!items || !items.length) return empty ? <div style={{ fontSize: 12, color: C.text3, padding: '6px 0' }}>{empty}</div> : null;
  return (
    <div>
      {title && <Eyebrow style={{ marginBottom: 6 }}>{title}</Eyebrow>}
      <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {items.map((c, i) => {
          const runId = c.canonical_id && c.canonical_id.startsWith('pipeline_run:') ? c.canonical_id.slice(13) : null;
          return (
            <li key={c.canonical_id + i} style={{ padding: '10px 0', borderTop: i ? '1px solid ' + C.line : 'none' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                <span style={{ fontFamily: MONO, fontSize: 10, color: C.accent }}>[{i + 1}]</span>
                <span style={{ fontSize: 10.5, color: C.text3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{KIND_LABEL[c.kind] || c.kind}</span>
                <span style={{ fontSize: 12.5, color: C.text, fontWeight: 500, flex: 1, minWidth: 160 }}>{c.title || c.canonical_id}</span>
                {c.outcome && <span style={{ fontSize: 11, color: c.outcome === 'resolved' ? C.ok : C.text2 }}>{OUTCOME_LABEL[c.outcome] || c.outcome}</span>}
                {c.match && <span style={{ fontSize: 11, color: C.text3 }}>{c.match}</span>}
              </div>
              {c.snippet && <div style={{ fontSize: 12, color: C.text2, lineHeight: 1.55, marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}><SnippetText text={c.snippet} /></div>}
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 4, fontSize: 10.5, color: C.text3, fontFamily: MONO }}>
{c.created_at ? <span title={c.canonical_id}>{fmtAgo(c.created_at)}</span> : null}
                {runId && onInvestigate && <button type="button" onClick={() => onInvestigate(runId)} style={{ all: 'unset', cursor: 'pointer', color: C.accent, fontFamily: FONT, fontSize: 11.5 }}>Investigate this run</button>}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function AnswerBlock({ answer }) {
  if (!answer) return null;
  if (typeof answer === 'string') return <div className="p2g-md" style={{ fontSize: 13, color: C.text, lineHeight: 1.65, userSelect: 'text' }}><ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown></div>;
  const steps = Array.isArray(answer.next_steps) ? answer.next_steps : [];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, userSelect: 'text' }}>
      {answer.diagnosis && <div style={{ fontSize: 13, color: C.text, lineHeight: 1.65, whiteSpace: 'pre-wrap' }}>{answer.diagnosis}</div>}
      {answer.uncertainty && <div style={{ fontSize: 12, color: answer.uncertainty === 'low' ? C.text2 : C.warn }}>Uncertainty: {answer.uncertainty}{answer.uncertainty_note ? ' · ' + answer.uncertainty_note : ''}</div>}
      {steps.length > 0 && <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: C.text2, lineHeight: 1.6 }}>{steps.map((s, i) => <li key={i}><span style={{ color: C.text }}>{s.title}</span>{s.detail ? ' · ' + s.detail : ''}{s.requires === 'reviewed_change' ? <span style={{ color: C.text3 }}> (needs a reviewed change)</span> : ''}</li>)}</ol>}
    </div>
  );
}

function JobView({ api, pid, jobId, canWrite, canResolve, onInvestigate, onChanged }) {
  const [job, setJob] = React.useState(null), [err, setErr] = React.useState(''), [resolving, setResolving] = React.useState(false);
  const load = React.useCallback(async () => {
    const res = await supportFetch(api, `/api/projects/${pid}/support/jobs/${jobId}`);
    if (res.ok) { setJob(res.body.job); setErr(''); } else setErr(res.body.error || 'HTTP ' + res.status);
  }, [api, pid, jobId]);
  const open = job && JOB_OPEN.includes(job.status);
  React.useEffect(() => { setJob(null); load(); }, [jobId]);
  React.useEffect(() => { if (!open) return; const t = setInterval(load, job.status === 'running' ? 2500 : 6000); return () => clearInterval(t); }, [open, job && job.status, load]);
  if (!job) return <div role="status" style={{ fontSize: 12.5, color: C.text3, padding: '10px 0' }}>{err ? <ErrorLine msg={err} /> : 'Loading investigation…'}</div>;
  const [label, color] = JOB_LABEL[job.status] || [job.status, C.text3];
  const res = job.result || {}, rt = res.runtime || {};
  const provider = rt.provider === 'factory' ? 'Factory Droid' : rt.provider ? rt.provider : 'Factory Droid';
  const decided = async () => { await load(); if (onChanged) onChanged(); };
  return (
    <section aria-label="Investigation" style={{ border: '1px solid ' + C.line, borderRadius: 12, background: C.panel, overflow: 'hidden' }}>
      <header style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '11px 14px', borderBottom: '1px solid ' + C.line, flexWrap: 'wrap' }}>
        <Dot color={color} pulse={job.status === 'running'} /><span style={{ fontSize: 13, fontWeight: 600, color: C.text }} aria-live="polite">{label}</span>
        <span style={{ fontSize: 11.5, color: C.text3 }}>{provider} investigation · started {fmtAgo(job.at)}{job.finished_at ? ' · finished ' + fmtAgo(job.finished_at) : ''}</span>
        <span style={{ flex: 1 }} /><span style={{ fontFamily: MONO, fontSize: 10.5, color: C.text3 }}>{job.id}</span>
      </header>
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        {job.status === 'waiting_for_connection' && <div style={{ fontSize: 12.5, color: C.warn, lineHeight: 1.6 }}>{job.waiting_reason || 'The Factory worker is not connected.'} The request is saved and starts once the worker is ready; nothing else runs in its place.</div>}
        {job.run_id && <div style={{ fontSize: 11.5, color: C.text3 }}>About run <span style={{ fontFamily: MONO }}>{job.run_id}</span></div>}
        {(job.steps || []).length > 0 && <div>
          <Eyebrow style={{ marginBottom: 6 }}>Tool steps</Eyebrow>
          <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>{job.steps.map((s, i) => (
            <li key={i} style={{ display: 'grid', gridTemplateColumns: '18px minmax(0,1fr) auto', gap: 8, padding: '5px 0', fontSize: 12, borderTop: i ? '1px solid ' + C.line : 'none' }}>
              <Icon name={s.status === 'error' || s.status === 'failed' ? 'alert' : 'check'} size={12} color={s.status === 'error' || s.status === 'failed' ? C.err : C.text3} style={{ marginTop: 3 }} />
              <span style={{ color: C.text2, minWidth: 0 }}><span style={{ fontFamily: MONO, color: C.text }}>{s.tool || 'step'}</span>{s.summary ? ' · ' + s.summary : ''}{typeof s.result_count === 'number' ? ' · ' + s.result_count + ' result' + (s.result_count === 1 ? '' : 's') : ''}</span>
              <span style={{ fontSize: 10.5, color: C.text3 }}>{s.at ? fmtAgo(s.at) : ''}</span>
            </li>))}</ol>
        </div>}
        {job.status === 'running' && !(job.steps || []).length && <div role="status" style={{ fontSize: 12, color: C.text3 }}>The worker claimed the investigation; tool steps appear as they finish.</div>}
        {res.answer && <div><Eyebrow style={{ marginBottom: 6 }}>Diagnosis</Eyebrow><AnswerBlock answer={res.answer} /><div style={{ fontSize: 11, color: C.text3, marginTop: 8, lineHeight: 1.5 }}>A diagnosis, not a guarantee. Check the cited records first.</div></div>}
        {res.citations && res.citations.length > 0 && <CitationList title="Sources" items={res.citations} onInvestigate={onInvestigate} />}
        {res.citations_dropped_since > 0 && <div style={{ fontSize: 11.5, color: C.text3 }}>{res.citations_dropped_since} cited record{res.citations_dropped_since === 1 ? ' is' : 's are'} no longer accessible and {res.citations_dropped_since === 1 ? 'is' : 'are'} hidden.</div>}
        {(res.rejected_citations || []).length > 0 && <div style={{ fontSize: 11.5, color: C.text3 }}>{res.rejected_citations.length} citation{res.rejected_citations.length === 1 ? ' was' : 's were'} rejected because the investigation's tools did not return {res.rejected_citations.length === 1 ? 'it' : 'them'}.</div>}
        {(res.proposals || []).length > 0 && <div><Eyebrow style={{ marginBottom: 6 }}>Proposed changes</Eyebrow><div style={{ fontSize: 11.5, color: C.text3, marginBottom: 6 }}>Nothing changes until you approve it.</div>{res.proposals.map((p) => <CopilotProposal key={p.id} api={api} pid={pid} p={{ ...p, changes: p.changes || [] }} onDecided={decided} canWrite={canWrite} />)}</div>}
        {(res.rejected_proposals || []).length > 0 && <div style={{ fontSize: 11.5, color: C.text3 }}>Not offered: {res.rejected_proposals.map((r) => (r.title ? r.title + ': ' : '') + r.reason).join('; ')}</div>}
        {job.error && <div><ErrorLine msg={(FAIL_TEXT[job.error.code || job.error_code] || FAIL_TEXT.other) + (job.error.message ? ' ' + job.error.message : '')} /></div>}
        {!job.error && ['failed', 'interrupted'].includes(job.status) && <ErrorLine msg={FAIL_TEXT[job.error_code] || FAIL_TEXT.other} />}
        {job.uncertain_charge && <div style={{ fontSize: 11.5, color: C.warn }}>The provider may have charged for this run. It is not retried automatically; ask again only if you want a new investigation.</div>}
        {(rt.model || rt.sdk_version || rt.cli_version) && <div style={{ fontSize: 10.5, color: C.text3, fontFamily: MONO }}>{[provider, rt.model, rt.sdk_version && 'sdk ' + rt.sdk_version, rt.cli_version && 'cli ' + rt.cli_version, Array.isArray(rt.tool_calls) ? rt.tool_calls.length + ' tool calls' : typeof rt.tool_calls === 'number' ? rt.tool_calls + ' tool calls' : null, rt.duration_ms ? Math.round(rt.duration_ms / 1000) + ' s' : null].filter(Boolean).join(' · ')}</div>}
        {canResolve && job.case_id && !open && (resolving ? <ResolveForm api={api} pid={pid} caseId={job.case_id} jobId={job.id} onDone={() => { setResolving(false); if (onChanged) onChanged(); }} onCancel={() => setResolving(false)} /> : <div><Btn small onClick={() => setResolving(true)}>Record the outcome</Btn></div>)}
      </div>
    </section>
  );
}

function ResolveForm({ api, pid, caseId, jobId, onDone, onCancel }) {
  const key = React.useRef('res-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now()));
  const [outcome, setOutcome] = React.useState('resolved'), [summary, setSummary] = React.useState(''), [steps, setSteps] = React.useState(''), [busy, setBusy] = React.useState(false), [err, setErr] = React.useState('');
  const submit = async () => {
    if (!summary.trim()) { setErr('Describe what happened in a sentence.'); return; }
    setBusy(true); setErr('');
    const res = await supportFetch(api, `/api/projects/${pid}/support/cases/${caseId}/resolve`, { outcome, summary: summary.trim(), fix_steps: steps.split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 12), job_id: jobId || undefined, idempotency_key: key.current });
    setBusy(false);
    if (res.ok) onDone(res.body); else setErr(res.body.error || 'HTTP ' + res.status);
  };
  return (
    <div style={{ borderTop: '1px solid ' + C.line, paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'flex', gap: 14, flexWrap: 'wrap' }}><legend style={{ fontSize: 11.5, color: C.text2, marginBottom: 6 }}>Outcome</legend>
        {Object.entries(OUTCOME_LABEL).map(([k, l]) => <label key={k} style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12.5, color: C.text2, cursor: 'pointer', minHeight: 28 }}><input type="radio" name={'outcome-' + caseId} checked={outcome === k} onChange={() => setOutcome(k)} style={{ accentColor: C.accent }} />{l}</label>)}
      </fieldset>
      <label style={{ fontSize: 11.5, color: C.text2 }}>What happened<textarea value={summary} maxLength={2000} rows={2} onChange={(e) => setSummary(e.target.value)} style={{ ...inputCss(), height: 'auto', padding: 8, marginTop: 5, resize: 'vertical', lineHeight: 1.5 }} /></label>
      <label style={{ fontSize: 11.5, color: C.text2 }}>Fix steps <span style={{ color: C.text3 }}>(optional, one per line)</span><textarea value={steps} rows={2} onChange={(e) => setSteps(e.target.value)} style={{ ...inputCss(), height: 'auto', padding: 8, marginTop: 5, resize: 'vertical', lineHeight: 1.5 }} /></label>
      <div style={{ fontSize: 11, color: C.text3 }}>Saved to project history for similar failures. Doesn’t change project data.</div>
      {err && <ErrorLine msg={err} />}
      <div style={{ display: 'flex', gap: 8 }}><Btn kind="primary" disabled={busy} onClick={submit}>{busy ? 'Saving…' : 'Save outcome'}</Btn><Btn kind="quiet" onClick={onCancel}>Cancel</Btn></div>
    </div>
  );
}

function defaultQuestion(run) {
  if (!run) return 'What is blocking this project right now, and what should I check first?';
  return `Why did the ${String(run.kind || 'pipeline').replaceAll('_', ' ')} run fail${run.stage ? ' at ' + run.stage : ''}, and what should I change before retrying?`;
}

function TroubleshootPanel({ api, pid, detail, support, helpRun, onRun, onInvestigate, narrow }) {
  const runs = (detail && detail.pipeline_run) || [];
  const run = helpRun ? runs.find((r) => r.id === helpRun) : null;
  const failed = runs.filter((r) => r.status === 'failed').sort((a, b) => (b.created_at || 0) - (a.created_at || 0)).slice(0, 5);
  const d = support.data, caps = (d && d.capabilities) || {}, quotas = d && d.quotas;
  const [q, setQ] = React.useState(() => defaultQuestion(run)), [edited, setEdited] = React.useState(false);
  const [busy, setBusy] = React.useState(false), [err, setErr] = React.useState(''), [jobId, setJobId] = React.useState(null);
  const [related, setRelated] = React.useState(null), [relErr, setRelErr] = React.useState('');
  const key = React.useRef(null);
  React.useEffect(() => { if (!edited) setQ(defaultQuestion(run)); key.current = null; }, [helpRun, run && run.stage]);
  React.useEffect(() => {
    if (!helpRun || !caps.search) { setRelated(null); return; }
    let off = false; setRelated(null); setRelErr('');
    supportFetch(api, `/api/projects/${pid}/support/search`, { run_id: helpRun, scope: 'project', size: 6 }).then((res) => { if (off) return; if (res.ok) setRelated(res.body); else setRelErr(res.body.error || 'HTTP ' + res.status); });
    return () => { off = true; };
  }, [helpRun, caps.search, api, pid]);
  const openJobs = ((d && d.jobs) || []).filter((j) => JOB_OPEN.includes(j.status));
  const runCase = helpRun ? ((d && d.cases) || []).find((c) => c.run_id === helpRun && c.latest_job) : null;
  const shownJob = jobId || (openJobs[0] && openJobs[0].id) || (runCase && runCase.latest_job.id) || null;
  const ask = async () => {
    const question = q.trim(); if (!question) return;
    if (!key.current) key.current = 'ask-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now());
    setBusy(true); setErr('');
    const res = await supportFetch(api, `/api/projects/${pid}/support/ask`, { question, run_id: helpRun || undefined, idempotency_key: key.current });
    setBusy(false);
    if (res.ok) { setJobId(res.body.job && res.body.job.id); key.current = null; support.reload(); }
    else if (res.status === 409 && res.body.job) { setJobId(res.body.job.id); setErr('An investigation is already open for this project; showing it below.'); }
    else setErr(res.body.error || 'HTTP ' + res.status);
  };
  const cases = (d && d.cases) || [];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {run ? (
        <section aria-label="Run under investigation" style={{ padding: '12px 14px', border: '1px solid ' + (run.status === 'failed' ? 'rgba(238,124,124,0.35)' : C.line), borderRadius: 10 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}><Status s={runDisplayStatus(run)} /><span style={{ fontSize: 13, fontWeight: 600 }}>{String(run.kind).replaceAll('_', ' ')} run</span><span style={{ fontFamily: MONO, fontSize: 11, color: C.text3 }}>{run.id}</span><span style={{ flex: 1 }} /><Btn small kind="quiet" icon="x" onClick={() => onRun(null)}>Clear run</Btn></div>
          <div style={{ fontSize: 12, color: C.text3, marginTop: 6 }}>{run.stage ? 'Stage ' + run.stage + ' · ' : ''}{fmtAgo(run.created_at)}{run.commit ? ' · repo ' + String(run.commit).slice(0, 7) : ''}</div>
          {run.error && <div style={{ marginTop: 8 }}><ErrorLine msg={run.error} /></div>}
        </section>
      ) : helpRun ? <div style={{ fontSize: 12.5, color: C.text3 }}>Run <span style={{ fontFamily: MONO }}>{helpRun}</span> is not in this project. <Btn small kind="quiet" onClick={() => onRun(null)}>Clear</Btn></div>
        : failed.length > 0 && <div><Eyebrow style={{ marginBottom: 6 }}>Recent failed runs</Eyebrow><div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>{failed.map((r) => <Btn key={r.id} small onClick={() => onRun(r.id)} ariaLabel={'Investigate ' + r.kind + ' run ' + r.id}>{String(r.kind).replaceAll('_', ' ')} · {r.stage || 'failed'} · {fmtAgo(r.created_at)}</Btn>)}</div></div>}

      <section aria-label="Ask for an investigation">
        <label htmlFor="rs-ask" style={{ display: 'block', fontSize: 12, color: C.text2, marginBottom: 6 }}>Question for a Factory Droid investigation</label>
        <textarea id="rs-ask" value={q} rows={3} maxLength={2000} onChange={(e) => { setQ(e.target.value); setEdited(true); key.current = null; }} style={{ ...inputCss(), height: 'auto', padding: 10, resize: 'vertical', lineHeight: 1.5 }} />
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 8 }}>
          <Btn kind="primary" icon="assistant" disabled={busy || !q.trim() || !caps.ask || support.status !== 'ready' && support.status !== 'stale'} onClick={ask}>{busy ? 'Sending…' : 'Investigate'}</Btn>
          <span style={{ fontSize: 11.5, color: C.text3, flex: 1, minWidth: 200, lineHeight: 1.5 }}>
            {support.status === 'unavailable' ? 'Investigations are not available on this backend yet. ' : support.status === 'loading' ? 'Checking availability… ' : !caps.ask && caps.ask_reason ? caps.ask_reason + '. ' : ''}{caps.ask && !caps.factory_ready ? 'Factory isn’t connected: the request waits; nothing else runs instead. ' : ''}
            Read-only: it can’t change data or run commands.{quotas ? ` ${quotas.asks_left_hour} left this hour.` : ''}
          </span>
        </div>
        {err && <div style={{ marginTop: 8 }}><ErrorLine msg={err} /></div>}
      </section>

      {shownJob && <JobView api={api} pid={pid} jobId={shownJob} canWrite={!!caps.resolve} canResolve={!!caps.resolve} onInvestigate={onInvestigate} onChanged={support.reload} />}

      {helpRun && caps.search && <section aria-label="Related records">
        <Eyebrow style={{ marginBottom: 6 }}>Related records from search</Eyebrow>
        {!related && !relErr && <div role="status" style={{ fontSize: 12, color: C.text3 }}>Searching this project's history…</div>}
        {relErr && <ErrorLine msg={'Search unavailable: ' + relErr} />}
        {related && <>
          {related.run_context && related.run_context.error_excerpt && <div style={{ fontSize: 11.5, color: C.text3, marginBottom: 6 }}>Matched on <span style={{ fontFamily: MONO }}>{related.run_context.error_class || 'error text'}</span>{related.fingerprint ? ' · fingerprint ' + String(related.fingerprint).slice(0, 10) : ''}</div>}
          <CitationList title={related.prior_resolutions && related.prior_resolutions.length ? 'Earlier outcomes for this error' : null} items={related.prior_resolutions} onInvestigate={onInvestigate} />
          <CitationList items={(related.hits || []).filter((h) => h.canonical_id !== 'pipeline_run:' + helpRun)} onInvestigate={onInvestigate} empty="No related records yet." />
        </>}
      </section>}

      {cases.length > 0 && <section aria-label="Cases">
        <Eyebrow style={{ marginBottom: 6 }}>Cases in this project</Eyebrow>
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, border: '1px solid ' + C.line, borderRadius: 10, overflow: 'hidden' }}>
          {cases.map((c, i) => { const lj = c.latest_job, [lbl, col] = lj ? (JOB_LABEL[lj.status] || [lj.status, C.text3]) : ['No investigation', C.text3]; return (
            <li key={c.id} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '9px 12px', borderTop: i ? '1px solid ' + C.line : 'none', flexWrap: narrow ? 'wrap' : 'nowrap' }}>
              <div style={{ flex: 1, minWidth: 0 }}><div style={{ fontSize: 12.5, color: C.text, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.question}</div>
                <div style={{ fontSize: 11, color: C.text3, marginTop: 2 }}>{fmtAgo(c.at)}{c.run_id ? ' · run ' + c.run_id.slice(-6) : ''} · {c.resolution ? (OUTCOME_LABEL[c.resolution.outcome] || c.resolution.outcome) : 'open'}</div></div>
              <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontSize: 11.5, color: C.text2 }}><Dot color={col} pulse={lj && lj.status === 'running'} />{lbl}</span>
              {lj && <Btn small kind="quiet" ariaLabel={'Open investigation for: ' + c.question} onClick={() => setJobId(lj.id)}>Open</Btn>}
            </li>); })}
        </ul>
      </section>}
    </div>
  );
}

function HistoryPanel({ api, pid, support, onInvestigate, narrow }) {
  const caps = (support.data && support.data.capabilities) || {};
  const [q, setQ] = React.useState(''), [scope, setScope] = React.useState('project'), [kind, setKind] = React.useState(''), [res, setRes] = React.useState(null), [busy, setBusy] = React.useState(false), [err, setErr] = React.useState('');
  const seq = React.useRef(0);
  const run = React.useCallback(async (query, sc, kd) => {
    const n = ++seq.current; setBusy(true); setErr('');
    const r = await supportFetch(api, `/api/projects/${pid}/support/search`, { query: query || undefined, scope: sc, filters: kd ? { kind: [kd] } : {}, size: 12 });
    if (n !== seq.current) return;              // stale response guard
    setBusy(false);
    if (r.ok) setRes(r.body); else setErr(r.body.error || 'HTTP ' + r.status);
  }, [api, pid]);
  React.useEffect(() => { if (caps.search) run('', scope, kind); }, [caps.search, scope, kind]);
  if (support.status === 'loading') return <div role="status" style={{ fontSize: 12.5, color: C.text3 }}>Loading history…</div>;
  if (!caps.search) return <Empty title="Search is unavailable" sub={support.status === 'unavailable' ? 'History search is not available on this backend yet.' : 'Search is offline. Your records are still saved.'} />;
  const kinds = ((res && res.facets && res.facets.kind) || []);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <form role="search" onSubmit={(e) => { e.preventDefault(); run(q, scope, kind); }} style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input type="search" aria-label="Search runs, errors, versions and past outcomes" placeholder="Search runs, errors, versions and outcomes" value={q} onChange={(e) => setQ(e.target.value)} style={{ ...inputCss(), flex: '1 1 240px', width: 'auto' }} />
        <select aria-label="Search scope" value={scope} onChange={(e) => setScope(e.target.value)} style={{ ...inputCss(), width: 'auto', flex: 'none' }}><option value="project">This project</option><option value="my_projects">All my projects</option></select>
        <Btn type="submit" kind="primary" disabled={busy}>{busy ? 'Searching…' : 'Search'}</Btn>
      </form>
      {kinds.length > 0 && <div role="group" aria-label="Filter by record type" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <Btn small kind={kind ? 'quiet' : 'ghost'} pressed={!kind} onClick={() => setKind('')}>All</Btn>
        {kinds.map((k) => <Btn key={k.value} small kind={kind === k.value ? 'ghost' : 'quiet'} pressed={kind === k.value} onClick={() => setKind(kind === k.value ? '' : k.value)}>{(KIND_LABEL[k.value] || k.value) + ' · ' + k.count}</Btn>)}
      </div>}
      {err && <ErrorLine msg={err} />}
      {res && <div style={{ fontSize: 11.5, color: C.text3 }} aria-live="polite">{res.total} match{res.total === 1 ? '' : 'es'}{res.query ? ' for “' + res.query + '”' : ''} across {res.searched_projects} project{res.searched_projects === 1 ? '' : 's'} · keyword search; each result's access is re-checked before it is shown{res.dropped_stale ? ` (${res.dropped_stale} outdated hidden)` : ''}.</div>}
      {res && <CitationList title={res.prior_resolutions && res.prior_resolutions.length ? 'Recorded outcomes' : null} items={res.prior_resolutions} onInvestigate={onInvestigate} />}
      {res && <CitationList items={res.hits} onInvestigate={onInvestigate} empty="Nothing matched. Try an error message, a stage name or an object name." />}
    </div>
  );
}

function AssistantSurface({ api, pid, detail, system, narrow, refresh, mode, helpRun, onMode, onRun, onInvestigate, isOwner }) {
  const support = useSupport(api, pid);
  const active = ASSIST_MODES.some((m) => m[0] === mode) ? mode : (helpRun ? 'troubleshoot' : 'setup');
  const fstat = support.data && support.data.provider && support.data.provider.factory && support.data.provider.factory.status;
  const tabKey = (e, i) => { if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return; e.preventDefault(); const n = ASSIST_MODES[(i + (e.key === 'ArrowRight' ? 1 : ASSIST_MODES.length - 1)) % ASSIST_MODES.length][0]; onMode(n); setTimeout(() => { const el = document.getElementById('rs-assist-tab-' + n); if (el) el.focus(); }, 0); };
  return (
    <div style={{ maxWidth: 980, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <header style={{ display: 'flex', alignItems: 'flex-end', gap: 14, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 220 }}><Eyebrow>Assistant</Eyebrow><h2 style={{ margin: '6px 0 0', fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em' }}>{active === 'setup' ? 'Set up this project' : active === 'troubleshoot' ? 'Troubleshoot a problem' : 'Search project history'}</h2>
          <p style={{ margin: '6px 0 0', fontSize: 12.5, color: C.text2, lineHeight: 1.6 }}>{active === 'setup' ? 'Claude reads the project and proposes reviewable changes; you approve each one.' : active === 'troubleshoot' ? 'Factory Droid diagnoses a failure from this project’s records, with sources.' : 'Search runs, errors, versions and outcomes.'}</p></div>
        <div role="tablist" aria-label="Assistant mode" style={{ display: 'flex', gap: 2, padding: 2, border: '1px solid ' + C.line, borderRadius: 999 }}>
          {ASSIST_MODES.map(([k, l], i) => <button key={k} id={'rs-assist-tab-' + k} type="button" role="tab" aria-selected={active === k} aria-controls="rs-assist-panel" tabIndex={active === k ? 0 : -1} className="p2g-nav-item" onKeyDown={(e) => tabKey(e, i)} onClick={() => onMode(k)} style={{ minHeight: 30, padding: '0 14px', borderRadius: 999, border: 'none', cursor: 'pointer', fontFamily: FONT, fontSize: 12.5, background: active === k ? C.raised : 'transparent', color: active === k ? C.text : C.text2, boxShadow: active === k ? 'inset 0 0 0 1px ' + C.line : 'none' }}>{l}</button>)}
        </div>
      </header>
      {active !== 'setup' && <ProviderStatus support={support} isOwner={isOwner} onConnect={isOwner && active === 'troubleshoot' ? <FactoryConnect factoryStatus={fstat} /> : null} />}
      <div id="rs-assist-panel" role="tabpanel" aria-labelledby={'rs-assist-tab-' + active}>
        {active === 'setup' ? <SetupCopilot api={api} pid={pid} detail={detail} system={system} refresh={refresh} narrow={narrow} />
          : active === 'troubleshoot' ? <TroubleshootPanel api={api} pid={pid} detail={detail} support={support} helpRun={helpRun} onRun={onRun} onInvestigate={onInvestigate} narrow={narrow} />
          : <HistoryPanel api={api} pid={pid} support={support} onInvestigate={onInvestigate} narrow={narrow} />}
      </div>
    </div>
  );
}


/* ── RealiSIM shell: authored 216px rail (first-version proportions) organised around the capture-to-export pipeline ── */
const BRAND = 'RealiSIM';
const TAGLINE = 'From your video to simulation-ready assets and environments.';
const WS_STEPS = [['capture', 'Upload', 'upload'], ['inputs', 'Review inputs', 'review'], ['assets', 'Build assets', 'cube'], ['scene', 'Align & simulate', 'sim'], ['export', 'Approve & export', 'box']];
const OBJ_STEPS = [['capture', 'Capture', 'upload'], ['review', 'Review', 'review'], ['simulation', 'Simulation', 'sim'], ['export', 'Export', 'box']];
const OBJ_STEP_ALIAS = { inputs: 'capture', assets: 'review', scene: 'simulation' };
const SECONDARY = [['setup', 'Assistant', 'assistant'], ['library', 'Files', 'folder'], ['settings', 'Settings', 'settings']];
const WS_RUNNING = { queued: 'assets', building: 'assets', accepting: 'assets', layout: 'scene', composing: 'scene', exporting: 'export' };

const projectModeOf = (p) => { const st = (p && p.settings_json) || {}; return st.project_mode || (st.workflow && st.workflow.mode) || 'object'; };

/* Stage marks: done | run | attention | error. Derived only from saved project state; nothing is inferred optimistically. */
function workspaceMarks(wf) {
  const m = {}; if (!wf) return { marks: m, current: 'capture' };
  const b = wf.build, ph = b && b.phase, an = wf.analysis;
  if (wf.primary_upload_id) m.capture = 'done';
  if (an && ['queued', 'running'].includes(an.status)) m.inputs = 'run';
  else if (an && an.status === 'failed') m.inputs = 'error';
  else if (b && ph !== 'cancelled') m.inputs = 'done';
  else if (an && an.status === 'complete') m.inputs = 'attention';
  if (b && ph !== 'cancelled') {
    const runAt = WS_RUNNING[ph];
    if (b.preview) m.assets = 'done';
    if (runAt) m[runAt] = 'run';
    if (ph === 'failed') m[b.preview ? 'scene' : 'assets'] = 'error';
    if (ph === 'awaiting_approval') { m.scene = 'attention'; }
    if (ph === 'exporting') m.scene = 'done';
    if (ph === 'exported') { m.scene = 'done'; m.export = 'done'; }
  }
  const current = ph === 'exported' || ph === 'exporting' ? 'export' : b && b.preview ? 'scene' : b && ph !== 'cancelled' ? 'assets' : (an && an.status === 'complete') || wf.primary_upload_id ? 'inputs' : 'capture';
  return { marks: m, current };
}
function legacyMarks(d) {
  const m = {}; if (!d) return { marks: m, current: 'capture' };
  const runs = d.pipeline_run || [], av = d.asset_version || [], ex = d.gym_export || [];
  const act = (kinds) => runs.find((r) => kinds.includes(r.kind) && ['queued', 'running'].includes(r.status));
  if ((d.upload || []).some((u) => u.status === 'ready')) m.capture = 'done';
  if (act(['asset', 'rebuild', 'scene_capture'])) m.review = 'run';
  else if (av.some((a) => a.status === 'needs_review')) m.review = 'attention';
  else if (av.some((a) => a.status === 'accepted')) m.review = 'done';
  if ((d.scene_version || []).some((s) => s.status === 'validated')) m.simulation = 'done';
  if (act(['export'])) m.export = 'run'; else if (ex.some((e) => e.status === 'ready')) m.export = 'done';
  const current = m.export ? 'export' : m.simulation ? 'export' : m.review === 'done' ? 'simulation' : m.review || m.capture ? 'review' : 'capture';
  return { marks: m, current };
}
function listProgress(p) {
  const mode = projectModeOf(p);
  if (mode === 'workspace') {
    const wf = p.settings_json && p.settings_json.workflow, b = wf && wf.build, ph = b && b.phase, an = wf && wf.analysis;
    if (ph === 'exported') return { mode, label: 'Export ready', tone: 'ok', step: 'export' };
    if (ph === 'exporting') return { mode, label: 'Validating export', tone: 'run', step: 'export' };
    if (ph === 'awaiting_approval') return { mode, label: 'Inspect layout and approve', tone: 'attention', step: 'scene' };
    if (ph === 'failed') return { mode, label: 'Build stopped · see details', tone: 'error', step: b.preview ? 'scene' : 'assets' };
    if (ph && WS_RUNNING[ph]) return { mode, label: ph === 'layout' || ph === 'composing' ? 'Composing scene' : 'Building assets', tone: 'run', step: WS_RUNNING[ph] };
    if (an && ['queued', 'running'].includes(an.status)) return { mode, label: 'Analyzing video', tone: 'run', step: 'inputs' };
    if (an && an.status === 'complete') return { mode, label: 'Review inputs', tone: 'attention', step: 'inputs' };
    if (wf && wf.primary_upload_id) return { mode, label: 'Video uploaded · analyze next', tone: 'idle', step: 'inputs' };
    return { mode, label: 'Upload a workspace video', tone: 'idle', step: 'capture' };
  }
  const pk = projectPackage(p);
  if (p.run && ['queued', 'running'].includes(p.run.status)) return { mode, label: 'Running · ' + String(p.run.stage || p.run.kind).replaceAll('_', ' '), tone: 'run', step: p.run.kind === 'export' ? 'export' : 'review' };
  if ((p.run && p.run.status === 'needs_review') || (p.asset && p.asset.status === 'needs_review')) return { mode, label: 'Review asset properties', tone: 'attention', step: 'review' };
  if (pk && pk.status === 'ready') return { mode, label: 'Package ready · v' + pk.package_version, tone: 'ok', step: 'export' };
  if (p.scene && p.scene.status === 'validated') return { mode, label: 'Scene validated · export next', tone: 'idle', step: 'export' };
  if (p.asset && p.asset.status === 'accepted') return { mode, label: p.asset.object_name + ' v' + p.asset.version + ' accepted · simulate next', tone: 'ok', step: 'simulation' };
  if (p.asset) return { mode, label: p.asset.object_name + ' v' + p.asset.version + ' ' + (STATUS[p.asset.status] ? STATUS[p.asset.status][0].toLowerCase() : p.asset.status) + ' · review or rebuild', tone: 'idle', step: 'review' };
  if (p.run && p.run.status === 'failed') return runDisplayStatus(p.run) === 'cancelled' ? { mode, label: 'Last run cancelled', tone: 'idle', step: 'review' } : { mode, label: 'Last run failed · see details', tone: 'error', step: 'review' };
  return { mode, label: 'Upload a capture', tone: 'idle', step: 'capture' };
}
const toneColor = (t) => ({ ok: C.ok, run: C.accent, attention: C.warn, error: C.err, done: C.ok }[t] || C.text3);

function StageMark({ state }) {
  if (!state) return null;
  if (state === 'done') return <span aria-hidden="true" style={{ display: 'inline-flex', color: C.ok }}><Icon name="check" size={12} sw={2.2} /></span>;
  return <Dot color={toneColor(state)} pulse={state === 'run'} />;
}
const MARK_TEXT = { done: 'complete', run: 'in progress', attention: 'needs your review', error: 'needs attention' };

function NavItem({ icon, label, on, onClick, mark, index, current = 'page', compact }) {
  return (
    <button type="button" className="p2g-nav-item" onClick={onClick} aria-current={on ? current : undefined}
      style={{ display: 'flex', alignItems: 'center', gap: 9, minHeight: compact ? 40 : 32, padding: '0 10px', borderRadius: 8, border: 'none', cursor: 'pointer', fontFamily: FONT, fontSize: 12.5, flex: 'none',
        background: on ? C.raised : 'transparent', color: on ? C.text : C.text2, boxShadow: on ? 'inset 0 0 0 1px ' + C.line : 'none', width: '100%', textAlign: 'left' }}>
      {index != null ? <span aria-hidden="true" style={{ width: 14, fontFamily: MONO, fontSize: 10, color: on ? C.accent : C.text3, textAlign: 'center' }}>{String(index).padStart(2, '0')}</span> : <Icon name={icon} size={14} color={on ? C.text : C.text3} />}
      <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</span>
      {mark && <StageMark state={mark} />}{mark && <span className="p2g-sr">, {MARK_TEXT[mark]}</span>}
    </button>
  );
}

function CubeMark({ size = 22 }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.6" strokeLinejoin="round" aria-hidden="true" style={{ flex: 'none' }}><path d="M12 3.5 19.5 7.75v8.5L12 20.5 4.5 16.25v-8.5z" opacity=".5" /><path d="M4.5 7.75 12 12l7.5-4.25M12 12v8.5" /><circle cx="12" cy="12" r="1.7" stroke={C.accent} /></svg>;
}

function DisplayControls({ theme, setTheme, stacked }) {
  const seg = (id, icon, label) => <button type="button" className="p2g-nav-item" aria-pressed={theme === id} aria-label={label} title={label} onClick={() => { if (theme !== id) setTheme(id); }}
    style={{ flex: 1, minHeight: 28, border: 'none', borderRadius: 6, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 5, fontFamily: FONT, fontSize: 11, background: theme === id ? C.raised : 'transparent', color: theme === id ? C.text : C.text3, boxShadow: theme === id ? 'inset 0 0 0 1px ' + C.line : 'none' }}><Icon name={icon} size={12} />{stacked ? label.replace(' theme', '') : null}</button>;
  return (
    <div role="group" aria-label="Display" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
      <div style={{ display: 'flex', gap: 2, padding: 2, borderRadius: 8, border: '1px solid ' + C.line, flex: stacked ? 1 : 'none', minWidth: stacked ? 0 : 62 }}>{seg('light', 'sun', 'Light theme')}{seg('dark', 'moon', 'Dark theme')}</div>
    </div>
  );
}

/* ── activity: real aggregates from GET /api/metrics (public_beta.py); never estimated, never zero-filled on error ── */
function metricVal(data, group, key, seg) { const g = data && data[group], c = g && g[key]; const v = c && typeof c === 'object' ? c[seg] : null; return typeof v === 'number' && isFinite(v) ? v : null; }
function useMetrics(api) {
  const [st, setSt] = React.useState({ status: 'loading', data: null, error: '' });
  const load = React.useCallback(async () => {
    if (!api.base) return;
    try { const j = await api.req('/api/metrics'); setSt({ status: 'ready', data: j, error: '' }); }
    catch (e) { setSt((s) => ({ status: s.data ? 'stale' : 'error', data: s.data, error: e.message })); }
  }, [api]);
  React.useEffect(() => { if (!api.base) return; load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [api.base, load]);
  return { ...st, reload: load };
}
/* ── visits: one open per page load + batched pointer presses (POST /api/visit). No UI; analytics never block the app. ── */
function useVisitTracker(api, rootRef) {
  const clicks = React.useRef(0);
  React.useEffect(() => {
    if (!api.base) return;
    let off = false;
    const send = (json, keepalive) => api.req('/api/visit', { json, keepalive }).catch(() => null);
    // once per page load (not per remount), after the backend actually accepts it; a few spaced retries if it is still starting
    const open = (n) => { if (window.__realisimVisitOpened) return; window.__realisimVisitOpened = 'pending';
      api.req('/api/visit', { json: { opens: 1 } }).then(() => { window.__realisimVisitOpened = true; })
        .catch((e) => { window.__realisimVisitOpened = (e && e.status >= 400 && e.status < 500) ? true : undefined; if (window.__realisimVisitOpened === undefined && n < 4) setTimeout(() => open(n + 1), 15000); }); };
    open(1);
    const flush = (keepalive) => { const n = Math.min(clicks.current, 500); if (!n) return; clicks.current -= n; send({ clicks: n }, keepalive); };
    const el = rootRef.current;
    const onDown = () => { clicks.current += 1; };
    const onVis = () => { if (document.visibilityState === 'hidden') flush(true); };
    if (el) el.addEventListener('pointerdown', onDown, { capture: true, passive: true });
    document.addEventListener('visibilitychange', onVis);
    const t = setInterval(() => { if (!off) flush(false); }, 30000);
    return () => { off = true; clearInterval(t); if (el) el.removeEventListener('pointerdown', onDown, { capture: true }); document.removeEventListener('visibilitychange', onVis); flush(true); };
  }, [api, rootRef]);
}
function ActivityTracker({ metrics, narrow }) {
  const { status, data, error, reload } = metrics;
  const fmt = (v) => v == null ? '—' : v.toLocaleString();
  const eng = [
    ['Users', 'users', 'project_owners', 'People who created a project'],
    ['Visitors', 'users', 'distinct', 'Distinct people who opened RealiSIM, including guests (each browser counts once)'],
  ];
  const rows = [
    ['Projects', 'projects', 'real', 'Projects with an upload or a recorded run'],
    ['Render versions', 'renderable_versions', 'distinct_total', 'Saved asset previews and scene versions'],
    ['Runs', 'pipeline_runs', 'total', 'All recorded pipeline runs, including failed and cancelled runs'],
  ];
  const cap = { margin: 0, fontSize: 10.5, fontWeight: 500, color: C.text3, letterSpacing: '0.06em', textTransform: 'uppercase' };
  return <section aria-label="Total activity" style={{borderTop:'1px solid '+C.line,borderBottom:'1px solid '+C.line,padding:'12px 0 14px'}}>
    <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:10}}>
      <h2 style={cap}>Total activity</h2>
      <span style={{marginLeft:'auto',fontSize:10,color:C.text3}} aria-live="polite">{status==='loading'?'Loading…':status==='stale'?'Refresh unavailable':data?.as_of_ms?'Updated '+fmtAgo(data.as_of_ms):''}</span>
    </div>
    {status==='error'&&!data?<div role="status" style={{display:'flex',alignItems:'center',gap:10,fontSize:12,color:C.text2}}><span title={error}>Activity is unavailable.</span><Btn small kind="quiet" onClick={reload}>Retry</Btn></div>:
    <div style={{display:'grid',gridTemplateColumns:narrow?'minmax(0,1fr)':'minmax(0,1.5fr) minmax(0,1fr)',gap:narrow?16:28,alignItems:'end'}}>
      <div role="group" aria-label="Engagement">
        <div style={{...cap,marginBottom:6,color:C.text2}}>Engagement</div>
        <dl style={{margin:0,display:'grid',gridTemplateColumns:'repeat(2,minmax(0,1fr))'}}>
          {eng.map(([label,g,k,hint],i)=><div key={label} title={hint+' · QA and system records excluded'} style={{paddingLeft:i?18:0,borderLeft:i?'1px solid '+C.line:'none',minWidth:0}}>
            <dd style={{margin:0,fontSize:narrow?38:52,fontWeight:500,lineHeight:1.02,fontFamily:MONO,letterSpacing:'-0.045em',color:C.text}}>{fmt(metricVal(data,g,k,'all'))}</dd>
            <dt style={{marginTop:6,fontSize:12.5,fontWeight:500,color:C.text2}}>{label}</dt>
          </div>)}
        </dl>
      </div>
      <dl style={{margin:0,display:'grid',gridTemplateColumns:'repeat(3,minmax(0,1fr))',borderLeft:narrow?'none':'1px solid '+C.line,paddingLeft:narrow?0:18}}>
        {rows.map(([label,g,k,hint],i)=><div key={g} title={hint+' · QA and system records excluded'} style={{paddingLeft:i?12:0,minWidth:0}}>
          <dd style={{margin:0,fontSize:20,fontWeight:500,lineHeight:1.25,fontFamily:MONO,letterSpacing:'-0.025em',color:C.text}}>{fmt(metricVal(data,g,k,'all'))}</dd>
          <dt style={{marginTop:2,fontSize:10.5,lineHeight:1.4,color:C.text3}}>{label}</dt>
        </div>)}
      </dl>
    </div>}
  </section>;
}

function Dashboard({ api, list, listError, onRetry, system, onOpen, onNew, onDelete, narrow, showArchived, setShowArchived, metrics, beta, isOwner }) {
  const ps = (list && list.projects) || [];
  const shown = ps.filter((p) => showArchived || !p.archived);
  const closed = !isOwner && beta && (beta.enabled === false || beta.open_to_you === false);
  return (
    <div style={{ maxWidth: 1080, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 22 }}>
      <section aria-label="About RealiSIM" style={{ display: 'flex', flexDirection: 'column', gap: 14, paddingBottom: 4 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: narrow ? 24 : 30, fontWeight: 600, letterSpacing: '-0.025em', lineHeight: 1.15 }}>Turn a video into a robot simulation.</h1>
          <p style={{ margin: '8px 0 0', fontSize: 14, lineHeight: 1.6, color: C.text2, maxWidth: 640 }}>RealiSIM turns a phone video of your table or an object into simulation-ready 3D assets and a physics scene you can export as a Gymnasium environment.</p>
        </div>
        <ol style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: narrow ? '1fr' : 'repeat(3,minmax(0,1fr))', gap: narrow ? 10 : 20 }}>
          {[['Upload a video', 'A short clip of your workspace or one object.'], ['Autofill with Claude', 'It finds the objects and sizes. You check them.'], ['Build and export', 'Get 3D assets, a tested scene and a download.']].map(([t, d], i) => (
            <li key={t} style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
              <span aria-hidden="true" style={{ fontFamily: MONO, fontSize: 12, color: C.accent, lineHeight: '20px', minWidth: 16 }}>{i + 1}</span>
              <span><span style={{ display: 'block', fontSize: 13.5, fontWeight: 600, color: C.text }}>{t}</span><span style={{ display: 'block', fontSize: 12.5, color: C.text3, lineHeight: 1.5, marginTop: 2 }}>{d}</span></span>
            </li>))}
        </ol>
      </section>
      <header style={{ display: 'flex', alignItems: 'flex-end', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h2 style={{ margin: 0, fontSize: narrow ? 18 : 20, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.2 }}>Projects</h2>
        </div>
        {!closed && <Btn kind="primary" icon="plus" onClick={onNew}>Add project</Btn>}
      </header>

      <ActivityTracker metrics={metrics} narrow={narrow} />


      {closed ? (
        <section style={{ border: '1px dashed ' + C.line2, borderRadius: 12, padding: '26px 20px' }}>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{beta.enabled === false ? 'The RealiSIM beta is not open yet' : 'Your account is not in the beta yet'}</h2>
          <p style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.6, margin: '6px 0 0', maxWidth: 620 }}>New projects are disabled while the beta is private.</p>
        </section>
      ) : <>
        <section aria-labelledby="rs-projects">
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10, gap: 10 }}>
            <h2 id="rs-projects" style={{ margin: 0, fontSize: 10.5, color: C.text3, textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 500 }}>Latest projects</h2><div style={{ flex: 1 }} />
            <label style={{ fontSize: 12, color: C.text3, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', minHeight: 28 }}><input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} style={{ accentColor: C.accent }} />Show archived</label>
          </div>
          {listError && <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}><div style={{ flex: 1, minWidth: 220 }}><ErrorLine msg={listError} /></div><Btn small onClick={onRetry}>Retry</Btn></div>}
          {!list && !listError && <div style={{ color: C.text3, fontSize: 13, padding: '16px 0' }}>Loading projects…</div>}
          {list && shown.length === 0 && (
            <div style={{ border: '1px dashed ' + C.line2, borderRadius: 12, padding: '32px 20px', textAlign: 'center' }}>
              <div style={{ fontSize: 14, color: C.text, fontWeight: 500 }}>No projects yet</div>
              <div style={{ fontSize: 12.5, color: C.text3, marginTop: 6, lineHeight: 1.55 }}>Upload a workspace video or one object.</div>
              <div style={{ marginTop: 14 }}><Btn kind="primary" icon="plus" onClick={onNew}>Add project</Btn></div>
            </div>
          )}
          {shown.length > 0 && <ul style={{ listStyle: 'none', margin: 0, padding: 0, border: '1px solid ' + C.line, borderRadius: 12, overflow: 'hidden', background: C.panel }}>
            {shown.map((p, i) => {
              const pr = listProgress(p), steps = pr.mode === 'workspace' ? WS_STEPS : OBJ_STEPS, stepLabel = (steps.find((s) => s[0] === (pr.mode === 'workspace' ? pr.step : (OBJ_STEP_ALIAS[pr.step] || pr.step))) || [])[1];
              return (
                <li key={p.id} className="p2g-row" onClick={() => onOpen(p.id)} style={{ display: 'grid', gridTemplateColumns: narrow ? '40px minmax(0,1fr)' : '44px minmax(180px,1.4fr) minmax(160px,1fr) auto', gap: narrow ? 12 : 16, alignItems: 'center', padding: '11px 14px', borderTop: i ? '1px solid ' + C.line : 'none', cursor: 'pointer' }}>
                  <Thumb api={api} pid={p.id} fid={p.thumb_file_id} icon={pr.mode === 'workspace' ? 'layers' : 'cube'} size={narrow ? 40 : 44} />
                  <div style={{ minWidth: 0 }}>
                    <button type="button" onClick={(e) => { e.stopPropagation(); onOpen(p.id); }} style={{ all: 'unset', cursor: 'pointer', fontSize: 13.5, fontWeight: 500, color: C.text, display: 'block', maxWidth: '100%', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.name}{p.archived && <span style={{ color: C.text3, fontWeight: 400 }}> · archived</span>}</button>
                    <div style={{ fontSize: 11.5, color: C.text3, marginTop: 3 }}>{pr.mode === 'workspace' ? 'Workspace video' : 'Single object'}{p.role !== 'owner' ? ' · shared with you' : ''} · updated {fmtAgo(p.updated_at)}</div>
                    {narrow && <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: C.text2, marginTop: 5 }}><Dot color={toneColor(pr.tone)} pulse={pr.tone === 'run'} />{pr.label}</div>}
                  </div>
                  {!narrow && <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12, color: C.text2, minWidth: 0 }}><Dot color={toneColor(pr.tone)} pulse={pr.tone === 'run'} /><span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{pr.label}</span></div>}
                  <div style={{display:'flex',justifyContent:'flex-end',gap:5,flexWrap:'wrap',gridColumn:narrow?'2':undefined}} onClick={(e)=>e.stopPropagation()}>
                    {!narrow && <Btn small icon="chevron" ariaLabel={`Open ${p.name}`} onClick={()=>onOpen(p.id)}>Open</Btn>}
                    {p.role === 'owner' && <Btn small kind="quiet" icon="trash" title="Delete project" ariaLabel={`Delete project ${p.name}`} style={{color:C.text3,padding:'4px 8px'}} onClick={()=>onDelete(p)} />}
                  </div>
                </li>
              );
            })}
          </ul>}
        </section>

      </>}
    </div>
  );
}

function projectHasActiveWork(d) {
  if ((d.pipeline_run || []).some(r => ['queued','running'].includes(r.status))) return true;
  return ['queued','building','accepting','layout','composing','exporting'].includes(d.project?.settings_json?.workflow?.build?.phase);
}
function DeleteProjectDialog({api,project,onClose,onDeleted}) {
  const [data,setData]=React.useState(null),[typed,setTyped]=React.useState(''),[busy,setBusy]=React.useState(false),[error,setError]=React.useState('');
  const pending=React.useRef(false),input=React.useRef(null);
  React.useEffect(()=>{let off=false;const ac=new AbortController();api.req(`/api/projects/${project.id}`,{signal:ac.signal}).then(d=>{if(!off)setData(d);}).catch(e=>{if(!off)setError(e.message);});return()=>{off=true;ac.abort();};},[api,project.id]);
  React.useEffect(()=>{if(data&&input.current)input.current.focus();},[!!data]);
  const owned=data?.project?.role==='owner',active=data&&projectHasActiveWork(data),name=data?.project?.name||project.name;
  const close=()=>{if(!pending.current)onClose();};
  const remove=async(e)=>{
    e?.preventDefault();
    if(pending.current||!owned||active||typed!==name)return;
    pending.current=true;setBusy(true);setError('');let sent=false;
    try {
      const fresh=await api.req(`/api/projects/${project.id}`);
      if(fresh.project.role!=='owner')throw new Error('Only the project owner can delete it.');
      if(fresh.project.name!==typed){setData(fresh);setTyped('');throw new Error('The project was renamed. Confirm its current name.');}
      if(projectHasActiveWork(fresh)){setData(fresh);throw new Error('Cancel active or queued jobs before deleting.');}
      sent=true;await api.req(`/api/projects/${project.id}`,{method:'DELETE'});
      let absent=false;try{await api.req(`/api/projects/${project.id}`);}catch(check){if(check.status===404)absent=true;else throw check;}
      if(!absent)throw new Error('Deletion could not be verified. Check the project list before retrying.');
      pending.current=false;setBusy(false);onDeleted();
    } catch(e) {
      setTyped('');
      setError(sent?'Deletion was not confirmed. Check the project list before retrying. '+e.message:e.message);
    } finally {pending.current=false;setBusy(false);}
  };
  return <Modal title="Delete project" onClose={close} width={480}>
    <div style={{fontSize:14,fontWeight:600,overflowWrap:'anywhere',marginBottom:8}}>{name}</div>
    <p style={{fontSize:12.5,color:C.text2,lineHeight:1.55,margin:'0 0 14px'}}>Permanently deletes its uploads, versions, runs and exports. This cannot be undone.</p>
    {!data&&!error&&<div role="status" style={{fontSize:12.5,color:C.text3,marginBottom:12}}>Checking project…</div>}
    {data&&!owned&&<ErrorLine msg="Only the project owner can delete it."/>}
    {active&&<ErrorLine msg="Cancel active or queued jobs before deleting."/>}
    {error&&<ErrorLine msg={error}/>}
    <form onSubmit={remove} style={{marginTop:12}}>
      <label style={{display:'block',fontSize:12,color:C.text2,marginBottom:6}}>Type the project name to confirm</label>
      <input ref={input} aria-label="Project name confirmation" autoComplete="off" spellCheck={false} value={typed} placeholder={name} disabled={busy||!owned||!!active} onChange={e=>setTyped(e.target.value)} style={{...inputCss(),width:'100%'}}/>
      <div style={{display:'flex',justifyContent:'flex-end',gap:8,flexWrap:'wrap',marginTop:16}}>
        <Btn disabled={busy} onClick={close}>Cancel</Btn>
        <Btn type="submit" kind="danger" icon="trash" disabled={busy||!owned||!!active||typed!==name}>{busy?'Deleting…':'Delete project'}</Btn>
      </div>
    </form>
  </Modal>;
}

function UserDiv() {
  const ing = useIngressUrl('photo2grasp', { ttlSeconds: 604800 }); // service name kept for compatibility; a `realisim` alias may be added later
  const { theme, setTheme } = useTheme();
  const isOwner = !!userId && userId === ownerId;
  const api = React.useMemo(() => makeApi(ing.url), [ing.url]);
  const [route, setRoute] = React.useState(readUrl), [system, setSystem] = React.useState(null), [caps, setCaps] = React.useState(null);
  const [list, setList] = React.useState(null), [listErr, setListErr] = React.useState(''), [detail, setDetail] = React.useState(null), [detailErr, setDetailErr] = React.useState(''), [sysErr, setSysErr] = React.useState('');
  const [deleteTarget, setDeleteTarget] = React.useState(null);
  const [newOpen, setNewOpen] = React.useState(false), [showArchived, setShowArchived] = React.useState(false), [health, setHealth] = React.useState(null), [, tick] = React.useState(0);
  const [box, setBox] = React.useState({ w: 1100, h: 700 }), [menuOpen, setMenuOpen] = React.useState(false);
  const root = React.useRef(null), mainRef = React.useRef(null), menuBtn = React.useRef(null), current = React.useRef(route); current.current = route;
  const metrics = useMetrics(api);
  useVisitTracker(api, root);
  const z = 1, effW = box.w / z, railMode = effW >= 760, narrow = (railMode ? effW - 216 : effW) < 700;
  const pid = route.pid, tab = route.tab || 'flow';


  const nav = (patch, replace = false) => { const next = { ...current.current, ...patch }; if (!next.pid) { next.tab = null; next.step = null; } if (next.tab !== 'setup') { next.assist = null; next.helpRun = null; } setRoute(next); setMenuOpen(false); const u = new URL(window.location.href); for (const [k, v] of [['p', next.pid], ['tab', next.tab], ['step', next.step], ['assistMode', next.assist], ['helpRun', next.helpRun]]) v ? u.searchParams.set(k, v) : u.searchParams.delete(k); window.history[replace ? 'replaceState' : 'pushState']({}, '', u.toString()); };
  const investigate = React.useCallback((runId) => nav({ tab: 'setup', assist: 'troubleshoot', helpRun: runId || null }), []);
  React.useEffect(() => { const f = () => { setRoute(readUrl()); setMenuOpen(false); }; window.addEventListener('popstate', f); return () => window.removeEventListener('popstate', f); }, []);
  React.useEffect(() => { const el = root.current; if (!el) return; const m = () => setBox({ w: el.clientWidth || 1, h: el.clientHeight || 1 }); const ro = new ResizeObserver(m); ro.observe(el); m(); return () => ro.disconnect(); }, []);
  React.useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 15000); return () => clearInterval(t); }, []);
  React.useEffect(() => { const m = mainRef.current; if (m && m.scrollTo) m.scrollTo(0, 0); }, [route.pid, route.tab, route.step]);

  const loadList = React.useCallback(async () => { try { setList(await api.req('/api/projects')); setListErr(''); } catch (e) { setListErr(e.message); } }, [api]);
  const loadDetail = React.useCallback(async () => { if (!pid) return; try { const d = await api.req(`/api/projects/${pid}`); if (current.current.pid === pid) { setDetail(d); setDetailErr(''); } return d; } catch (e) { if (current.current.pid === pid) setDetailErr(e.message); } }, [api, pid]);
  // Owner reads /api/system; everyone else reads the guarded /api/beta/capabilities subset (owner-only routes are never called by visitors).
  const loadSystem = React.useCallback(async (refreshHf) => {
    try {
      if (isOwner) { setSystem(await api.req(refreshHf ? '/api/system?hf=refresh' : '/api/system')); }
      else { const c = await api.req('/api/beta/capabilities'); setCaps(c); setSystem(c.system || null); }
      setSysErr('');
    } catch (e) { setSysErr(e.message); }
  }, [api, isOwner]);
  const refreshSystem = React.useCallback(() => loadSystem(true), [loadSystem]);
  React.useEffect(() => { if (!ing.url) return; let off = false, t = 0;
    // GET-only health poll: a 503 with database_starting / recovering_saved_jobs is shown as reconnecting and re-checked every 5 s; nothing is re-POSTed.
    const ping = async () => { let next = 20000; try { const r = await fetch(ing.url + '/health'); const j = await r.json().catch(() => ({})); if (!off) { if (r.ok) setHealth(j); else if (j && (j.code === 'database_starting' || j.code === 'recovering_saved_jobs')) { setHealth({ ...j, starting: true }); next = 5000; } else setHealth(null); } } catch (e) { if (!off) setHealth(null); next = 10000; } if (!off) t = setTimeout(ping, next); };
    ping(); loadSystem(false); return () => { off = true; clearTimeout(t); }; }, [ing.url, loadSystem]);
  React.useEffect(() => { if (!ing.url) return; loadList(); const t = setInterval(loadList, 30000); return () => clearInterval(t); }, [ing.url, loadList, pid]);
  const active = !!(detail && detail.pipeline_run && detail.pipeline_run.some((r) => ['queued', 'running'].includes(r.status)));
  React.useEffect(() => { if (!ing.url || !pid) { setDetail(null); setDetailErr(''); return; } setDetail((d) => (d && d.project && d.project.id === pid ? d : null)); setDetailErr(''); loadDetail(); const t = setInterval(loadDetail, active ? 2500 : 10000); return () => clearInterval(t); }, [ing.url, pid, active, loadDetail]);
  React.useEffect(() => { if (!menuOpen) return; const f = (e) => { if (e.key === 'Escape') { setMenuOpen(false); menuBtn.current && menuBtn.current.focus(); } }; window.addEventListener('keydown', f); return () => window.removeEventListener('keydown', f); }, [menuOpen]);

  const mode = detail && detail.project && detail.project.settings_json ? projectModeOf(detail.project) : 'object';
  const steps = mode === 'workspace' ? WS_STEPS : OBJ_STEPS;
  const { marks, current: resumeAt } = mode === 'workspace' ? workspaceMarks(detail && detail.project && detail.project.settings_json && detail.project.settings_json.workflow) : legacyMarks(detail);
  const rawStep = route.step || resumeAt, step = mode === 'workspace' ? rawStep : (OBJ_STEP_ALIAS[rawStep] || rawStep);
  const canSuggest = isOwner || !!(caps && caps.features && (caps.features.suggest_fields || caps.features.claude));
  const goto = (t) => nav({ tab: t });
  const props = { api, pid, detail, system, narrow, refresh: loadDetail, goto, refreshSystem, canSuggest };
  const advanced = !['flow', 'setup', 'library', 'settings'].includes(tab);
  const stageLabel = tab === 'flow' ? ((steps.find((s) => s[0] === step) || [])[1] || '') : ((SECONDARY.find((s) => s[0] === tab) || [])[1] || 'Advanced tools');
  const starting = !!(health && health.starting);
  const online = (health && !starting) || (!isOwner && caps);
  const beta = caps && caps.beta;

  const navBody = (drawer) => (
    <>
      <NavItem icon="grid" label="Dashboard" on={!pid} onClick={() => nav({ pid: null })} compact={drawer} />
      {pid && <div style={{ marginTop: 14 }}>
        <div style={{ padding: '0 10px 6px', fontSize: 12, color: C.text, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={detail ? detail.project.name : ''}>{detail ? detail.project.name : 'Loading project…'}</div>
        <div style={{ padding: '0 10px 8px', fontSize: 10.5, color: C.text3 }}>{detail ? (mode === 'workspace' ? 'Workspace video' : 'Single object') : ''}</div>
        {detail && <div role="list" aria-label="Pipeline stages" style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {steps.map(([k, l], i) => <div role="listitem" key={k}><NavItem index={i + 1} label={l} on={tab === 'flow' && step === k} current="step" mark={marks[k]} compact={drawer} onClick={() => nav({ tab: 'flow', step: k })} /></div>)}
        </div>}
        {detail && <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 12, paddingTop: 10, borderTop: '1px solid ' + C.line }}>
          {SECONDARY.map(([k, l, ic]) => <NavItem key={k} icon={ic} label={l} on={tab === k} compact={drawer} onClick={() => nav({ tab: k })} />)}
        </div>}
      </div>}
    </>
  );
  const statusBlock = (
    <div style={{ fontSize: 10.5, color: C.text3, lineHeight: 1.65 }}>
      <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={sysErr ? 'System status unavailable' : undefined}><Dot color={online ? C.ok : starting ? C.warn : C.text3} />{online ? 'Online' : starting ? 'Reconnecting' : ing.url ? 'Offline' : 'Connecting'}</div>
    </div>
  );
  const brandRow = (
    <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
      <CubeMark />
      <div style={{ minWidth: 0, fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>{BRAND}</div>
    </div>
  );

  const mh = detail && detail.maintenance && detail.maintenance.hold ? detail.maintenance : null;
  const procHold = mh || (starting ? { reason: 'reconnecting to saved state', label: 'RealiSIM is reconnecting to its saved state; new processing will be available shortly.' } : null);   // canProcess gate; canWrite (edits, uploads) is separate
  const runData = React.useMemo(() => (pid && detail ? { api, pid, checkpoints: Array.isArray(detail.checkpoints) ? detail.checkpoints : null, refresh: loadDetail, goto: (t) => nav({ tab: t }), canWrite: ['owner', 'editor'].includes((detail.project && detail.project.role) || 'owner'), hold: procHold, canProcess: !procHold } : null), [api, pid, detail, loadDetail, procHold && procHold.reason]);
  const reconnecting = starting ? <div role="status" style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 14, fontSize: 12, color: C.text2, lineHeight: 1.5 }}><Dot color={C.warn} /><span>{(health && health.error) || 'RealiSIM is reconnecting to its saved state.'} Checking again every few seconds; nothing you started is sent twice.</span></div> : null;
  const maintenance = mh ? <div role="status" style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 14, fontSize: 12, color: C.text2, lineHeight: 1.5 }}><Dot color={C.warn} /><span>Processing paused. {mh.reason ? String(mh.reason).replace(/[.]+$/, '') + '. ' : ''}Saved work is safe.</span></div> : null;
  let content;
  if (!ing.url) content = <div style={{ padding: 20, color: C.text3, fontSize: 13 }} role="status">{ing.error ? 'Backend unavailable: ' + ing.error.message : 'Connecting to the RealiSIM backend…'}</div>;
  else if (!pid) content = <Dashboard api={api} list={list} listError={listErr} onRetry={loadList} system={system} onOpen={(id) => nav({ pid: id, tab: 'flow', step: null })} onNew={() => setNewOpen(true)} onDelete={setDeleteTarget} narrow={narrow} showArchived={showArchived} setShowArchived={setShowArchived} metrics={metrics} beta={beta} isOwner={isOwner} />;
  else if (!detail) content = detailErr ? <div style={{ maxWidth: 640 }}><ErrorLine msg={detailErr} /><div style={{ display: 'flex', gap: 8, marginTop: 10 }}><Btn onClick={loadDetail}>Retry</Btn><Btn kind="quiet" onClick={() => nav({ pid: null })}>Back to dashboard</Btn></div></div> : <div style={{ padding: 20, color: C.text3, fontSize: 13 }} role="status">Loading project…</div>;
  else {
    const flowProps = { ...props, step, autoResume: !route.step, showProgress: !railMode, onStep: (s, replace) => nav({ tab: 'flow', step: s }, !!replace) };
    content = <>
      {detailErr && <div style={{ marginBottom: 14 }}><ErrorLine msg={'Showing the last loaded state. Refresh failed: ' + detailErr} /></div>}
      {advanced && <div style={{ marginBottom: 18 }}><Btn icon="back" onClick={() => nav({ tab: 'flow', step: null })}>Back to guided workflow</Btn></div>}
      {tab === 'flow' ? (mode === 'workspace' ? <PrimaryWorkflow key={pid} {...flowProps} /> : <LegacyObjectFlow key={pid} {...flowProps} />) :
        tab === 'setup' ? <AssistantSurface {...props} mode={route.assist} helpRun={route.helpRun} isOwner={isOwner} onMode={(m) => nav({ tab: 'setup', assist: m })} onRun={(id) => nav({ tab: 'setup', assist: 'troubleshoot', helpRun: id })} onInvestigate={investigate} /> :
        tab === 'library' ? <LibraryScreen {...props} /> :
        tab === 'settings' ? <SettingsScreen key={pid} {...props} onDeleted={() => { nav({ pid: null }); loadList(); }} /> :
        tab === 'review' ? <ReviewScreen {...props} /> :
        tab === 'robot' ? <RobotScreen {...props} /> :
        tab === 'workcell' ? <WorkcellComposer {...props} /> :
        tab === 'simulation' ? (mode === 'object' ? <LegacySimulationScreen {...props} /> : <SimulationScreen {...props} />) :
        tab === 'export' ? (mode === 'object' ? <LegacyExportScreen {...props} /> : <ExportScreen {...props} />) :
        tab === 'capture' ? <CaptureWizard {...props} singleObjectOnly={mode === 'object'} onStarted={() => { loadDetail(); nav({ tab: 'review' }); }} /> :
        tab === 'deploy' ? <DeploymentReadiness {...props} /> : <WorkcellOverview {...props} />}
    </>;
  }

  const css = `@keyframes p2gPulse{0%,100%{opacity:1}50%{opacity:.3}}@keyframes p2gSpin{to{transform:rotate(360deg)}}.p2g-pulse{animation:p2gPulse 1.6s ease-in-out infinite}.p2g-spin{animation:p2gSpin .9s linear infinite}
.p2g-row:hover,.p2g-nav-item:hover{background:${C.hover}}.p2g-root ::selection{background:rgba(240,144,95,0.3)}
.p2g-root :focus-visible{outline:2px solid ${C.accent};outline-offset:2px}.p2g-root main:focus{outline:none}
.p2g-root select option{background:${C.panel}}.p2g-root input[type=range]{height:4px}
.p2g-md pre{white-space:pre-wrap;background:${C.sunken};padding:12px;border-radius:8px}.p2g-md a{color:${C.accent}}.p2g-md table{border-collapse:collapse;font-size:12px}.p2g-md th,.p2g-md td{padding:5px;border:1px solid ${C.line}}
.p2g-sr{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;margin:-1px;padding:0;border:0}
.p2g-skip{position:absolute;left:10px;top:-80px;z-index:60;padding:8px 14px;border-radius:999px;border:1px solid ${C.accentLine};background:${C.panel};color:${C.text};font:500 12.5px ${FONT};cursor:pointer}.p2g-skip:focus{top:10px}
@media (pointer:coarse){.p2g-root .p2g-btn,.p2g-root .p2g-nav-item{min-height:44px}}
@media (prefers-reduced-motion:reduce){.p2g-pulse,.p2g-spin{animation:none}.p2g-root *{transition:none!important;scroll-behavior:auto!important}}`;

  const shellStyle = z === 1 ? { width: '100%', height: '100%' } : { zoom: z, width: box.w / z, height: box.h / z };
  return (
    <div ref={root} className="w-full p2g-root" style={{ ...colorVars(theme === 'dark' ? DARK_C : LIGHT_C), height: 'var(--mana-app-height,82vh)', position: 'relative', background: C.bg, color: C.text, fontFamily: FONT, border: '1px solid ' + C.line, borderRadius: 14, overflow: 'hidden', boxSizing: 'border-box', colorScheme: theme === 'dark' ? 'dark' : 'light' }}>
      <style>{css + '@container (max-width: 230px) { .p2g-kv { flex-direction: column; gap: 3px !important; } .p2g-kv > span { flex: none !important; width: 100%; overflow-wrap: anywhere; } }'}</style>
      <div style={{ ...shellStyle, display: 'flex', flexDirection: railMode ? 'row' : 'column', position: 'relative', overflow: 'hidden' }}>
        <button type="button" className="p2g-skip" onClick={() => { const m = mainRef.current; if (m) m.focus(); }}>Skip to main content</button>
        {railMode ? (
          <nav aria-label={BRAND} style={{ width: 216, flex: 'none', borderRight: '1px solid ' + C.line, background: C.panel, display: 'flex', flexDirection: 'column', padding: '14px 10px', boxSizing: 'border-box', overflowY: 'auto' }}>
            <div style={{ padding: '2px 8px 14px' }}>{brandRow}</div>
            {navBody(false)}
            <div style={{ flex: 1, minHeight: 12 }} />
            <div style={{ padding: '10px 4px 2px', borderTop: '1px solid ' + C.line, marginTop: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
              <DisplayControls theme={theme} setTheme={setTheme} />
              <div style={{ padding: '0 4px' }}>{statusBlock}</div>
            </div>
          </nav>
        ) : (
          <header style={{ flex: 'none', position: 'relative', zIndex: 30, background: C.panel, borderBottom: '1px solid ' + C.line }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px', minHeight: 52 }}>
              <button ref={menuBtn} type="button" className="p2g-nav-item" aria-expanded={menuOpen} aria-controls="rs-drawer" aria-label={menuOpen ? 'Close navigation' : 'Open navigation'} onClick={() => setMenuOpen((o) => !o)} style={{ width: 40, height: 40, borderRadius: 10, border: '1px solid ' + C.line, background: 'transparent', color: C.text2, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', flex: 'none' }}><Icon name={menuOpen ? 'x' : 'menu'} size={17} /></button>
              <CubeMark size={20} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 650, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{pid && detail ? detail.project.name : BRAND}</div>
                {pid && stageLabel && <div style={{ fontSize: 10.5, color: C.text3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{stageLabel}</div>}
              </div>
              <Btn small kind="quiet" icon={theme === 'dark' ? 'sun' : 'moon'} ariaLabel={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} />
            </div>
            {menuOpen && <>
              <div onClick={() => setMenuOpen(false)} style={{ position: 'absolute', top: '100%', left: 0, right: 0, height: Math.max(0, box.h / z - 53), background: 'rgba(5,6,8,0.35)' }} aria-hidden="true" />
              <nav id="rs-drawer" aria-label={BRAND} style={{ position: 'absolute', top: '100%', left: 0, right: 0, maxHeight: Math.max(160, box.h / z - 60), overflowY: 'auto', background: C.panel, borderBottom: '1px solid ' + C.line, padding: '10px 10px 14px', boxSizing: 'border-box', display: 'flex', flexDirection: 'column', gap: 2, boxShadow: '0 12px 32px rgba(0,0,0,0.18)' }}>
                {navBody(true)}
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid ' + C.line, display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <DisplayControls theme={theme} setTheme={setTheme} stacked />
                  {statusBlock}
                </div>
              </nav>
            </>}
          </header>
        )}
        <main ref={mainRef} tabIndex={-1} aria-label={pid ? (detail ? detail.project.name + ' · ' + stageLabel : 'Project') : 'Dashboard'} style={{ flex: 1, minWidth: 0, minHeight: 0, overflow: 'auto', padding: narrow ? '18px 14px 32px' : '24px 28px 40px', boxSizing: 'border-box' }}>
          {pid && detail && <h1 className="p2g-sr">{BRAND} · {detail.project.name} · {stageLabel}</h1>}
          <InvestigateCtx.Provider value={pid && detail ? investigate : null}><RunDataCtx.Provider value={runData}><div style={{ maxWidth: 1140, margin: '0 auto' }}>{reconnecting}{maintenance}{content}</div></RunDataCtx.Provider></InvestigateCtx.Provider>
        </main>
        {deleteTarget && <DeleteProjectDialog key={deleteTarget.id} api={api} project={deleteTarget} onClose={()=>setDeleteTarget(null)} onDeleted={()=>{setDeleteTarget(null);loadList();if(current.current.pid===deleteTarget.id)nav({pid:null});}} />}
      {newOpen && <NewProject api={api} system={system} onClose={() => setNewOpen(false)} onCreated={(id) => { setNewOpen(false); loadList(); nav({ pid: id, tab: 'flow', step: 'capture' }); }} />}
      </div>
    </div>
  );
}


function RailItem({ icon, label, on, onClick, mark, narrow }) {
  return (
    <button onClick={onClick} style={{ display: 'flex', alignItems: 'center', gap: 9, height: 32, padding: '0 10px', borderRadius: 8, border: 'none', cursor: 'pointer', fontFamily: FONT, fontSize: 12.5, flex: 'none',
      background: on ? C.raised : 'transparent', color: on ? C.text : C.text2, boxShadow: on ? 'inset 0 0 0 1px ' + C.line : 'none', width: narrow ? 'auto' : '100%', textAlign: 'left' }}>
      <Icon name={icon} size={14} color={on ? C.text : C.text3} /><span style={{ flex: 1 }}>{label}</span>{mark && <Dot color={mark} pulse={mark === C.accent} />}
    </button>
  );
}

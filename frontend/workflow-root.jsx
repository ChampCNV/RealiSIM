
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
          {[['Upload a video', 'A short clip of your workspace or one object.'], ['Autofill with Claude + Apify web scraper', 'Claude finds the objects and sizes; Apify looks up real product specs online. You check them.'], ['Build and export', 'Get 3D assets, a tested scene and a download.']].map(([t, d], i) => (
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

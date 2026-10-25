
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

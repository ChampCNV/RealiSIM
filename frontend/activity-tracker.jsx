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

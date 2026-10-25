
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

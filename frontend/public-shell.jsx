/* ── RealiSIM product shell (public-shell.jsx, appended by build_public.py; beta surface only) ─────────────────────
   The beta page shows ONLY the product, for every viewer (anonymous or signed-in Mana user): the same root element is
   promoted to the top layer with the manual Popover API and sized to the full window. This is presentation, not
   security: projects stay private through the authenticated project API and SpacetimeDB; frontend code is inspectable.
   - Fails closed: without Popover support the app is not rendered at all (no workspace-embedded fallback).
   - Focus stays inside the product (Tab cycles, focus that escapes is pulled back); app modals keep their own trap.
   - Keyboard, paste, drag/drop and context-menu events that start inside the product stop at <body>, after the app's
     own React handlers ran, so Mana workspace shortcuts (chat, command palette, undo) never see them. Plain Escape
     still bubbles so the app's own window-level Escape handlers keep working (Mana only closes hidden chat on it).
   - Owner only: ?manage=1 shows the ordinary embedded app for Share administration; visitors ignore it.
   The original component is RealiSimCore; HfConnect is owner-only here (visitors use the owner-funded quota). */
const RS_SHELL_VERSION = 'realisim-public-shell-2';
const RS_FOCUSABLE = 'a[href],area[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),iframe,summary,[contenteditable="true"],[tabindex]:not([tabindex="-1"])';
const RS_ISOLATED_EVENTS = ['keydown', 'keyup', 'keypress', 'paste', 'copy', 'cut', 'drop', 'dragover', 'dragenter', 'dragleave', 'contextmenu'];

function rsIsOwner() { return !!userId && !!ownerId && userId === ownerId; }
function rsPopoverSupported() {
  try {
    return typeof HTMLElement !== 'undefined' && typeof HTMLElement.prototype.showPopover === 'function'
      && typeof HTMLElement.prototype.hidePopover === 'function' && !!(window.CSS && CSS.supports && CSS.supports('selector(:popover-open)'));
  } catch (e) { return false; }
}
function rsManageParam() { try { return new URL(window.location.href).searchParams.get('manage') === '1'; } catch (e) { return false; } }
function rsFocusables(scope) {
  return [...scope.querySelectorAll(RS_FOCUSABLE)].filter((n) => {
    if (n.closest('[inert]')) return false;
    if (!n.getClientRects().length) return false;
    const s = getComputedStyle(n);
    return s.visibility !== 'hidden' && s.display !== 'none';
  });
}
function rsTrapScope(layer) {
  const modals = [...layer.querySelectorAll('[aria-modal="true"]')].filter((m) => m.getClientRects().length);
  return modals.length ? modals[modals.length - 1] : layer;
}

/* Visitors run mesh generation on the owner-funded hosted quota; only the owner manages the Hugging Face token. */
function HfConnect(props) {
  if (rsIsOwner()) return <HfConnectCore {...props} />;
  return <div style={{ fontSize: 12.5, color: C.text2, lineHeight: 1.55 }}>Mesh generation uses RealiSIM’s hosted GPU quota. No token needed. If the shared daily quota is used up, retry later.</div>;
}


function rsInertOutside(el) {
  const marked = new Map(), tag = 'rs-' + Math.random().toString(36).slice(2);
  const apply = () => {
    if (!el || !el.isConnected) return;
    for (let branch=el; branch && branch!==document.body && branch.parentElement; branch=branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling===branch || ['SCRIPT','STYLE','LINK','META'].includes(sibling.tagName)) continue;
        if (!marked.has(sibling)) marked.set(sibling,{inert:sibling.getAttribute('inert'),aria:sibling.getAttribute('aria-hidden'),tag:sibling.getAttribute('data-realisim-inert')});
        sibling.setAttribute('inert',''); sibling.setAttribute('aria-hidden','true'); sibling.setAttribute('data-realisim-inert',tag);
      }
    }
  };
  apply();
  const observer=new MutationObserver(apply);observer.observe(document.body,{childList:true,subtree:true});
  return () => {
    observer.disconnect();
    for (const [node,old] of marked) {
      if (node.getAttribute('data-realisim-inert')!==tag) continue;
      for (const [key,value] of [['inert',old.inert],['aria-hidden',old.aria],['data-realisim-inert',old.tag]]) value===null?node.removeAttribute(key):node.setAttribute(key,value);
    }
  };
}

function RsUnsupported() {
  const { theme } = useTheme(); const root=React.useRef(null); const dark=theme==='dark';
  React.useLayoutEffect(()=>{const el=root.current;if(!el)return;const restore=rsInertOutside(el);try{if(typeof el.showModal==='function')el.showModal();else el.setAttribute('open','');el.focus();}catch(e){el.setAttribute('open','');}return()=>{restore();try{el.close();}catch(e){}};},[]);
  return <dialog ref={root} tabIndex={-1} aria-label="Browser update needed" aria-modal="true" onCancel={e=>e.preventDefault()} className="w-full" style={{position:'fixed',inset:0,width:'100vw',height:'100dvh',maxWidth:'none',maxHeight:'none',margin:0,border:0,padding:'56px 20px',boxSizing:'border-box',background:dark?'#101012':'#fafafa',color:dark?'#E8E9EB':'#202124',fontFamily:FONT}}>
    <div role="alert" style={{maxWidth:420,margin:'0 auto',textAlign:'center'}}><div style={{fontSize:15,fontWeight:600,marginBottom:6}}>RealiSIM needs a newer browser</div><div style={{fontSize:13,lineHeight:1.55}}>Open this link in a current version of Chrome, Edge, Safari or Firefox.</div></div>
  </dialog>;
}

function RsProductLayer({ owner, onManage }) {
  const { theme } = useTheme();
  const layer = React.useRef(null);
  const [shown, setShown] = React.useState(false), [failed, setFailed] = React.useState(false);

  React.useLayoutEffect(() => {
    const el = layer.current; if (!el) return;
    try {
      el.setAttribute('popover', 'manual');
      el.showPopover();
      if (!el.matches(':popover-open')) throw new Error('popover did not open');
      setShown(true);
    } catch (e) { setFailed(true); }
    return () => { try { if (el.matches(':popover-open')) el.hidePopover(); } catch (e) {} };
  }, []);

  React.useEffect(() => { if (!shown) return; try { if (typeof closeChat === 'function') closeChat(); } catch (e) {} }, [shown]);
  React.useLayoutEffect(() => { if (!shown || !layer.current) return; return rsInertOutside(layer.current); }, [shown]);

  React.useEffect(() => {
    if (!shown) return;
    const el = layer.current; if (!el) return;
    const body = document.body, html = document.documentElement;
    const fromProduct = (t) => t === body || t === html || t === document || (t && t.nodeType === 1 && el.contains(t));
    const isolate = (e) => {
      if (!fromProduct(e.target)) return;
      if (e.type === 'keydown' && e.key === 'Escape' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) return;
      e.stopPropagation();
    };
    for (const t of RS_ISOLATED_EVENTS) body.addEventListener(t, isolate, false);
    const stopMenu = (e) => e.stopPropagation();   // native: Mana's item context menu never sees right-clicks inside the product
    el.addEventListener('contextmenu', stopMenu, false);
    const onFocusIn = (e) => {
      const t = e.target;
      if (t && t.nodeType === 1 && el.contains(t)) return;
      const first = rsFocusables(rsTrapScope(el))[0];
      (first || el).focus({ preventScroll: true });
    };
    document.addEventListener('focusin', onFocusIn, true);
    const a = document.activeElement;
    if (!a || !el.contains(a)) el.focus({ preventScroll: true });
    return () => {
      for (const t of RS_ISOLATED_EVENTS) body.removeEventListener(t, isolate, false);
      el.removeEventListener('contextmenu', stopMenu, false);
      document.removeEventListener('focusin', onFocusIn, true);
    };
  }, [shown]);

  const onKeyDown = (e) => {   // React bubble phase: runs after the app's own handlers (its Modal traps Tab itself)
    if (e.key !== 'Tab' || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const el = layer.current; if (!el) return;
    const items = rsFocusables(rsTrapScope(el));
    if (!items.length) { e.preventDefault(); el.focus({ preventScroll: true }); return; }
    const first = items[0], last = items[items.length - 1], act = document.activeElement;
    const i = items.indexOf(act);
    if (e.shiftKey && (i <= 0)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (i === -1 ? false : i === items.length - 1)) { e.preventDefault(); first.focus(); }
    else if (i === -1 && act !== el) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
  };

  if (failed) return <RsUnsupported />;
  const bg = theme === 'dark' ? DARK_C.bg : LIGHT_C.bg;
  return (
    <div ref={layer} tabIndex={-1} data-realisim-product={RS_SHELL_VERSION} aria-label="RealiSIM" onKeyDown={onKeyDown}
      style={{ position: 'fixed', inset: 0, width: '100vw', height: '100dvh', maxWidth: 'none', maxHeight: 'none', margin: 0, padding: 0, border: 0,
        borderRadius: 0, overflow: 'hidden', background: bg, color: 'inherit', outline: 'none', boxSizing: 'border-box',
        '--mana-app-height': '100%', '--mana-top-inset': '0px', '--mana-bottom-inset': '0px', '--mana-bottom-inset-overflow': '0px' }}>
      <style>{'[data-realisim-product]::backdrop{background:transparent}[data-realisim-product] .p2g-root{border:0!important;border-radius:0!important}[data-realisim-product] .rs-manage{opacity:.5;transition:opacity .15s}[data-realisim-product] .rs-manage:hover,[data-realisim-product] .rs-manage:focus-visible{opacity:1}'}</style>
      {shown && <RealiSimCore />}
      {shown && owner && (
        <button type="button" className="rs-manage" onClick={onManage} title="Owner only: open the workspace view to manage sharing" aria-label="Manage (owner only)"
          style={{ position: 'absolute', right: 12, bottom: 12, zIndex: 60, height: 26, padding: '0 10px', borderRadius: 999, border: '1px solid ' + (theme === 'dark' ? DARK_C.line2 : LIGHT_C.line2),
            background: theme === 'dark' ? DARK_C.raised : LIGHT_C.panel, color: theme === 'dark' ? DARK_C.text2 : LIGHT_C.text2, fontFamily: FONT, fontSize: 11.5, cursor: 'pointer' }}>Manage</button>
      )}
    </div>
  );
}

function UserDiv() {
  const owner = rsIsOwner();
  const [manage, setManage] = React.useState(() => owner && rsManageParam());
  const supported = React.useMemo(rsPopoverSupported, []);
  React.useEffect(() => {
    const f = () => setManage(rsIsOwner() && rsManageParam());
    window.addEventListener('popstate', f);
    return () => window.removeEventListener('popstate', f);
  }, []);
  const openManage = React.useCallback(() => {
    if (!rsIsOwner()) return;
    try { const u = new URL(window.location.href); u.searchParams.set('manage', '1'); window.history.pushState({}, '', u.toString()); } catch (e) {}
    setManage(true);
  }, []);
  if (owner && manage) return <RealiSimCore />;
  if (!supported) return <RsUnsupported />;
  return <RsProductLayer owner={owner} onManage={openManage} />;
}

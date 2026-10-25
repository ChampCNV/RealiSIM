"""realisim-item-evidence: public web evidence for physical item specs (dimensions, weight, material).

Text-only input. For each item it calls apify/rag-web-browser (Google search + raw-HTTP fetch), then extracts JSON-LD
Product data and dimension/weight/material mentions deterministically, each with the exact source quote. One dataset
item per input item. No images, no private identifiers. Pairs with a Claude matching step in RealiSIM.
"""
import asyncio
import os
import re
from datetime import timedelta

from apify import Actor

from .extract import candidate_specs, domain, extract_source

RAG = "apify/rag-web-browser"
MAX_ITEMS = 8
REMOVE = ("nav, footer, script:not([type='application/ld+json']), style, noscript, svg, img[src^='data:'], [role='alert'], "
          "[role='banner'], [role='dialog'], [role='alertdialog'], [role='region'][aria-label*='skip' i], [aria-modal='true']")


def clean(s, n):
    s = re.sub(r"(https?://|www\.)\S+", "", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def build_query(it):
    parts = [clean(it.get("label"), 80), clean(it.get("visible_text"), 80), clean(it.get("description"), 120)]
    q = " ".join(p for p in parts if p)
    return (q[:220] + " dimensions weight specifications").strip()


def norm_items(raw):
    out = []
    for i, it in enumerate(raw or []):
        if not isinstance(it, dict):
            continue
        label = clean(it.get("label"), 80)
        if not label:
            continue
        out.append(dict(id=clean(it.get("id") or f"item_{i + 1}", 40), label=label, description=clean(it.get("description"), 200),
                        visible_text=clean(it.get("visible_text"), 120), material_guess=clean(it.get("material_guess"), 40),
                        size_hint_cm=it.get("size_hint_cm") if isinstance(it.get("size_hint_cm"), (dict, list)) else None))
    return out[:MAX_ITEMS]


def _get(run, *names):
    for n in names:
        v = getattr(run, n, None) if not isinstance(run, dict) else run.get(n)
        if v:
            return v
    return None


async def one(it, max_results, sem):
    q = build_query(it)
    rec = dict(id=it["id"], label=it["label"], query=q, status="ok", sources=[], candidate_specs=[], error=None, child_run_id=None)
    async with sem:
        try:
            run = await Actor.call(RAG, run_input=dict(query=q, maxResults=max_results, outputFormats=["markdown", "html"], scrapingTool="raw-http",
                                                       requestTimeoutSecs=30, removeElementsCssSelector=REMOVE),
                                   memory_mbytes=1024, timeout=timedelta(seconds=120))
            if run is None:
                raise RuntimeError("rag-web-browser run did not start")
            rec["child_run_id"] = _get(run, "id")
            status = _get(run, "status")
            ds = _get(run, "default_dataset_id", "defaultDatasetId")
            if not ds:
                raise RuntimeError(f"rag-web-browser returned no dataset (status {status})")
            page = await Actor.apify_client.dataset(ds).list_items(limit=max_results)
            rows = getattr(page, "items", None) or (page.get("items") if isinstance(page, dict) else []) or []
        except Exception as e:  # one item failing must not lose the others
            rec.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
            return rec
    for r in rows[:max_results]:
        meta = r.get("metadata") or {}
        sr = r.get("searchResult") or {}
        url = meta.get("url") or sr.get("url") or (r.get("crawl") or {}).get("loadedUrl")
        if not url or not str(url).startswith(("http://", "https://")):
            continue
        rec["sources"].append(extract_source(url, meta.get("title") or sr.get("title"), r.get("markdown") or r.get("text") or "", r.get("html")))
    rec["candidate_specs"] = candidate_specs(rec["sources"])
    rec["domains"] = sorted({s["domain"] for s in rec["sources"] if s["domain"]})
    if not rec["sources"]:
        rec["status"] = "no_results"
    return rec


async def main():
    async with Actor:
        inp = await Actor.get_input() or {}
        items = norm_items(inp.get("items"))
        n = inp.get("maxResultsPerItem", 3)
        n = max(1, min(5, int(n) if isinstance(n, (int, float)) else 3))
        run_id = os.environ.get("ACTOR_RUN_ID") or os.environ.get("APIFY_ACTOR_RUN_ID")
        if not items:
            await Actor.fail(status_message="items[] with a label is required")
            return
        sem = asyncio.Semaphore(3)
        results = await asyncio.gather(*(one(it, n, sem) for it in items))
        for r in results:
            r["run_id"] = run_id
            await Actor.push_data(r)
        ok = sum(1 for r in results if r["status"] == "ok")
        await Actor.set_status_message(f"{ok}/{len(results)} items with web evidence")

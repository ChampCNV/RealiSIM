"""elastic_index: derived, rebuildable Elasticsearch index for RealiSIM help / history / troubleshooting.

SpacetimeDB is the system of record. Everything in this index is derived from canonical rows (pipeline runs, versions,
exports, workflow drafts, support entries) or from official repository help docs pinned to a commit, and can be dropped
and rebuilt at any time (`rebuild`). Retrieval is lexical BM25 (multi_match over title/body/error text) plus exact
error-fingerprint matching; there is no vector or semantic retrieval in this module.

Security model:
  * Elasticsearch binds 127.0.0.1 only, security enabled; this module uses the least-privilege `realisim_support` user
    (indices realisim-support* only). Credentials are generated locally in elastic/secrets (0600).
  * Every document is sanitised before indexing (credentials, signed/ingress URLs, host paths, internal ports, emails).
  * No user-supplied DSL: queries are built here from a validated (query text, allow-listed filter) tuple.
  * Tenant scoping is an index filter on project_id, AND every returned hit is revalidated against SpacetimeDB by the
    caller (support.py) before it leaves the server.
"""
import hashlib, json, re, threading, time
from pathlib import Path

import requests

HOME = Path("/workspace/photo2grasp/elastic")
ALIAS = "realisim-support"
PREFIX = "realisim-support-v1-"
VERSION_PIN = "9.5.5"
FACET_FIELDS = ("kind", "stage", "error_class", "status")
FILTER_FIELDS = {"kind", "stage", "error_class", "status", "error_fingerprint", "run_kind", "outcome"}
KINDS = ("pipeline_error", "pipeline_run", "asset_version", "scene_version", "simulation_run", "gym_export",
         "workflow_error", "support_case", "resolution", "help_doc")

MAPPING = {
    "dynamic": "strict",
    "properties": {
        "canonical_id": {"type": "keyword"}, "canonical_table": {"type": "keyword"}, "record_id": {"type": "keyword"},
        "kind": {"type": "keyword"}, "visibility": {"type": "keyword"}, "project_id": {"type": "keyword"},
        "stage": {"type": "keyword"}, "run_kind": {"type": "keyword"}, "status": {"type": "keyword"},
        "error_class": {"type": "keyword"}, "error_fingerprint": {"type": "keyword"}, "outcome": {"type": "keyword"},
        "commit": {"type": "keyword"}, "case_id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": "english", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}},
        "body": {"type": "text", "analyzer": "english"},
        "error_text": {"type": "text", "analyzer": "english"},
        "created_at": {"type": "date", "format": "epoch_millis"},
        "indexed_at": {"type": "date", "format": "epoch_millis"},
        "content_hash": {"type": "keyword"}, "is_test": {"type": "boolean"},
        "provenance": {"type": "object", "enabled": False},
    },
}
SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"}


# ── sanitisation (before indexing AND before anything is handed to a model) ──────────────────────────
_PATH = re.compile(r"(?<![\w.])/(?:workspace|tmp|home|root|opt|usr|var|cloudspace|run|proc|etc|srv|mnt|app|data|private)(?:/[^\s\"'\\<>`)\]]*)?")
_RULES = [
    (re.compile(r"https?://[^\s\"'\\]*?/api/sandbox/ingress/[^\s\"'\\]*"), "[redacted-url]"),
    (re.compile(r"https?://[^\s\"'\\]*[?&](?:token|sig|signature|X-Goog-Signature|X-Amz-Signature|X-Amz-Credential|access_token|key)=[^\s\"'\\]*", re.I), "[redacted-signed-url]"),
    (re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[redacted-token]"),
    (re.compile(r"(?i)(bearer\s+)[\w.\-~+/=]{12,}"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(token|api[_-]?key|authorization|secret|password|passwd|credential)s?(\"?'?\s*[:=]\s*\"?'?)[^\s\"',}]{6,}"), r"\1\2[redacted]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_\-]{16,}|\bsk_car_[A-Za-z0-9_]{10,}|\bfk-[A-Za-z0-9_\-]{16,}|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}"), "[redacted]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted-key]"),
    (re.compile(r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d{2,5}\b"), "[internal]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
]


def sanitize(text, limit=None):
    s = str(text or "")
    for rx, rep in _RULES[:2]:          # URLs first, before the path rule eats their tails
        s = rx.sub(rep, s)
    s = _PATH.sub(lambda m: "[path]/" + m.group(0).rstrip("/").rsplit("/", 1)[-1][:80] if m.group(0).count("/") > 1 else "[path]", s)
    for rx, rep in _RULES[2:]:
        s = rx.sub(rep, s)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


# ── error fingerprints ────────────────────────────────────────────────────────
KNOWN = [  # (error_class, regex) for product errors that are not Python exceptions
    ("zerogpu_quota", re.compile(r"ZeroGPU quota", re.I)),
    ("table_not_found", re.compile(r"table_not_found|no reliable table plane|table plane not found", re.I)),
    ("hf_token_missing", re.compile(r"hugging ?face token|HF_TOKEN", re.I)),
    ("upload_invalid", re.compile(r"cannot decode|invalid (?:video|upload)|unsupported (?:codec|container)", re.I)),
    ("cancelled", re.compile(r"^cancelled$", re.I)),
    ("rejected_in_review", re.compile(r"rejected in property review", re.I)),
    ("timeout", re.compile(r"timed? ?out|TimeoutExpired", re.I)),
    ("out_of_memory", re.compile(r"out of memory|OOM|Killed|MemoryError|exit code -9|\b137\b", re.I)),
]
_EXC = re.compile(r"\b((?:[A-Za-z_][\w]*\.)*([A-Z][A-Za-z0-9_]*(?:Error|Exception|Exit|Interrupt|Failure)))\b\s*:?\s*(.*)")


def error_class(text):
    s = str(text or "")
    for name, rx in KNOWN:
        if rx.search(s):
            return name
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    for l in reversed(lines):
        m = _EXC.search(l)
        if m:
            return m.group(2)
    return "error" if s.strip() else ""


def _norm(line):
    l = sanitize(line).lower()
    l = re.sub(r"\[path\]/\S+", "<path>", l)
    l = re.sub(r"'[^']*'|\"[^\"]*\"", "<str>", l)
    l = re.sub(r"\b[0-9a-f]{6,}\b|\b\w+_[0-9a-f]{8,}\b", "<id>", l)
    l = re.sub(r"\d+(?:\.\d+)?", "<n>", l)
    return re.sub(r"\s+", " ", l).strip()[:240]


def fingerprint(text):
    """Stable fingerprint of an error: class + normalised exception line (ids, numbers, paths, strings removed)."""
    s = str(text or "").strip()
    if not s:
        return ""
    cls = error_class(s)
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    key = next((l for l in reversed(lines) if _EXC.search(l)), None)
    if cls in dict(KNOWN):
        key = cls
    key = _norm(key or lines[-1])
    return hashlib.sha1(f"{cls}|{key}".encode()).hexdigest()[:16]


def content_hash(doc):
    d = {k: v for k, v in doc.items() if k not in ("indexed_at", "content_hash")}
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:24]


def doc_id(canonical_id):
    return hashlib.sha256(canonical_id.encode()).hexdigest()[:32]


class IndexUnavailable(Exception):
    pass


class Index:
    def __init__(self, home=HOME, alias=ALIAS, prefix=PREFIX):
        self.home, self.alias, self.prefix = Path(home), alias, prefix
        self._s = threading.local()
        self.last_error = None

    # ── transport ──
    @property
    def base(self):
        return f"http://127.0.0.1:{(self.home / 'port').read_text().strip()}"

    def _sess(self):
        if not hasattr(self._s, "s"):
            c = json.loads((self.home / "secrets" / "support.json").read_text())
            s = requests.Session()
            s.auth = (c["user"], c["password"])
            self._s.s = s
        return self._s.s

    def req(self, method, path, body=None, ok=(200, 201), timeout=20, nd=None):
        try:
            if nd is not None:
                r = self._sess().request(method, self.base + path, data=nd, timeout=timeout,
                                         headers={"Content-Type": "application/x-ndjson"})
            else:
                r = self._sess().request(method, self.base + path, json=body, timeout=timeout)
        except (requests.RequestException, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            raise IndexUnavailable(self.last_error)
        if r.status_code not in ok:
            self.last_error = f"HTTP {r.status_code}: {r.text[:240]}"
            raise IndexUnavailable(self.last_error)
        self.last_error = None
        return r.json() if r.text else {}

    # ── lifecycle ──
    def status(self):
        try:
            root = self.req("GET", "/")
            h = self.req("GET", "/_cluster/health")
            st = dict(connected=True, engine="elasticsearch", version=root["version"]["number"], pinned=VERSION_PIN,
                      cluster=h.get("status"), deployment="local single-node, loopback-only, security enabled",
                      retrieval="BM25 lexical (multi_match) + exact error fingerprints; no semantic/vector retrieval")
            try:
                al = self.req("GET", f"/_alias/{self.alias}")
                idx = next(iter(al))
                c = self.req("GET", f"/{self.alias}/_count")
                agg = self.req("POST", f"/{self.alias}/_search", {"size": 0, "aggs": {"k": {"terms": {"field": "kind", "size": 20}}}})
                st.update(index=idx, documents=c.get("count", 0),
                          by_kind={b["key"]: b["doc_count"] for b in agg["aggregations"]["k"]["buckets"]})
            except IndexUnavailable:
                st.update(index=None, documents=0)
            try:
                ns = self.req("GET", "/_nodes/stats/jvm,process")
                n = next(iter(ns["nodes"].values()))
                st.update(heap_used_mb=round(n["jvm"]["mem"]["heap_used_in_bytes"] / 1048576), heap_max_mb=round(n["jvm"]["mem"]["heap_max_in_bytes"] / 1048576))
            except (IndexUnavailable, KeyError, StopIteration):
                pass
            return st
        except IndexUnavailable as e:
            return dict(connected=False, engine="elasticsearch", pinned=VERSION_PIN, error=str(e))

    def current_index(self):
        try:
            return next(iter(self.req("GET", f"/_alias/{self.alias}")))
        except IndexUnavailable:
            return None

    def _create(self, name):
        self.req("PUT", f"/{name}", {"settings": SETTINGS, "mappings": MAPPING})

    def ensure(self):
        if self.current_index():
            return self.current_index()
        name = self.prefix + time.strftime("%Y%m%d%H%M%S")
        self._create(name)
        self.req("POST", "/_aliases", {"actions": [{"add": {"index": name, "alias": self.alias}}]})
        return name

    # ── writes ──
    def _prep(self, doc):
        d = dict(doc)
        for k in ("title", "body", "error_text"):
            if d.get(k):
                d[k] = sanitize(d[k], 12000)
        d["content_hash"] = content_hash(d)
        d["indexed_at"] = int(time.time() * 1000)
        return d

    def upsert(self, docs, index=None, refresh=False):
        """Idempotent: unchanged documents are reported as noop (update + doc_as_upsert + detect_noop)."""
        target = index or self.alias
        out = dict(created=0, updated=0, noop=0, errors=0)
        docs = list(docs)
        for i in range(0, len(docs), 200):
            lines = []
            for d in docs[i:i + 200]:
                p = self._prep(d)
                body = {k: v for k, v in p.items() if k != "indexed_at"}
                lines.append(json.dumps({"update": {"_index": target, "_id": doc_id(p["canonical_id"])}}))
                # indexed_at only on first insert so that re-indexing identical content is a true noop
                lines.append(json.dumps({"doc": body, "upsert": p, "detect_noop": True}))
            r = self.req("POST", "/_bulk" + ("?refresh=wait_for" if refresh else ""), nd="\n".join(lines) + "\n", timeout=60)
            for it in r.get("items", []):
                u = it.get("update", {})
                if u.get("error"):
                    out["errors"] += 1
                    self.last_error = json.dumps(u["error"])[:300]
                else:
                    out[{"created": "created", "updated": "updated", "noop": "noop"}.get(u.get("result"), "updated")] += 1
        return out

    def delete(self, canonical_ids, refresh=False):
        ids = list(canonical_ids)
        if not ids:
            return 0
        lines = [json.dumps({"delete": {"_index": self.alias, "_id": doc_id(c)}}) for c in ids]
        r = self.req("POST", "/_bulk" + ("?refresh=wait_for" if refresh else ""), nd="\n".join(lines) + "\n", timeout=60)
        return sum(1 for it in r.get("items", []) if it.get("delete", {}).get("result") == "deleted")

    def existing(self, project_ids=None, visibility=None):
        """canonical_id -> content_hash for every document (optionally restricted). search_after paging."""
        flt = []
        if project_ids is not None:
            flt.append({"terms": {"project_id": list(project_ids)}})
        if visibility:
            flt.append({"term": {"visibility": visibility}})
        q = {"size": 1000, "_source": ["canonical_id", "content_hash", "project_id"], "sort": [{"canonical_id": "asc"}],
             "query": {"bool": {"filter": flt}} if flt else {"match_all": {}}}
        out, after = {}, None
        while True:
            if after:
                q["search_after"] = after
            r = self.req("POST", f"/{self.alias}/_search", q)
            hs = r["hits"]["hits"]
            for h in hs:
                out[h["_source"]["canonical_id"]] = (h["_source"].get("content_hash"), h["_source"].get("project_id"))
            if len(hs) < 1000:
                return out
            after = hs[-1]["sort"]

    def refresh(self):
        self.req("POST", f"/{self.alias}/_refresh")

    def rebuild(self, docs):
        """Build a fresh index from canonical sources, verify its count, swap the alias atomically, drop old indices."""
        docs = list(docs)
        name = self.prefix + time.strftime("%Y%m%d%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        self._create(name)
        res = self.upsert(docs, index=name)
        self.req("POST", f"/{name}/_refresh")
        n = self.req("GET", f"/{name}/_count")["count"]
        uniq = len({d["canonical_id"] for d in docs})
        if n != uniq or res["errors"]:
            self.req("DELETE", f"/{name}", ok=(200, 404))
            raise IndexUnavailable(f"rebuild verification failed: {n} != {uniq} documents, {res['errors']} errors")
        old = self.current_index()
        acts = [{"add": {"index": name, "alias": self.alias}}]
        if old:
            acts.insert(0, {"remove": {"index": old, "alias": self.alias}})
        self.req("POST", "/_aliases", {"actions": acts})
        try:
            for ix in self.req("GET", f"/_cat/indices/{self.prefix}*?format=json&h=index"):
                if ix["index"] != name:
                    self.req("DELETE", f"/{ix['index']}", ok=(200, 404))
        except IndexUnavailable:
            pass
        return dict(index=name, previous=old, documents=n, **res)

    # ── search (no caller DSL) ──
    def search(self, query, project_ids, include_public=True, filters=None, size=10, highlight=True, facets=True):
        query = str(query or "").strip()[:500]
        size = max(1, min(int(size or 10), 20))
        scope = [{"terms": {"project_id": sorted(set(project_ids))}}] if project_ids else []
        if include_public:
            scope.append({"term": {"visibility": "public"}})
        if not scope:
            return dict(total=0, hits=[], facets={})
        flt = [{"bool": {"should": scope, "minimum_should_match": 1}}]
        for k, v in (filters or {}).items():
            if k in FILTER_FIELDS and v:
                vals = [str(x)[:80] for x in (v if isinstance(v, list) else [v])][:12]
                flt.append({"terms": {k: vals}})
        rng = {}
        if (filters or {}).get("since_ms"):
            rng["gte"] = int(filters["since_ms"])
        if (filters or {}).get("until_ms"):
            rng["lte"] = int(filters["until_ms"])
        if rng:
            flt.append({"range": {"created_at": rng}})
        if query:
            must = [{"multi_match": {"query": query, "fields": ["title^2", "error_text^1.5", "body"], "type": "best_fields",
                                     "operator": "or", "fuzziness": "AUTO:4,8", "prefix_length": 2, "lenient": True}}]
            should = [{"term": {"error_class": {"value": query, "boost": 3}}}]
        else:
            must, should = [{"match_all": {}}], []
        body = {"size": size, "track_total_hits": True,
                "query": {"bool": {"must": must, "should": should, "filter": flt}},
                "sort": ["_score", {"created_at": {"order": "desc", "missing": "_last"}}],
                "_source": {"excludes": ["body"]}}
        if highlight:
            body["highlight"] = {"fields": {"body": {"fragment_size": 180, "number_of_fragments": 2},
                                            "error_text": {"fragment_size": 180, "number_of_fragments": 1}},
                                 "pre_tags": ["⟦"], "post_tags": ["⟧"], "encoder": "default"}
        if facets:
            body["aggs"] = {f: {"terms": {"field": f, "size": 12}} for f in FACET_FIELDS}
            body["aggs"]["error_fingerprint"] = {"terms": {"field": "error_fingerprint", "size": 8, "min_doc_count": 1}}
            body["aggs"]["created_day"] = {"date_histogram": {"field": "created_at", "calendar_interval": "day", "min_doc_count": 1}}
        r = self.req("POST", f"/{self.alias}/_search", body)
        hits = []
        for h in r["hits"]["hits"]:
            s = h["_source"]
            hl = h.get("highlight") or {}
            s["snippet"] = " … ".join((hl.get("error_text") or []) + (hl.get("body") or []))[:600]
            s["score"] = h.get("_score")
            hits.append(s)
        aggs = r.get("aggregations") or {}
        fac = {k: [dict(value=b["key"], count=b["doc_count"]) for b in v.get("buckets", [])] for k, v in aggs.items() if k != "created_day"}
        if "created_day" in aggs:
            fac["created_day"] = [dict(value=b["key_as_string"][:10], count=b["doc_count"]) for b in aggs["created_day"]["buckets"]][-30:]
        return dict(total=r["hits"]["total"]["value"], hits=hits, facets=fac)

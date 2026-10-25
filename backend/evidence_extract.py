"""Deterministic product-spec extraction for RealiSIM item evidence (stdlib only).

Shared byte-for-byte by the Apify Actor (src/extract.py) and the RealiSIM backend (backend/evidence_extract.py) so the
backend can re-validate every cited quote with the same parser. Units are normalised to centimetres and grams.
Every candidate carries the exact substring ("quote") it was read from.
"""
import html as _html
import json
import re
from urllib.parse import urlparse

LEN_UNITS = {"mm": 0.1, "millimeter": 0.1, "millimeters": 0.1, "millimetre": 0.1, "millimetres": 0.1,
             "cm": 1.0, "centimeter": 1.0, "centimeters": 1.0, "centimetre": 1.0, "centimetres": 1.0,
             "m": 100.0, "meter": 100.0, "meters": 100.0, "metre": 100.0, "metres": 100.0,
             "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54, "”": 2.54, "''": 2.54, "ft": 30.48, "feet": 30.48, "foot": 30.48}
MASS_UNITS = {"mg": 0.001, "g": 1.0, "gram": 1.0, "grams": 1.0, "gr": 1.0, "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0, "kgs": 1000.0,
              "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495, "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592}
UNIT_CODES = {"MMT": ("len", 0.1), "CMT": ("len", 1.0), "MTR": ("len", 100.0), "INH": ("len", 2.54), "FOT": ("len", 30.48),
              "GRM": ("mass", 1.0), "KGM": ("mass", 1000.0), "LBR": ("mass", 453.592), "ONZ": ("mass", 28.3495), "MGM": ("mass", 0.001)}
MATERIALS = ["stainless steel", "cast iron", "carbon steel", "aluminium", "aluminum", "steel", "iron", "copper", "brass", "bronze", "titanium", "zinc", "polypropylene", "polyethylene", "polycarbonate", "polystyrene", "acrylic", "abs plastic", "pvc", "pet plastic", "nylon", "silicone", "rubber",
             "plastic", "resin", "foam", "glass", "borosilicate", "ceramic", "porcelain", "stoneware", "earthenware", "wood", "bamboo", "cardboard",
             "paperboard", "paper", "cork", "leather", "cotton", "felt", "melamine", "tritan", "carbon fiber", "carbon fibre"]
NUM = r"(\d{1,5}(?:[.,]\d{1,3})?)"
LU = r"(mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?|m|met(?:er|re)s?|inch(?:es)?|in|ft|feet|foot|\"|”|'')"
MU = r"(mg|kgs?|kilograms?|grams?|gr|g|ounces?|oz|lbs?|pounds?)"
SEP = r"\s*(?:x|×|\*|by)\s*"
TRIPLE = re.compile(NUM + r"\s*" + LU + r"?" + SEP + NUM + r"\s*" + LU + r"?" + SEP + NUM + r"\s*" + LU + r"(?![a-z])", re.I)
PAIR = re.compile(NUM + r"\s*" + LU + r"?" + SEP + NUM + r"\s*" + LU + r"(?![a-z])", re.I)
LABELED = re.compile(r"\b(length|long|width|wide|height|high|tall|depth|deep|diameter|dia\.?|thickness|thick)\b[^0-9]{0,25}?" + NUM + r"\s*" + LU + r"(?![a-z])", re.I)
LABELED_AFTER = re.compile(NUM + r"\s*" + LU + r"\s*(?:\(\s*[^)]{0,20}\))?\s*(long|wide|high|tall|deep|in diameter|diameter|thick)\b", re.I)
WEIGHT = re.compile(r"\b(weight|weighs|weigh|net\s*wt\.?|net weight|item weight|product weight|shipping weight|mass|wt\.?)\b[^0-9]{0,25}?" + NUM + r"\s*" + MU + r"(?![a-z])", re.I)
WEIGHT_AFTER = re.compile(NUM + r"\s*" + MU + r"\s*\(?\s*(?:net\s*)?(?:weight|wt)\b", re.I)
FLUID = re.compile(r"fl\.?\s*$", re.I)
AXIS = {"length": "length", "long": "length", "width": "width", "wide": "width", "height": "height", "high": "height", "tall": "height",
        "depth": "depth", "deep": "depth", "diameter": "diameter", "dia": "diameter", "dia.": "diameter", "in diameter": "diameter",
        "thickness": "thickness", "thick": "thickness"}


def _f(s):
    return float(str(s).replace(",", "."))


def _r(v, n=2):
    return round(v + 0.0, n)


def _lu(u):
    u = (u or "").lower().strip()
    return LEN_UNITS.get(u)


def _quote(text, start, end, pad=60, limit=220):
    """Exact substring of `text` around a match, clipped to sentence/line boundaries when possible."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    seg = text[a:b]
    lo = max(seg.rfind("\n", 0, start - a), seg.rfind(". ", 0, start - a))
    if lo >= 0:
        a = a + lo + (2 if seg[lo:lo + 2] == ". " else 1)
    seg = text[a:b]
    hi_n = seg.find("\n", end - a)
    if hi_n >= 0:
        b = a + hi_n
    q = text[a:b].strip()
    if len(q) > limit:
        s0 = max(0, start - a - 40)
        q = text[a + s0:min(b, a + s0 + limit)].strip()
    return q


def domain(url):
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def dims_in(text):
    """Dimension mentions: triples (L x W x H), pairs and labelled single dimensions, in cm."""
    out, used = [], []
    for m in TRIPLE.finditer(text):
        tail = _lu(m.group(6))
        f = [_lu(m.group(2)) or tail, _lu(m.group(4)) or tail, tail]
        if None in f:
            continue
        vals = [_r(_f(m.group(i)) * k) for i, k in zip((1, 3, 5), f)]
        if all(0.05 <= v <= 2000 for v in vals):
            out.append(dict(kind="triple", values_cm=vals, raw=m.group(0), quote=_quote(text, m.start(), m.end()), span=[m.start(), m.end()]))
            used.append((m.start(), m.end()))
    for m in PAIR.finditer(text):
        if any(a <= m.start() < b for a, b in used):
            continue
        tail = _lu(m.group(4))
        f = [_lu(m.group(2)) or tail, tail]
        if None in f:
            continue
        vals = [_r(_f(m.group(1)) * f[0]), _r(_f(m.group(3)) * f[1])]
        if all(0.05 <= v <= 2000 for v in vals):
            out.append(dict(kind="pair", values_cm=vals, raw=m.group(0), quote=_quote(text, m.start(), m.end()), span=[m.start(), m.end()]))
            used.append((m.start(), m.end()))
    for rx, ax_i, n_i, u_i in ((LABELED, 1, 2, 3), (LABELED_AFTER, 3, 1, 2)):
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in used):
                continue
            k = _lu(m.group(u_i))
            if k is None:
                continue
            v = _r(_f(m.group(n_i)) * k)
            if 0.05 <= v <= 2000:
                out.append(dict(kind="single", axis=AXIS.get(m.group(ax_i).lower().rstrip("."), m.group(ax_i).lower()), values_cm=[v],
                                raw=m.group(0), quote=_quote(text, m.start(), m.end()), span=[m.start(), m.end()]))
    return out


def weights_in(text):
    out = []
    for rx, n_i, u_i in ((WEIGHT, 2, 3), (WEIGHT_AFTER, 1, 2)):
        for m in rx.finditer(text):
            u = m.group(u_i).lower()
            if u in ("oz", "ounce", "ounces") and FLUID.search(text[max(0, m.start(n_i) - 6):m.start(n_i)] + " " + text[m.end(u_i):m.end(u_i) + 1]):
                continue
            if re.match(r"\s*fl\b", text[m.end(n_i):m.end(n_i) + 6], re.I):
                continue
            k = MASS_UNITS.get(u)
            if k is None:
                continue
            v = _r(_f(m.group(n_i)) * k, 1)
            if 0.1 <= v <= 200000 and not any(abs(o["mass_g"] - v) < 1e-6 and o["span"][0] == m.start() for o in out):
                out.append(dict(mass_g=v, raw=m.group(0), quote=_quote(text, m.start(), m.end()), span=[m.start(), m.end()]))
    return out


def materials_in(text, limit=3):
    low = text.lower()
    found = {}
    for mat in MATERIALS:
        for m in re.finditer(r"(?<![a-z])" + re.escape(mat) + r"(?![a-z])", low):
            if any(mat in other and mat != other and low.find(other, max(0, m.start() - len(other)), m.end() + len(other)) >= 0 for other in found):
                continue
            ctx = low[max(0, m.start() - 40):m.end() + 10]
            score = 3 if re.search(r"material|made (?:of|from)|construction|body", ctx) else 1
            e = found.setdefault(mat, dict(material=mat, count=0, score=0, quote=None, span=None))
            e["count"] += 1
            e["score"] += score
            if e["quote"] is None or score == 3 and e.get("_s", 0) < 3:
                e["quote"], e["span"], e["_s"] = _quote(text, m.start(), m.end()), [m.start(), m.end()], score
    res = sorted(found.values(), key=lambda e: (-e["score"], -e["count"]))[:limit]
    for e in res:
        e.pop("_s", None)
    return res


# ── JSON-LD ─────────────────────────────────────────────────────────────────
LDRE = re.compile(r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def _walk(o):
    if isinstance(o, list):
        for x in o:
            yield from _walk(x)
    elif isinstance(o, dict):
        t = o.get("@type")
        ts = t if isinstance(t, list) else [t]
        if any(str(x).lower() in ("product", "productmodel", "individualproduct") for x in ts):
            yield o
        for k in ("@graph", "mainEntity", "itemOffered", "hasVariant"):
            if k in o:
                yield from _walk(o[k])


def _qv(v, want):
    """QuantitativeValue / string / number -> normalised value (cm or g) or None."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, (int, float)):
        return None                      # unitless: refuse to guess
    if isinstance(v, dict):
        code = str(v.get("unitCode") or "").upper()
        txt = str(v.get("unitText") or "").lower()
        try:
            val = _f(v.get("value"))
        except (TypeError, ValueError):
            return None
        if code in UNIT_CODES and UNIT_CODES[code][0] == want:
            return _r(val * UNIT_CODES[code][1], 2 if want == "len" else 1)
        k = (LEN_UNITS if want == "len" else MASS_UNITS).get(txt)
        return _r(val * k, 2 if want == "len" else 1) if k else None
    if isinstance(v, str):
        m = re.match(r"\s*" + NUM + r"\s*" + (LU if want == "len" else MU) + r"\s*$", v, re.I)
        if m:
            k = (_lu(m.group(2)) if want == "len" else MASS_UNITS.get(m.group(2).lower()))
            return _r(_f(m.group(1)) * k, 2 if want == "len" else 1) if k else None
    return None


def _snippet(key, val):
    return json.dumps({key: val}, ensure_ascii=False)[1:-1]


def jsonld_products(html_text, limit=2):
    out = []
    for blob in LDRE.findall(html_text or ""):
        try:
            data = json.loads(_html.unescape(blob.strip()))
        except Exception:
            continue
        for p in _walk(data):
            brand = p.get("brand")
            if isinstance(brand, list):
                brand = brand[0] if brand else None
            if isinstance(brand, dict):
                brand = brand.get("name")
            keep = {k: p[k] for k in ("name", "brand", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "sku", "mpn", "model", "weight", "width",
                                       "height", "depth", "material", "size") if k in p and p[k] not in (None, "", [], {})}
            if "name" not in keep:
                continue
            text = json.dumps(keep, ensure_ascii=False)
            specs = {}
            for k, want, field in (("weight", "mass", "mass_g"), ("width", "len", "width_cm"), ("height", "len", "height_cm"), ("depth", "len", "length_cm")):
                if k in keep:
                    v = _qv(keep[k], want)
                    if v is not None:
                        specs[field] = dict(value=v, quote=_snippet(k, keep[k]))
            if isinstance(keep.get("material"), str) and keep["material"].strip():
                specs["material"] = dict(value=keep["material"].strip()[:60], quote=_snippet("material", keep["material"]))
            gt = next((str(keep[k]) for k in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin") if keep.get(k)), None)
            out.append(dict(name=str(keep.get("name"))[:200], brand=str(brand)[:80] if brand else None, gtin=gt,
                            sku=str(keep.get("sku"))[:80] if keep.get("sku") else None, mpn=str(keep.get("mpn"))[:80] if keep.get("mpn") else None,
                            specs=specs, text=text[:4000]))
            if len(out) >= limit:
                return out
    return out


def html_title(html_text):
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", re.I | re.S)
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()[:200] if m else None


def _windows(text, spans, head=2500, pad=350, limit=9000):
    """Stored page text: the page head plus windows around every spec mention, so citations survive long pages."""
    iv = sorted([(max(0, a - pad), min(len(text), b + pad)) for a, b in spans] + [(0, min(len(text), head))])
    merged = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out, n = [], 0
    for a, b in merged:
        if n >= limit:
            break
        seg = text[a:min(b, a + limit - n)]
        out.append(seg)
        n += len(seg)
    return "\n…\n".join(out)


def extract_source(url, title, text, html_text=None, text_limit=9000, scan_limit=300000):
    """One fetched page -> evidence record. The whole page (up to scan_limit) is scanned; the stored text keeps the head
    plus windows around each mention. Every kept quote is an exact substring of the stored text."""
    full = (text or "")[:scan_limit]
    prods = jsonld_products(html_text) if html_text else []
    dims, wts, mats = dims_in(full), weights_in(full), materials_in(full)
    stored = _windows(full, [d["span"] for d in dims[:12]] + [w["span"] for w in wts[:8]] + [m["span"] for m in mats if m.get("span")], limit=text_limit)
    keep = lambda xs: [{k: v for k, v in x.items() if k != "span"} for x in xs if x["quote"] in stored]
    return dict(url=url, domain=domain(url), title=(title or html_title(html_text) or "").strip()[:200], text=stored,
                jsonld=prods, jsonld_text="\n".join(p["text"] for p in prods),
                dimensions=keep(dims[:12]), weights=keep(wts[:8]), materials=keep(mats))


def candidate_specs(sources):
    """Flatten sources into cited candidates: [{field|kind, value(s), url, quote, origin}]."""
    c = []
    for s in sources:
        for p in s.get("jsonld") or []:
            for f, v in p["specs"].items():
                c.append(dict(field=f, value=v["value"], url=s["url"], quote=v["quote"], origin="jsonld"))
        for d in s.get("dimensions") or []:
            c.append(dict(field="dimensions", kind=d["kind"], axis=d.get("axis"), values_cm=d["values_cm"], url=s["url"], quote=d["quote"], origin="text"))
        for w in s.get("weights") or []:
            c.append(dict(field="mass_g", value=w["mass_g"], url=s["url"], quote=w["quote"], origin="text"))
        for m in s.get("materials") or []:
            c.append(dict(field="material", value=m["material"], url=s["url"], quote=m["quote"], origin="text"))
    return c[:60]


def norm_ws(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _snippet_obj(q):
    """A JSON-LD citation is one or more `"key": value` members; parse them as an object (empty dict otherwise)."""
    try:
        o = json.loads("{" + q.strip().strip(",") + "}")
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def quote_values(quote, field):
    """All normalised values a quote supports for one field (used to re-validate Claude's citations)."""
    q = str(quote or "")
    if field == "mass_g":
        vals = [w["mass_g"] for w in weights_in(q)]
        v = _qv(_snippet_obj(q).get("weight"), "mass")
        if v is not None:
            vals.append(v)
        # a bare "<n> <mass unit>" inside an explicit quote is acceptable once the quote itself was verified in the page
        for mm in re.finditer(NUM + r"\s*" + MU + r"(?![a-z])", q, re.I):
            k = MASS_UNITS.get(mm.group(2).lower())
            if k and not re.match(r"\s*fl\b", q[mm.end(1):mm.end(1) + 6], re.I):
                vals.append(_r(_f(mm.group(1)) * k, 1))
        return vals
    vals = []
    for d in dims_in(q):
        vals.extend(d["values_cm"])
    obj = _snippet_obj(q)
    for key in ("width", "height", "depth"):
        v = _qv(obj.get(key), "len")
        if v is not None:
            vals.append(v)
    for mm in re.finditer(NUM + r"\s*" + LU + r"(?![a-z])", q, re.I):
        k = _lu(mm.group(2))
        if k:
            vals.append(_r(_f(mm.group(1)) * k))
    return vals

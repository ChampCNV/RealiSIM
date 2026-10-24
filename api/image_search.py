"""Product photos from the web (Google Images through SerpAPI)."""
import io

import httpx
from PIL import Image

from .config import secret

SERPAPI = "https://serpapi.com/search.json"
MIN_SIDE = 300  # px: smaller images add nothing over the video frames
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


def serpapi_key():
    """The key is SERPAPI_API_KEY in the docs; SERPAPI_KEY is the name SerpAPI itself shows."""
    return secret("SERPAPI_API_KEY") or secret("SERPAPI_KEY")


def search_images(query, n=10):
    """Full-size image URLs for `query`, best results first."""
    key = serpapi_key()
    if not key:
        raise RuntimeError("SERPAPI_API_KEY is not set (environment or .env)")
    r = httpx.get(SERPAPI, params=dict(engine="google_images", q=query, api_key=key), timeout=30)
    r.raise_for_status()
    return [x["original"] for x in r.json().get("images_results", []) if x.get("original")][:n]


def download(urls, out_dir, prefix="candidate"):
    """Saves every URL that is a usable image as out_dir/<prefix>_<i>.jpg. Returns [(path, url)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with httpx.Client(follow_redirects=True, timeout=15, headers=HEADERS) as client:
        for url in urls:
            try:
                r = client.get(url)
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content))
                if img.mode in ("RGBA", "LA", "P"):  # transparent product shots: flatten onto white
                    rgba = img.convert("RGBA")
                    img = Image.new("RGB", rgba.size, (255, 255, 255))
                    img.paste(rgba, mask=rgba.split()[3])
                img = img.convert("RGB")
            except Exception as exc:
                print(f"   skipped {url[:80]}: {exc}")
                continue
            if min(img.size) < MIN_SIDE:
                continue
            path = out_dir / f"{prefix}_{len(saved)}.jpg"
            img.save(path, quality=95)
            saved.append((path, url))
    return saved

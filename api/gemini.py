"""Gemini: what each object is, which web photos show the same product, and its real size, mass and friction."""
import json
import re
from typing import Literal

from google import genai
from google.genai import types
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .config import secret

_client = None


def model_name():
    return secret("GEMINI_MODEL") or "gemini-flash-latest"


def client():
    global _client
    if _client is None:
        key = secret("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (environment or .env)")
        _client = genai.Client(api_key=key)
    return _client


def _small(img, side=768):
    img = img.copy()
    img.thumbnail((side, side))
    return img


def view_with_box(view_path, box):
    """The video crop with the object outlined: crops are padded, so neighbouring objects can show up too."""
    img = Image.open(view_path).convert("RGB")
    ImageDraw.Draw(img).rectangle(box, outline=(255, 0, 0), width=max(2, img.width // 150))
    return _small(img)


class FrameBox(BaseModel):
    frame: int = Field(description="index of the frame shown, starting at 0")
    box: list[float] = Field(min_length=4, max_length=4, description="x0, y0, x1, y1 as fractions of the frame, origin top left")


class SceneItem(BaseModel):
    label: str = Field(description="Specific name, e.g. 'Celsius Sparkling Grape Rush can'")
    slug: str = Field(description="snake_case name, e.g. 'celsius_grape_can'")
    brand: str | None = None
    search_query: str = Field(description="Google Images query for clean product photos of this exact item")
    shape: Literal["cylinder", "box", "other"]
    visible_text: str = ""
    confidence: float
    boxes: list[FrameBox]


class SceneList(BaseModel):
    objects: list[SceneItem]


def objects_in_frames(frame_paths):
    """Every distinct object on the table, with a box in each frame that shows it whole."""
    contents = [
        "These frames are from one phone video of a table. List every distinct object standing or lying on "
        "the table. Do not list the table, the cloth, or the background. For each object give a box in every "
        "frame where the whole object is visible, as fractions of the image: [x0, y0, x1, y1], origin at the "
        "top left. `frame` is the index of the image, 0 for the first.",
    ]
    for i, path in enumerate(frame_paths):
        contents += [f"Frame {i}:", _small(Image.open(path).convert("RGB"))]
    resp = client().models.generate_content(
        model=model_name(), contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SceneList))
    for item in resp.parsed.objects:
        item.slug = re.sub(r"[^a-z0-9]+", "_", item.slug.lower()).strip("_") or "object"
    return resp.parsed


class Identity(BaseModel):
    label: str = Field(description="Specific human-readable name, e.g. 'Celsius Sparkling Grape Rush can'")
    slug: str = Field(description="snake_case asset name, e.g. 'celsius_grape_can'")
    brand: str | None = Field(description="Brand if visible or recognisable, else null")
    search_query: str = Field(description="Google Images query that finds clean product photos of this exact item")
    shape: Literal["cylinder", "box", "other"]
    visible_text: str = Field(description="Text readable on the object, verbatim")
    confidence: float = Field(description="0-1: how sure you are of the exact product")


def identify(views):
    """views: [(image_path, object_box)] crops of one object from the video."""
    prompt = (
        "These are crops from a phone video of objects on a table. Every crop shows the SAME object, outlined "
        "with a red rectangle (ignore anything outside it, including other objects). Identify the object as "
        "specifically as you can: brand, product line, flavour/variant, size. If it is a generic or homemade "
        "item, describe it plainly (e.g. 'roll of beige masking tape'). The search query should find clean "
        "product photos of this exact item from several sides."
    )
    contents = [prompt] + [view_with_box(p, box) for p, box in views]
    resp = client().models.generate_content(
        model=model_name(), contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Identity))
    ident = resp.parsed
    ident.slug = re.sub(r"[^a-z0-9]+", "_", ident.slug.lower()).strip("_") or "object"
    return ident


class Picks(BaseModel):
    matches: list[int] = Field(description="Candidate numbers that show the same product, best first")


def pick_references(views, candidates, identity, n=3):
    """Indices into `candidates` (image paths) of web photos that show this exact product, best first."""
    contents = [
        f"The object is: {identity.label}. Video crops of it (object in the red rectangle):",
        *[view_with_box(p, box) for p, box in views],
        "Candidate product photos from the web:",
    ]
    for i, path in enumerate(candidates):
        contents += [f"Candidate {i}:", _small(Image.open(path).convert("RGB"))]
    contents.append(
        f"Pick up to {n} candidates to texture a 3D model of the object in the video. Only pick photos of the "
        "SAME product (same design, colours, flavour/variant and packaging size), showing exactly one item, "
        "whole and unobstructed (no hands, no multipacks, no lifestyle scenes, no other products), sharp and "
        "not a stylised render with a different label. Prefer plain backgrounds and photos from different "
        "sides. Return an empty list if none qualify.")
    resp = client().models.generate_content(
        model=model_name(), contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Picks))
    seen = []
    for i in resp.parsed.matches:
        if 0 <= i < len(candidates) and i not in seen:
            seen.append(i)
    return seen[:n]


class Physics(BaseModel):
    size_m: list[float] = Field(min_length=3, max_length=3, description="[length, width, height] in metres, standing as in the video; length >= width")
    mass_kg: float
    material: str
    friction: float = Field(description="Sliding friction coefficient against a dry wooden/laminate tabletop")
    collision: Literal["cylinder", "box", "mesh"]
    source: str = Field(description="Where each number comes from (spec sheet, product page, or 'estimate: ...')")


PHYSICS_PROMPT = """You are filling in physical properties for a robot simulation (MuJoCo) of a real object.

Object: {label}
Brand: {brand}
Text on it: {visible_text}
{height_line}

Look up the real product (manufacturer spec sheets, packaging standards, retailer listings) and give:
- size_m: [length, width, height] in metres as it stands on the table, length >= width. For a round object
  length = width = diameter. Prefer published dimensions; the scan height is a sanity check.
- mass_kg: total mass as it would be on the table (full and sealed unless it is obviously open or empty).
- material: short description of the outer surface and contents.
- friction: sliding friction coefficient of its surface on a dry wooden tabletop.
- collision: "cylinder" for cans, bottles and jars with straight sides, "box" for boxes, "mesh" otherwise.
- source: say where each number comes from, or that it is an estimate and how it was estimated.

Answer with only a JSON object with exactly these keys: size_m, mass_kg, material, friction, collision, source."""


def _json_block(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model answer: {text[:200]}")
    return json.loads(m.group(0))


def physics(identity, height_m):
    """Real size, mass, material and friction, looked up with Google Search when the model allows it.

    height_m is what the scan measured. None when the video gave no usable measurement.
    """
    if height_m:
        height_line = (f"Height measured from a 3D scan of the video: {height_m * 100:.1f} cm "
                       "(accurate to roughly +-1 cm). Prefer published dimensions; the scan is a sanity check.")
    else:
        height_line = "No measured height is available, so use the published dimensions of the product."
    prompt = PHYSICS_PROMPT.format(label=identity.label, brand=identity.brand or "unknown",
                                   visible_text=identity.visible_text or "none", height_line=height_line)
    try:  # search grounding and a response schema can't be combined, so the grounded answer is parsed by hand
        resp = client().models.generate_content(
            model=model_name(), contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
        result = Physics.model_validate(_json_block(resp.text))
    except Exception as exc:
        print(f"   grounded lookup failed ({exc}); asking without search")
        resp = client().models.generate_content(
            model=model_name(), contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Physics))
        result = resp.parsed
    L, W, H = (sorted(result.size_m[:2], reverse=True) + [result.size_m[2]])
    if result.collision == "cylinder":
        L = W = (L + W) / 2
    source = re.sub(r"-{2,}", "-", result.source)  # it ends up in an XML comment, where "--" is illegal
    return dict(source=source, size_m=[L, W, H], mass_kg=result.mass_kg, material=result.material,
                friction=[result.friction, 0.005, 0.0001], collision=result.collision)

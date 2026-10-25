"""Real-world properties of an object from its photos: Claude identifies it and looks the specs up online.

    props = object_properties("celsius_can", [frame paths])

Order: objects.json (if the object is already there) -> optionally Claude through the Anthropic API
(use_claude=True / make_asset.py --claude: claude-opus-5-5 with web search returns size, mass, material, friction,
sources and confidence) -> otherwise you are asked in the terminal (Claude's guesses, if any, as defaults). Nothing is invented silently: every number keeps a
"source" line, and the result is saved to objects.json.
"""
import base64
import json
from pathlib import Path

import cv2

MODEL = "claude-opus-5-5"
SCHEMA = {
    "type": "object",
    "properties": {
        "identified": {"type": "boolean", "description": "true only if the exact object/product is recognised"},
        "name": {"type": "string", "description": "what the object is, e.g. brand + product + size"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "size_m": {"type": "array", "items": {"type": "number"},
                   "description": "[length, width, height] in metres, height = up when it stands normally"},
        "mass_kg": {"type": "number", "description": "mass as it appears in the photos (e.g. opened can = empty)"},
        "mass_note": {"type": "string", "description": "e.g. 'empty (opened)'; mass if full, if relevant"},
        "material": {"type": "string"},
        "friction": {"type": "number", "description": "sliding friction coefficient on a cloth/wood table"},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs / references used"},
        "question_for_user": {"type": "string",
                              "description": "one short question if something cannot be known from photos "
                                             "(e.g. 'Is the can full or empty?'), else empty"},
    },
    "required": ["identified", "name", "confidence", "size_m", "mass_kg", "mass_note", "material", "friction",
                 "sources", "question_for_user"],
    "additionalProperties": False,
}
PROMPT = """These photos show one object that a small robot arm (SO-100) will pick up in a physics simulation.
Identify it as precisely as you can (brand, product, size variant). Then use web search to find its real
dimensions and mass from reliable sources (manufacturer, retailer, packaging specs). Estimate the sliding
friction of its surface on a cloth or wood table from published values for that material.
Judge from the photos whether it is full, empty or opened when that changes the mass, and say so in mass_note.
Only mark identified=true and confidence=high when you found specs for this exact product. If the object is
custom or unrecognisable, set identified=false, give your best visual estimates, and say so."""


def _image_block(path, max_side=1024):
    img = cv2.imread(str(path))
    s = min(1.0, max_side / max(img.shape[:2]))
    ok, buf = cv2.imencode(".jpg", cv2.resize(img, None, fx=s, fy=s), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.tobytes()).decode()}}


def ask_claude(frames):
    """Claude's answer as a dict (raises on missing credentials / API errors)."""
    import anthropic
    client = anthropic.Anthropic()
    content = [_image_block(f) for f in frames] + [{"type": "text", "text": PROMPT}]
    messages = [{"role": "user", "content": content}]
    for _ in range(5):  # web search runs server-side; pause_turn means "send it back to continue"
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if a safety classifier declines, another model answers instead
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": content}, {"role": "assistant", "content": response.content}]
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _ask(question, default):
    answer = input(f"   {question} [{default}]: ").strip()
    return answer or default


def ask_user(name, guess=None):
    """Terminal questions, with Claude's (or no) guesses as defaults."""
    g = guess or {}
    print(f"   Please give the real properties of '{name}' (Enter keeps the value in brackets).")
    size = g.get("size_m") or [0.05, 0.05, 0.10]
    size_cm = _ask("size in cm: length width height", " ".join(f"{v * 100:.1f}" for v in size))
    mass_g = _ask("weight in grams (as it will be picked up)", f"{g.get('mass_kg', 0.05) * 1000:.0f}")
    material = _ask("material", g.get("material") or "plastic")
    friction = _ask("friction on the table (0.3 slippery ... 0.8 grippy)", f"{g.get('friction', 0.5):.2f}")
    return dict(size_m=[float(v) / 100 for v in size_cm.split()], mass_kg=float(mass_g) / 1000,
                material=material, friction=float(friction))


def object_properties(name, frames, objects_path, use_claude=False):
    """Properties for objects.json: already there, from Claude (use_claude, confident) or from you; saved."""
    objects = json.loads(Path(objects_path).read_text()) if Path(objects_path).exists() else {}
    if name in objects:
        print(f"   '{name}' already in {Path(objects_path).name}: using it")
        return objects[name]
    guess, source = None, "entered by the user"
    if not use_claude:
        props = ask_user(name)
        entry = dict(source=source, size_m=[round(v, 4) for v in props["size_m"]], mass_kg=round(props["mass_kg"], 4),
                     material=props["material"], friction=[float(props["friction"]), 0.005, 0.0001])
        objects[name] = entry
        Path(objects_path).write_text(json.dumps(objects, indent=2))
        return entry
    try:
        print(f"   asking Claude ({MODEL}, web search) what this is and its real specs...")
        guess = ask_claude(frames)
        print(f"   Claude: {guess['name']} | confidence {guess['confidence']} | size {guess['size_m']} m | "
              f"{guess['mass_kg'] * 1000:.0f} g ({guess['mass_note']}) | friction {guess['friction']}")
        for s in guess["sources"][:5]:
            print(f"     source: {s}")
    except Exception as err:  # no credentials, no network, API error: fall back to asking
        print(f"   (Claude not available: {type(err).__name__}: {err}) -> asking you instead")

    if guess and guess["identified"] and guess["confidence"] == "high":
        props = dict(size_m=guess["size_m"], mass_kg=guess["mass_kg"], material=guess["material"],
                     friction=guess["friction"])
        if guess["question_for_user"]:  # e.g. full or empty: only you can tell
            print(f"   Claude asks: {guess['question_for_user']}")
            props["mass_kg"] = float(_ask("weight in grams", f"{props['mass_kg'] * 1000:.0f}")) / 1000
        source = f"Claude ({MODEL}) identified '{guess['name']}': " + "; ".join(guess["sources"])
    else:
        props = ask_user(name, guess)
        if guess:
            source = f"user, with Claude's guess '{guess['name']}' ({guess['confidence']} confidence) as default"
    entry = dict(source=source, size_m=[round(v, 4) for v in props["size_m"]], mass_kg=round(props["mass_kg"], 4),
                 material=props["material"], friction=[float(props["friction"]), 0.005, 0.0001])
    objects[name] = entry
    Path(objects_path).write_text(json.dumps(objects, indent=2))
    return entry

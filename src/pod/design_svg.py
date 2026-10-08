"""
Vector art lane: the LLM writes SVG, we rasterize it at print resolution.

Why this is the backbone of the zero-cash design engine:

  * No image-model bill. Claude / Gemini write SVG as text; that is covered
    by a subscription or a free tier. gpt-image-1 at $0.167/image was the
    single biggest line item in the paid plan.
  * No DPI problem. A diffusion model gives you 1024 px and you have to
    super-resolve it to ~3240 px for a 4500x5400 DTG file. Vectors render at
    ANY size, razor sharp, so the Real-ESRGAN step disappears.
  * Transparent by construction. No white-backdrop knockout, no halos.
  * It fits the product. The POD designs that sell are flat, bold, 2-6
    colours, silhouette-driven: badges, line icons, retro sunsets, mountain
    silhouettes, paw prints, EKG lines, coffee cups. That IS vector art.
    What vector can't do (painterly, photoreal) is also what prints worst on
    DTG, so you lose little.

Safety: the SVG is untrusted model output. We parse it, reject anything that
is not pure geometry (no <text>, <image>, <script>, <foreignObject>, no
external hrefs, no CSS @import), clamp the size, and rasterize in-process.
Text is NEVER allowed in the SVG: lettering is typeset by Pillow from the
screened phrase so the IP gate sees exactly what gets printed.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger("pod.design_svg")

SVG_NS = "http://www.w3.org/2000/svg"
FORBIDDEN_TAGS = {
    "text", "tspan", "textPath", "image", "script", "foreignObject",
    "use", "a", "animate", "animateTransform", "set", "iframe", "video",
    "audio", "style",
}
ALLOWED_TAGS = {
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline",
    "polygon", "defs", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "title", "desc",
}
MAX_SVG_BYTES = 200_000
MAX_ELEMENTS = 1500


class SVGError(Exception):
    pass


@dataclass
class SVGArt:
    svg: str
    width_units: float
    height_units: float
    elements: int


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def sanitize_svg(raw: str) -> SVGArt:
    """Validate model-written SVG; raise SVGError on anything suspicious."""
    s = raw.strip()
    s = re.sub(r"^```(?:svg|xml)?\s*|\s*```$", "", s, flags=re.M).strip()
    m = re.search(r"<svg\b.*?</svg>", s, flags=re.S | re.I)
    if not m:
        raise SVGError("no <svg> element in response")
    s = m.group(0)
    if len(s.encode()) > MAX_SVG_BYTES:
        raise SVGError(f"svg too large ({len(s)} bytes)")
    if re.search(r"(?i)(href\s*=\s*[\"']\s*(https?:|data:|//))|@import|url\(\s*[\"']?\s*https?:", s):
        raise SVGError("external reference in svg")
    if re.search(r"(?i)<!ENTITY|<!DOCTYPE", s):
        raise SVGError("entities/doctype not allowed")

    try:
        root = ET.fromstring(s)
    except ET.ParseError as exc:
        raise SVGError(f"svg does not parse: {exc}") from exc
    if _strip_ns(root.tag) != "svg":
        raise SVGError("root is not <svg>")

    count = 0
    for el in root.iter():
        count += 1
        tag = _strip_ns(el.tag)
        if tag in FORBIDDEN_TAGS:
            raise SVGError(f"forbidden element <{tag}>")
        if tag not in ALLOWED_TAGS:
            raise SVGError(f"unexpected element <{tag}>")
        for k in el.attrib:
            kl = k.lower()
            if kl.startswith("on") or kl in ("style",) and "url(" in el.attrib[k]:
                raise SVGError(f"forbidden attribute {k}")
    if count > MAX_ELEMENTS:
        raise SVGError(f"too many elements ({count})")

    vb = root.attrib.get("viewBox")
    if vb:
        parts = [float(p) for p in re.split(r"[\s,]+", vb.strip())]
        if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
            raise SVGError("bad viewBox")
        w, h = parts[2], parts[3]
    else:
        try:
            w = float(re.sub(r"[a-z%]+$", "", root.attrib.get("width", "1000")))
            h = float(re.sub(r"[a-z%]+$", "", root.attrib.get("height", "1000")))
        except ValueError as exc:
            raise SVGError("no usable viewBox/width/height") from exc
        root.set("viewBox", f"0 0 {w} {h}")
        s = ET.tostring(root, encoding="unicode")

    # Force a transparent canvas: strip any full-bleed background rect the
    # model added "for context". A white box prints as a white box.
    s = _drop_background_rects(s, w, h)
    return SVGArt(svg=s, width_units=w, height_units=h, elements=count)


def _drop_background_rects(svg: str, w: float, h: float) -> str:
    def _is_bg(mo: re.Match) -> bool:
        attrs = mo.group(0)
        def num(name: str, default: float) -> float:
            mm = re.search(rf'\b{name}="([\d.]+)%?"', attrs)
            return float(mm.group(1)) if mm else default
        rw, rh = num("width", 0), num("height", 0)
        x, y = num("x", 0), num("y", 0)
        return x <= 1 and y <= 1 and rw >= w * 0.95 and rh >= h * 0.95
    return re.sub(r"<rect\b[^>]*/>", lambda mo: "" if _is_bg(mo) else mo.group(0), svg)


def rasterize(art: SVGArt, target_width_px: int) -> bytes:
    """SVG -> transparent PNG bytes at the requested pixel width."""
    try:
        import cairosvg  # pure Python + libcairo (present on Ubuntu runners)
    except ImportError as exc:  # pragma: no cover
        raise SVGError("pip install cairosvg (needs libcairo2)") from exc
    scale = target_width_px / art.width_units
    png = cairosvg.svg2png(
        bytestring=art.svg.encode(),
        output_width=target_width_px,
        output_height=int(round(art.height_units * scale)),
        background_color=None,
    )
    # Sanity: must have real alpha and real ink.
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    alpha = img.getchannel("A")
    lo, hi = alpha.getextrema()
    if hi == 0:
        raise SVGError("rendered svg is fully transparent")
    if lo == 255:
        raise SVGError("rendered svg has no transparency (background rect?)")
    bbox = img.getbbox()
    if bbox and (bbox[2] - bbox[0]) < target_width_px * 0.25:
        raise SVGError("rendered art is too small/narrow for a chest print")
    return png


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

SVG_SYSTEM = (
    "You are a vector illustrator producing print-ready apparel graphics as SVG. "
    "Output ONLY a single <svg> element. Rules (violations are discarded by a "
    "parser, so follow them literally): viewBox=\"0 0 1000 1000\"; use only path, "
    "circle, ellipse, rect, line, polyline, polygon, g, defs, linearGradient, "
    "radialGradient, stop, clipPath, mask; NO text/tspan elements of any kind, no "
    "letters drawn as paths, no numbers, no <image>, no <use>, no <style>, no "
    "scripts, no external references, no background rectangle (transparent "
    "canvas), no gradients unless asked (DTG prints flat colour best). 2-6 flat "
    "colours. Bold silhouettes and shapes readable from 3 metres. Subject centred, "
    "occupying roughly 70-85% of the canvas width, with clear space top and bottom "
    "for lettering that will be added separately. Keep under 150 KB."
)


def svg_prompt(subject: str, style_desc: str, palette: str, garment: str = "black t-shirt") -> str:
    pal = f" Palette: {palette}." if palette else ""
    return (
        f"Subject: {subject}. Art direction: {style_desc}.{pal} It will be printed on a "
        f"{garment}, so avoid pure black fills on the main subject and prefer "
        f"cream (#F5F0E6), warm oranges, teals and golds that pop on dark fabric. "
        f"Return the SVG now."
    )


def generate_svg_art(subject: str, style_desc: str, palette: str, target_width_px: int,
                     attempts: int = 3) -> tuple[bytes, str]:
    """Ask the LLM chain for SVG, sanitize, rasterize. Returns (png_bytes, svg)."""
    from . import llm
    last = ""
    for i in range(attempts):
        res = llm.complete(svg_prompt(subject, style_desc, palette), SVG_SYSTEM)
        try:
            art = sanitize_svg(res.text)
            png = rasterize(art, target_width_px)
            log.info("svg art ok via %s: %d elements, %d bytes png",
                     res.provider, art.elements, len(png))
            return png, art.svg
        except SVGError as exc:
            last = str(exc)
            log.warning("svg attempt %d/%d rejected: %s", i + 1, attempts, exc)
    raise SVGError(f"no valid svg after {attempts} attempts: {last}")


# A bundled sample so DRY_RUN exercises the real vector path (sanitize ->
# rasterize at print width) without any LLM call.
DEMO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">
<defs><clipPath id="sun"><circle cx="500" cy="430" r="300"/></clipPath></defs>
<g clip-path="url(#sun)">
<rect x="200" y="130" width="600" height="80" fill="#F5F0E6"/><rect x="200" y="230" width="600" height="80" fill="#F2A541"/>
<rect x="200" y="330" width="600" height="80" fill="#E8743B"/><rect x="200" y="430" width="600" height="80" fill="#C84B31"/>
<rect x="200" y="530" width="600" height="80" fill="#2A9D8F"/><rect x="200" y="630" width="600" height="100" fill="#1B4965"/></g>
<polygon points="150,740 330,520 430,640 520,480 700,740" fill="#1B4965"/>
<polygon points="520,740 640,600 760,700 850,560 950,740" fill="#2A9D8F"/>
<path d="M330 760 h340 a20 20 0 0 1 20 20 v20 a20 20 0 0 1 -20 20 h-340 a20 20 0 0 1 -20 -20 v-20 a20 20 0 0 1 20 -20z" fill="#F5F0E6"/>
</svg>"""


def demo_art(target_width_px: int) -> bytes:
    return rasterize(sanitize_svg(DEMO_SVG), target_width_px)

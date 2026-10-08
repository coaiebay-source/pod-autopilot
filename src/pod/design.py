"""
Design generation.

THE PROBLEM EVERY POD AUTOMATION TUTORIAL GETS WRONG:

If you ask an image model for "a t-shirt design that says 'STILL RUNNING ON
CAFFEINE'", you will get a beautiful illustration wrapped around text that
reads "STILL RUNNIG ON CAFFEINNE" or "STlLL RUNN1NG ON CAFFElNE". Diffusion
models render glyph shapes, not characters. At 1 design in 20 you get lucky.
At 40 designs a day you are shipping garbage and eating returns.

THE FIX -- separate art from typography:

    1. Image model generates ART ONLY. Prompt explicitly forbids text, letters,
       words, and typography.
    2. The phrase is rendered by code (Pillow + a properly licensed font) at
       full print resolution, where every character is exact by construction.
    3. Composite art + type onto the print-file canvas at the spec dimensions.

This is deterministic where it needs to be and generative where it should be.
It also makes the design *editable* -- you can A/B the phrase without
regenerating art, and you can retarget the same art to a mug or poster by
changing the layout, not the model.

Output: 4500x5400 px, 300 DPI, transparent, sRGB PNG, under 200 MB.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .config import CREDS, DRY_RUN, FONT_DIR, IMAGE_MODEL, MAX_PRINT_FILE_MB, PRINT_SPECS, PrintSpec

log = logging.getLogger("pod.design")

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Pillow is required: pip install pillow\n"
        "It does the typography compositing that makes designs printable."
    ) from exc


# How art is produced. "svg" (default, zero-cash) | "none" (type only) |
# "cloudflare" (FLUX schnell, free tier) | "openai" (gpt-image-1, paid).
ART_PROVIDER = os.environ.get("ART_PROVIDER", "svg").strip().lower()


class DesignError(Exception):
    pass


# Super-resolution upscaler command for generated art (see upscale_art()).
# e.g. "realesrgan-ncnn-vulkan -i {in} -o {out} -s 4 -n realesrgan-x4plus"
UPSCALER_CMD = os.environ.get("UPSCALER_CMD", "")


# ---------------------------------------------------------------------------
# Style library
# ---------------------------------------------------------------------------
# Fixed visual languages. Constraining style is what makes a store look like a
# BRAND instead of 500 unrelated AI images. Pick 3-5 and rotate within them.
STYLES: dict[str, str] = {
    "retro_sunset": (
        "1970s retro sunset gradient with horizontal stripe bands, warm orange "
        "magenta and mustard palette, distressed halftone texture, grainy vintage "
        "screen print feel"
    ),
    "vintage_national_park": (
        "WPA national park poster style, flat vector shapes, limited earth-tone "
        "palette, bold simplified landscape silhouette, 1930s screenprint"
    ),
    "bootleg_90s": (
        "1990s rap bootleg tee aesthetic, high contrast, chrome gradient accents, "
        "dramatic lighting, collage composition, heavy black background areas"
    ),
    "minimal_line": (
        "single continuous thin line drawing, minimalist, monochrome, generous "
        "negative space, elegant understated illustration"
    ),
    "sticker_bomb": (
        "bold die-cut sticker illustration, thick clean outlines, flat saturated "
        "colors, playful cartoon shapes, no gradients"
    ),
    "engraved": (
        "antique woodcut engraving, fine hatching and crosshatch lines, "
        "monochrome etching, Victorian scientific illustration style"
    ),
    "vaporwave": (
        "vaporwave aesthetic, neon pink and cyan, grid horizon, chrome text-free "
        "geometry, glitch accents, 1980s digital surrealism"
    ),
}

# Words that must never appear in the art prompt. Enforced by string check on
# the final prompt before it is sent -- do not rely on the model obeying.
NO_TEXT_CLAUSE = (
    "Absolutely no text, no letters, no words, no numbers, no captions, no "
    "signage, no typography, no logos, no watermarks, no writing of any kind. "
    "Pure graphic artwork only."
)


@dataclass
class DesignBrief:
    concept: str
    phrase: str                    # exact text to render -- screened upstream
    subphrase: str = ""            # optional smaller second line
    niche: str = ""
    style: str = "retro_sunset"
    art_subject: str = ""          # what the illustration depicts, text-free
    palette: str = ""
    layout: str = "arch_top"       # see LAYOUTS
    product_type: str = "tshirt"
    audience_language: list[str] = field(default_factory=list)

    def art_prompt(self) -> str:
        style_desc = STYLES.get(self.style, STYLES["retro_sunset"])
        subject = self.art_subject or self.concept
        palette = f", color palette: {self.palette}" if self.palette else ""
        return (
            f"{subject}. Art direction: {style_desc}{palette}. "
            f"Composition centered, balanced, suitable for placement on the "
            f"upper chest area of a garment, with clear space above and below "
            f"for separate lettering. Transparent-background-ready subject on a "
            f"flat solid white backdrop. {NO_TEXT_CLAUSE}"
        )


LAYOUTS = {
    # name: (art_y_center_fraction, type_y_fraction, type_size_fraction)
    "arch_top": (0.30, 0.68, 0.085),
    "type_top_art_bottom": (0.62, 0.22, 0.095),
    "art_only": (0.45, None, 0.0),
    "stacked": (0.52, 0.20, 0.075),
}


# ---------------------------------------------------------------------------
# Art generation
# ---------------------------------------------------------------------------

def generate_art(brief: DesignBrief, size: str = "1024x1024") -> bytes:
    """
    Call an image model for ART ONLY. Returns PNG bytes.

    Supported: OpenAI images API (gpt-image-1), or any OpenAI-compatible
    gateway via OPENAI_BASE_URL. Swap in Flux/SDXL/Imagen by changing
    IMAGE_MODEL and, if needed, the endpoint path.

    Post-processing that matters for DTG:
      * Remove the white backdrop -> true alpha. DTG prints on colored
        garments; a white box around your art prints as a white box.
      * Knock out near-white to transparent, keep anti-aliased edges.
    """
    prompt = brief.art_prompt()
    if any(w in prompt.lower() for w in ("text", "letters")) and NO_TEXT_CLAUSE not in prompt:
        raise DesignError("Safety clause missing from art prompt -- refusing to send")

    if DRY_RUN:
        log.info("[DRY_RUN] generate_art: %s", prompt[:200])
        if ART_PROVIDER == "svg":
            # Exercise the real vector path (sanitize + rasterize at print width).
            from . import design_svg
            spec = PRINT_SPECS.get(f"{brief.product_type}_front", PRINT_SPECS["tshirt_front"])
            return design_svg.demo_art(int(spec.width * 0.72))
        if ART_PROVIDER == "none":
            return _transparent_art()
        return _placeholder_art(size)

    # ---- ZERO-CASH LANES ------------------------------------------------
    # ART_PROVIDER selects how art is made. Default "svg": the LLM writes
    # vector art (subscription/free-tier text tokens), rasterized at full
    # print resolution -- no image-model bill, no upscaler. See design_svg.py.
    provider = ART_PROVIDER
    if provider == "none":
        # Typography-only design. The biggest POD category by volume. Returns a
        # fully transparent art layer; compose() renders just the lettering.
        return _transparent_art()
    if provider == "svg":
        from . import design_svg
        style_desc = STYLES.get(brief.style, STYLES["retro_sunset"])
        target_w = int(PRINT_SPECS.get(f"{brief.product_type}_front", PRINT_SPECS["tshirt_front"]).width * 0.72)
        try:
            png, svg = design_svg.generate_svg_art(
                brief.art_subject or brief.concept, style_desc, brief.palette, target_w)
        except design_svg.SVGError as exc:
            raise DesignError(f"svg lane failed: {exc}") from exc
        _save_svg_sidecar(svg, brief)
        return png
    if provider == "cloudflare":
        from . import imagegen_cf
        try:
            return imagegen_cf.generate(prompt)
        except imagegen_cf.CFImageError as exc:
            raise DesignError(str(exc)) from exc
    if provider != "openai":
        raise DesignError(f"unknown ART_PROVIDER {provider!r} (svg|none|cloudflare|openai)")

    key = CREDS.openai_key
    if not key:
        raise DesignError("OPENAI_API_KEY not set")

    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    resp = requests.post(
        f"{base}/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "background": "transparent" if IMAGE_MODEL.startswith("gpt-image") else None,
            "response_format": "b64_json" if not IMAGE_MODEL.startswith("gpt-image") else None,
        },
        timeout=300,
    )
    if resp.status_code >= 400:
        raise DesignError(f"Image API {resp.status_code}: {resp.text[:400]}")
    data = resp.json()["data"][0]
    raw = data.get("b64_json")
    if raw:
        return base64.b64decode(raw)
    url = data.get("url")
    if url:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.content
    raise DesignError("Image API returned neither b64 nor url")


def upscale_art(png_bytes: bytes, factor: int = 4) -> tuple[bytes, bool]:
    """
    Generated art arrives at 1024px wide. The print canvas wants the art layer
    at ~3240px wide (72% of 4500). A naive 3.2x LANCZOS upscale of a diffusion
    image is SOFT at garment viewing distance -- effective ~95 DPI, below
    Printful's own 150 DPI quality floor for apparel.

    Fix: super-resolve 4x with Real-ESRGAN (free, local, single binary), then
    let compose downsample 4096 -> 3240, which is a detail-preserving
    direction. Illustrative/flat styles tolerate upscaling well; fine hatching
    (the 'engraved' style) tolerates it least -- if you sell that style,
    inspect samples at 100% zoom before enabling volume.

    Install (Linux, CPU or GPU):
      wget https://github.com/nihui/realesrgan-ncnn-vulkan/releases/latest/download/realesrgan-ncnn-vulkan-ubuntu.zip
      unzip realesrgan-ncnn-vulkan-ubuntu.zip && chmod +x realesrgan-ncnn-vulkan
    Then:
      UPSCALER_CMD="realesrgan-ncnn-vulkan -i {in} -o {out} -s 4 -n realesrgan-x4plus"

    {in} and {out} are substituted with temp paths. If UPSCALER_CMD is unset or
    fails, the original bytes are returned unchanged and compose() records a
    warning -- degraded is better than blocked, but you must see the warning.
    """
    if not UPSCALER_CMD:
        return png_bytes, False
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as td:
        fin = os.path.join(td, "in.png")
        fout = os.path.join(td, "out.png")
        with open(fin, "wb") as fh:
            fh.write(png_bytes)
        cmd = UPSCALER_CMD.format(**{"in": fin, "out": fout})
        try:
            subprocess.run(cmd.split(), check=True, capture_output=True, timeout=600)
            with open(fout, "rb") as fh:
                return fh.read(), True
        except Exception as exc:  # noqa: BLE001
            log.warning("Upscaler failed (%s); proceeding without super-resolution", exc)
            return png_bytes, False


def knockout_white(png_bytes: bytes, threshold: int = 238, feather: int = 2) -> Image.Image:
    """
    Convert a white-backdrop render into transparent alpha.

    Naive "make white pixels transparent" leaves a hard halo. This uses a
    luminance-derived alpha ramp so anti-aliased edge pixels become
    semi-transparent, which is what makes the composite look printed rather
    than pasted.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    r, g, b, a = img.split()
    # Minimum channel: high only where all three are near white.
    from PIL import ImageChops
    minc = ImageChops.darker(ImageChops.darker(r, g), b)
    # Map [threshold, 255] -> alpha [255, 0]
    alpha = minc.point(
        lambda p: 0 if p >= 255 else (255 if p < threshold else int(255 * (255 - p) / (255 - threshold)))
    )
    if feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    img.putalpha(alpha)
    return img


def _placeholder_art(size: str) -> bytes:
    w, h = (int(x) for x in size.split("x"))
    img = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    # A simple retro-sun stand-in so the pipeline is testable with no API key.
    cx, cy, rad = w // 2, int(h * 0.42), int(w * 0.30)
    bands = [(255, 94, 58), (255, 138, 61), (255, 183, 77), (240, 98, 146), (186, 104, 200)]
    for i, color in enumerate(bands):
        y0 = cy - rad + int(2 * rad * i / len(bands))
        y1 = cy - rad + int(2 * rad * (i + 1) / len(bands))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=255)
        band = Image.new("RGBA", (w, h), color + (255,))
        band_mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(band_mask).rectangle([0, y0, w, y1], fill=255)
        from PIL import ImageChops
        combined = ImageChops.multiply(mask, band_mask)
        img.paste(band, (0, 0), combined)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

def _transparent_art(w: int = 1024, h: int = 1024) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _save_svg_sidecar(svg: str, brief: DesignBrief) -> None:
    """Keep the vector source next to the artifacts: it is the master file.
    Re-rendering a sold design for a mug or poster is a 1-line resize."""
    try:
        out = Path("./artifacts")
        out.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (brief.concept or "art").lower())[:40]
        (out / f"{slug}_{uuid.uuid4().hex[:6]}.svg").write_text(svg)
    except OSError as exc:  # noqa: BLE001
        log.warning("could not save svg sidecar: %s", exc)


def _art_to_rgba(png_bytes: bytes) -> Image.Image:
    """Art from the SVG/none lanes already carries real alpha; running the
    white-knockout on it would turn transparent pixels black. Only knock out
    white when the layer is opaque (diffusion output on a white backdrop)."""
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode in ("RGBA", "LA") and img.getchannel("A").getextrema()[0] < 255:
        return img.convert("RGBA")
    return knockout_white(png_bytes)


def find_font(weight: str = "bold") -> str:
    """
    Resolve a font path. THE LICENSING WARNING IS NOT BOILERPLATE:

    A font installed on your OS is licensed for YOUR use, not for embedding in
    merchandise you sell. Shipping a shirt typeset in a retail-licensed font is
    a real, if rarely litigated against small sellers, infringement. Use an
    SIL Open Font License face (free for commercial embedding) or buy a
    commercial/webfont license that explicitly permits merchandise.

    Good OFL families that look commercial: Anton, Bebas Neue, Alfa Slab One,
    Oswald, Montserrat, Archivo Black, Righteous, Bungee, Lobster.
    Drop the .ttf/.otf files in FONT_DIR.
    """
    candidates = {
        "bold": ["Anton-Regular.ttf", "ArchivoBlack-Regular.ttf", "AlfaSlabOne-Regular.ttf", "BebasNeue-Regular.ttf"],
        "condensed": ["Oswald-Bold.ttf", "RobotoCondensed-Bold.ttf"],
        "script": ["Lobster-Regular.ttf", "Pacifico-Regular.ttf"],
        "display": ["Bungee-Regular.ttf", "Righteous-Regular.ttf"],
    }.get(weight, ["Anton-Regular.ttf"])

    for name in candidates:
        p = FONT_DIR / name
        if p.exists():
            return str(p)
    # Fall back to any ttf/otf present, then to Pillow's default (which is a
    # bitmap font and looks terrible at print size -- this is a last resort
    # that should be loud).
    for ext in ("*.ttf", "*.otf"):
        found = sorted(FONT_DIR.glob(ext))
        if found:
            log.warning("Using unlisted font %s -- verify its commercial license", found[0])
            return str(found[0])
    log.error(
        "No font found in %s. Falling back to Pillow default bitmap font, which "
        "WILL look broken at 300 DPI. Download an OFL face (Anton, Bebas Neue) "
        "into that directory.", FONT_DIR,
    )
    return ""


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_width: int,
    start_size: int,
    min_size: int = 40,
) -> ImageFont.FreeTypeFont:
    """Binary-search the largest point size that fits max_width."""
    lo, hi = min_size, start_size
    best = ImageFont.truetype(font_path, min_size) if font_path else ImageFont.load_default()
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(font_path, mid) if font_path else ImageFont.load_default()
        except OSError:
            return best
        w = draw.textlength(text, font=font)
        if w <= max_width:
            best = font
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def arch_text(
    canvas: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont,
    center: tuple[int, int],
    radius: int,
    fill: tuple[int, int, int, int],
    stroke: int = 0,
    stroke_fill: tuple[int, int, int, int] = (0, 0, 0, 255),
) -> None:
    """
    Draw text along an upward arc -- the classic vintage-tee look.

    Pillow has no text-on-path, so each glyph is rendered individually and
    rotated to its tangent angle. Letter spacing is angular so the arc reads
    evenly regardless of glyph width.
    """
    cx, cy = center
    chars = [c for c in text]
    # Estimate angular span from total advance width.
    tmp = ImageDraw.Draw(canvas)
    widths = [tmp.textlength(c, font=font) for c in chars]
    total = sum(widths)
    if total == 0:
        return
    span = min(3.0, total / max(radius, 1))
    start = -span / 2
    angle_cursor = start
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for ch, w in zip(chars, widths):
        if ch == " ":
            angle_cursor += (w / total) * span
            continue
        adv = (w / total) * span
        a = angle_cursor + adv / 2
        # Render glyph upright on its own tile, then rotate.
        pad = int(font.size * 0.6)
        gw, gh = int(w) + pad * 2, int(font.size * 1.8) + pad
        tile = Image.new("RGBA", (max(gw, 1), max(gh, 1)), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.text((pad, pad // 2), ch, font=font, fill=fill,
                stroke_width=stroke, stroke_fill=stroke_fill)
        rot = tile.rotate(-math_degrees(a), resample=Image.BICUBIC, expand=True)
        px = cx + radius * math_sin(a) - rot.width / 2
        py = cy - radius * math_cos(a) - rot.height / 2
        layer.alpha_composite(rot, (int(px), int(py)))
        angle_cursor += adv
    canvas.alpha_composite(layer)


def math_degrees(rad: float) -> float:
    import math
    return math.degrees(rad)


def math_sin(rad: float) -> float:
    import math
    return math.sin(rad)


def math_cos(rad: float) -> float:
    import math
    return math.cos(rad)


# ---------------------------------------------------------------------------
# Composite -> print file
# ---------------------------------------------------------------------------

@dataclass
class DesignOutput:
    png_path: Path
    width: int
    height: int
    dpi: int
    size_mb: float
    art_only: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.size_mb <= MAX_PRINT_FILE_MB
            and self.width >= 1000
            and self.height >= 1000
            and not self.warnings
        )


def compose_print_file(
    brief: DesignBrief,
    art_png: bytes,
    spec: PrintSpec | None = None,
    out_dir: Path | None = None,
    ink_color: tuple[int, int, int] = (250, 246, 235),
    outline: int = 6,
) -> DesignOutput:
    """
    Build the final print file: transparent PNG at spec dimensions, art
    composited with code-rendered typography.

    ink_color default is a warm off-white -- pure #FFFFFF on a black tee reads
    as slightly blue and cheap. Cream reads as intentional vintage.
    """
    spec = spec or PRINT_SPECS.get(f"{brief.product_type}_front", PRINT_SPECS["tshirt_front"])
    W, H = spec.width, spec.height
    warnings: list[str] = []

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    art = _art_to_rgba(art_png)

    layout = LAYOUTS.get(brief.layout, LAYOUTS["arch_top"])
    art_cy_frac, type_y_frac, type_frac = layout

    # -- place art ---------------------------------------------------------
    art_target_w = int(W * 0.72)
    art_is_empty = art.getbbox() is None
    if art_is_empty:
        pass  # typography-only lane: nothing to place or warn about
    elif art.width < art_target_w * 0.9:
        warnings.append(
            f"art_upscaled_{art_target_w / art.width:.1f}x_without_superres:"
            f"set UPSCALER_CMD (Real-ESRGAN) or the print will be soft"
        )
    if not art_is_empty:
        scale = art_target_w / art.width
        art_resized = art.resize(
            (art_target_w, max(1, int(art.height * scale))), Image.LANCZOS
        )
        ax = (W - art_resized.width) // 2
        ay = int(H * art_cy_frac) - art_resized.height // 2
        canvas.alpha_composite(art_resized, (ax, max(0, ay)))

    # -- place type --------------------------------------------------------
    if type_y_frac is not None and brief.phrase:
        font_path = find_font("bold")
        if not font_path:
            warnings.append("no_licensed_font_found")
        draw = ImageDraw.Draw(canvas)
        max_w = int(W * 0.86)
        start_size = int(H * type_frac)
        phrase_upper = brief.phrase.upper()

        # Arc for short phrases, straight for long ones -- arcing a long line
        # pushes the ends off-canvas.
        if len(phrase_upper) <= 24 and brief.layout == "arch_top":
            font = fit_text(draw, phrase_upper, font_path, int(W * 0.62), start_size)
            arch_text(
                canvas,
                phrase_upper,
                font,
                center=(W // 2, int(H * (type_y_frac + 0.22))),
                radius=int(H * 0.30),
                fill=ink_color + (255,),
                stroke=outline,
                stroke_fill=(24, 20, 18, 255),
            )
        else:
            font = fit_text(draw, phrase_upper, font_path, max_w, start_size)
            w = draw.textlength(phrase_upper, font=font)
            ty = int(H * type_y_frac)
            if outline:
                draw.text(
                    ((W - w) / 2, ty), phrase_upper, font=font,
                    fill=ink_color + (255,), stroke_width=outline,
                    stroke_fill=(24, 20, 18, 255),
                )
            else:
                draw.text(((W - w) / 2, ty), phrase_upper, font=font, fill=ink_color + (255,))

            if brief.subphrase:
                sub = brief.subphrase.upper()
                sub_font = fit_text(draw, sub, font_path, int(W * 0.6), int(start_size * 0.45))
                sw = draw.textlength(sub, font=sub_font)
                draw.text(
                    ((W - sw) / 2, ty + int(font.size * 1.5)),
                    sub, font=sub_font, fill=ink_color + (230,),
                )

    # -- validate ----------------------------------------------------------
    bbox = canvas.getbbox()
    if bbox is None:
        raise DesignError("Composed canvas is fully transparent -- nothing rendered")
    ink_w = bbox[2] - bbox[0]
    ink_h = bbox[3] - bbox[1]
    if ink_w < W * 0.30:
        warnings.append(f"ink_coverage_narrow:{ink_w/W:.0%}")
    if ink_h > H * 0.92:
        warnings.append(f"ink_too_tall:{ink_h/H:.0%} -- may exceed printable area")

    out_dir = out_dir or Path("./artifacts")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{brief.product_type}_{uuid.uuid4().hex[:10]}.png"
    # dpi=(300,300) embeds the DPI in metadata. Printful warns on files whose
    # metadata says 72 DPI even when pixel dimensions are correct.
    canvas.save(path, format="PNG", dpi=(spec.dpi, spec.dpi), optimize=True)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_PRINT_FILE_MB:
        warnings.append(f"oversize:{size_mb:.1f}MB")

    return DesignOutput(
        png_path=path, width=W, height=H, dpi=spec.dpi,
        size_mb=round(size_mb, 2), art_only=(type_y_frac is None),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Hosting -- Printful fetches by URL, it cannot take a stream
# ---------------------------------------------------------------------------

def upload_asset(path: Path, public_prefix: str | None = None) -> str:
    """
    Publish the print file somewhere Printful can GET it. Options:
      * Cloudflare R2 with a public bucket + custom domain (cheapest, fastest)
      * S3 + CloudFront
      * Any static host

    Set ASSET_BASE_URL to the public root and ASSET_DIR to the local mount if
    you sync via rclone. For a real deployment, use the R2/S3 SDK here rather
    than assuming a sync -- an unattended pipeline that silently fails to
    publish produces designs that never become products.
    """
    if DRY_RUN:
        return f"{CREDS.asset_base_url.rstrip('/')}/dryrun/{path.name}"
    if os.environ.get("ASSET_PROVIDER", "github_release").lower() == "github_release" \
            and os.environ.get("ASSETS_GH_REPO"):
        # Zero-cash default: public GitHub release asset (see assets_github.py).
        from . import assets_github
        try:
            return assets_github.upload(path)
        except assets_github.AssetError as exc:
            raise DesignError(str(exc)) from exc
    if not CREDS.asset_base_url:
        raise DesignError("ASSET_BASE_URL not set -- Printful needs a public URL")

    # Default implementation: the asset dir is already served (mounted volume
    # behind nginx, or rclone-synced to R2). Verify the file is where we expect.
    if not path.exists():
        raise DesignError(f"Asset {path} does not exist")
    return f"{CREDS.asset_base_url.rstrip('/')}/{path.name}"


# ---------------------------------------------------------------------------
# Top-level: brief -> print file -> public URL
# ---------------------------------------------------------------------------

def build_design(brief: DesignBrief, spec_key: str | None = None) -> tuple[DesignOutput, str]:
    spec = PRINT_SPECS.get(spec_key or f"{brief.product_type}_front")
    art = generate_art(brief)
    art, upscaled = upscale_art(art)
    if upscaled:
        log.info("Art super-resolved 4x before compositing")
    out = compose_print_file(brief, art, spec=spec)
    url = upload_asset(out.png_path)
    log.info(
        "Built design %s (%dx%d, %.2fMB, warnings=%s) -> %s",
        out.png_path.name, out.width, out.height, out.size_mb, out.warnings, url,
    )
    return out, url

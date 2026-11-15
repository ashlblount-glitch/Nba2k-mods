#!/usr/bin/env python3
"""NBA 2K27 custom shoe texture builder.

Builds the texture set for a custom sneaker colorway (diffuse/albedo, normal,
and specular/roughness maps) as PNG and DDS, plus a side-profile preview.

Two modes:

  generate  Paint a complete texture atlas from a colorway JSON using the
            built-in region template (see README "Template layout").

  recolor   Recolor a shoe texture you exported from the game with a modding
            tool. You supply the exported texture and a region mask (each
            region painted a flat key color, see MASK_COLORS) and the colorway
            is applied while keeping the original shading and detail.

  preview   Only render the side-profile preview image.

Examples:
  python tools/shoe_mod.py generate colorways/midnight_volt.json
  python tools/shoe_mod.py generate colorways/*.json --size 1024
  python tools/shoe_mod.py recolor colorways/sunset_fade.json \
      --texture exported/shoe_color.png --mask exported/shoe_mask.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

REGIONS = [
    "upper", "toe", "heel", "collar", "tongue",
    "laces", "midsole", "outsole", "accent", "logo",
]

# Normalised (x0, y0, x1, y1) rectangles of each region in the template atlas.
TEMPLATE_LAYOUT = {
    "upper":   (0.00, 0.00, 0.50, 0.40),
    "toe":     (0.50, 0.00, 0.75, 0.25),
    "heel":    (0.75, 0.00, 1.00, 0.25),
    "collar":  (0.50, 0.25, 0.75, 0.40),
    "tongue":  (0.75, 0.25, 1.00, 0.40),
    "accent":  (0.00, 0.40, 0.50, 0.60),
    "laces":   (0.50, 0.40, 0.75, 0.60),
    "logo":    (0.75, 0.40, 1.00, 0.60),
    "midsole": (0.00, 0.60, 1.00, 0.80),
    "outsole": (0.00, 0.80, 1.00, 1.00),
}

# Key colors for the region mask used by `recolor`.
MASK_COLORS = {
    "upper":   (255, 0, 0),
    "toe":     (0, 255, 0),
    "heel":    (0, 0, 255),
    "collar":  (255, 255, 0),
    "tongue":  (255, 0, 255),
    "laces":   (0, 255, 255),
    "midsole": (255, 128, 0),
    "outsole": (128, 0, 255),
    "accent":  (0, 128, 255),
    "logo":    (255, 255, 255),
}

# Material -> (roughness 0..1, metallic-ish spec strength 0..1)
MATERIALS = {
    "knit":    (0.85, 0.10),
    "mesh":    (0.80, 0.10),
    "leather": (0.55, 0.30),
    "suede":   (0.95, 0.05),
    "patent":  (0.10, 0.90),
    "foam":    (0.70, 0.15),
    "rubber":  (0.75, 0.20),
    "metallic": (0.25, 1.00),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def hex_to_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"bad hex color: #{value}")
    return np.array([int(value[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def load_colorway(path: Path) -> dict:
    data = json.loads(path.read_text())
    missing = [r for r in REGIONS if r not in data.get("regions", {})]
    if missing:
        raise ValueError(f"{path}: colorway missing regions: {', '.join(missing)}")
    for name, spec in data["regions"].items():
        if name not in REGIONS:
            raise ValueError(f"{path}: unknown region '{name}'")
        hex_to_rgb(spec["color"])
        if spec.get("gradient"):
            hex_to_rgb(spec["gradient"])
        if spec.get("material", "leather") not in MATERIALS:
            raise ValueError(f"{path}: region '{name}' has unknown material "
                             f"'{spec.get('material')}'")
    data.setdefault("id", path.stem)
    data.setdefault("name", path.stem.replace("_", " ").title())
    return data


def material_height(material: str, h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    """Return a 0..1 height field giving each material its surface texture."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    if material == "knit":
        field = 0.5 + 0.5 * np.sin((x + y) * 0.9) * np.sin((x - y) * 0.25)
    elif material == "mesh":
        field = ((np.sin(x * 0.6) * np.sin(y * 0.6)) > 0.2).astype(np.float32)
    elif material == "rubber":  # herringbone traction pattern
        band = (y // 24).astype(int) % 2
        diag = np.where(band == 0, x + y, x - y)
        field = (np.sin(diag * 0.35) > 0).astype(np.float32)
    elif material in ("leather", "suede"):
        noise = rng.random((h, w)).astype(np.float32)
        radius = 1.5 if material == "leather" else 0.8
        img = Image.fromarray((noise * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(radius))
        field = np.asarray(img, dtype=np.float32) / 255.0
    elif material == "foam":
        field = 0.5 + 0.08 * rng.standard_normal((h, w)).astype(np.float32)
    else:  # patent / metallic: smooth
        field = np.full((h, w), 0.5, dtype=np.float32)
    return np.clip(field, 0.0, 1.0)


def height_to_normal(height: np.ndarray, strength: float = 2.0) -> np.ndarray:
    dy, dx = np.gradient(height)
    nx, ny, nz = -dx * strength, -dy * strength, np.ones_like(height)
    length = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2)
    normal = np.stack([nx, ny, nz], axis=-1) / length[..., None]
    return ((normal * 0.5 + 0.5) * 255).astype(np.uint8)


def region_albedo(spec: dict, height: np.ndarray, h: int, w: int) -> np.ndarray:
    base = hex_to_rgb(spec["color"])
    if spec.get("gradient"):
        t = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]
        color = base * (1 - t) + hex_to_rgb(spec["gradient"]) * t
        color = np.broadcast_to(color, (h, w, 3))
    else:
        color = np.broadcast_to(base, (h, w, 3))
    shade = 1.0 + 0.3 * (height[..., None] - 0.5)
    return np.clip(color * shade, 0, 255)


def save_texture(img: Image.Image, out_base: Path) -> list[str]:
    """Save PNG + DDS (BC3/DXT5 when supported, otherwise uncompressed)."""
    written = []
    png = out_base.with_suffix(".png")
    img.save(png)
    written.append(png.name)
    dds = out_base.with_suffix(".dds")
    rgba = img.convert("RGBA")
    try:
        rgba.save(dds, pixel_format="DXT5")
    except Exception:
        rgba.save(dds)
    written.append(dds.name)
    return written


# --------------------------------------------------------------------------- #
# Texture builders
# --------------------------------------------------------------------------- #
def build_generate(colorway: dict, size: int, seed: int):
    rng = np.random.default_rng(seed)
    albedo = np.zeros((size, size, 3), dtype=np.float32)
    height = np.zeros((size, size), dtype=np.float32)
    spec_map = np.zeros((size, size, 3), dtype=np.float32)

    for region, (x0, y0, x1, y1) in TEMPLATE_LAYOUT.items():
        spec = colorway["regions"][region]
        material = spec.get("material", "leather")
        px0, py0 = int(x0 * size), int(y0 * size)
        px1, py1 = int(x1 * size), int(y1 * size)
        h, w = py1 - py0, px1 - px0
        hf = material_height(material, h, w, rng)
        height[py0:py1, px0:px1] = hf
        albedo[py0:py1, px0:px1] = region_albedo(spec, hf, h, w)
        rough, metal = MATERIALS[material]
        # Spec map channels: R = specular strength, G = gloss (1-roughness), B = height
        spec_map[py0:py1, px0:px1, 0] = metal * 255
        spec_map[py0:py1, px0:px1, 1] = (1 - rough) * 255
        spec_map[py0:py1, px0:px1, 2] = hf * 255

    # Stitching seams along region borders for a more realistic look.
    img = Image.fromarray(albedo.astype(np.uint8))
    draw = ImageDraw.Draw(img)
    dash = max(4, size // 128)
    for x0, y0, x1, y1 in TEMPLATE_LAYOUT.values():
        rect = [int(x0 * size) + dash, int(y0 * size) + dash,
                int(x1 * size) - dash, int(y1 * size) - dash]
        for i in range(rect[0], rect[2], dash * 2):
            draw.line([(i, rect[1]), (i + dash, rect[1])], fill=(235, 235, 235), width=2)
            draw.line([(i, rect[3]), (i + dash, rect[3])], fill=(235, 235, 235), width=2)

    normal = Image.fromarray(height_to_normal(height))
    spec_img = Image.fromarray(spec_map.astype(np.uint8))
    return img, normal, spec_img


def build_recolor(colorway: dict, texture_path: Path, mask_path: Path, seed: int):
    rng = np.random.default_rng(seed)
    tex = Image.open(texture_path).convert("RGB")
    mask = Image.open(mask_path).convert("RGB").resize(tex.size, Image.NEAREST)
    tex_a = np.asarray(tex, dtype=np.float32)
    mask_a = np.asarray(mask, dtype=np.int16)
    h, w = tex_a.shape[:2]

    # Luminance of the original keeps all baked shading/detail.
    lum = (tex_a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)) / 255.0
    out = tex_a.copy()
    height = lum.copy()
    spec_map = np.zeros((h, w, 3), dtype=np.float32)

    matched_any = False
    for region, key in MASK_COLORS.items():
        sel = np.all(np.abs(mask_a - np.array(key)) <= 24, axis=-1)
        if not sel.any():
            continue
        matched_any = True
        spec = colorway["regions"][region]
        material = spec.get("material", "leather")
        tint = region_albedo(spec, np.full((h, w), 0.5, np.float32), h, w)
        # Re-center luminance so the target color is hit at mid-grey.
        shade = np.clip(lum / max(float(lum[sel].mean()), 1e-3), 0.3, 1.8)
        out[sel] = np.clip(tint[sel] * shade[sel][:, None], 0, 255)
        hf = material_height(material, h, w, rng)
        height[sel] = 0.5 * lum[sel] + 0.5 * hf[sel]
        rough, metal = MATERIALS[material]
        spec_map[sel, 0] = metal * 255
        spec_map[sel, 1] = (1 - rough) * 255
        spec_map[sel, 2] = height[sel] * 255

    if not matched_any:
        raise ValueError("mask contains none of the region key colors "
                         "(see MASK_COLORS / README)")
    return (Image.fromarray(out.astype(np.uint8)),
            Image.fromarray(height_to_normal(height)),
            Image.fromarray(spec_map.astype(np.uint8)))


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def render_preview(colorway: dict, width: int = 1200) -> Image.Image:
    """Side-profile sneaker illustration colored by region."""
    s = width / 1200
    img = Image.new("RGB", (width, int(620 * s)), (244, 244, 246))
    d = ImageDraw.Draw(img)

    def c(region):
        return tuple(int(v) for v in hex_to_rgb(colorway["regions"][region]["color"]))

    def P(points):
        return [(x * s, y * s) for x, y in points]

    d.ellipse(P([(140, 500), (1090, 560)]), fill=(222, 222, 226))  # shadow
    d.polygon(P([(120, 470), (1110, 470), (1100, 520), (150, 525)]), fill=c("outsole"))
    d.polygon(P([(110, 400), (1110, 410), (1115, 472), (118, 472)]), fill=c("midsole"))
    upper = [(130, 400), (150, 250), (230, 170), (330, 150), (420, 175),
             (560, 250), (760, 300), (960, 330), (1080, 360), (1105, 410)]
    d.polygon(P(upper), fill=c("upper"))
    d.polygon(P([(850, 318), (960, 330), (1080, 360), (1105, 410), (850, 410)]),
              fill=c("toe"))
    d.polygon(P([(130, 400), (150, 250), (230, 175), (260, 400)]), fill=c("heel"))
    d.polygon(P([(230, 170), (330, 150), (420, 175), (400, 205), (320, 190), (245, 205)]),
              fill=c("collar"))
    d.polygon(P([(420, 175), (470, 110), (540, 120), (560, 250)]), fill=c("tongue"))
    # Original lightning-stripe side accent.
    d.polygon(P([(300, 380), (520, 260), (500, 320), (760, 300), (480, 400), (510, 345)]),
              fill=c("accent"))
    for i in range(5):  # laces
        x = 470 + i * 70
        d.line(P([(x, 195 + i * 22), (x + 60, 235 + i * 22)]), fill=c("laces"),
               width=max(2, int(12 * s)))
    d.ellipse(P([(170, 300), (220, 350)]), fill=c("logo"))  # heel badge
    for i in range(14):  # midsole stitching
        x = 150 + i * 68
        d.line(P([(x, 436), (x + 30, 436)]), fill=(250, 250, 250), width=max(1, int(3 * s)))

    d.text((30 * s, 24 * s), colorway["name"], fill=(30, 30, 34))
    d.text((30 * s, 44 * s), f"id: {colorway['id']}", fill=(110, 110, 118))
    return img


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def write_outputs(colorway: dict, maps, out_root: Path, size_note: str) -> Path:
    out_dir = out_root / colorway["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for suffix, img in zip(("color", "normal", "spec"), maps):
        files += save_texture(img, out_dir / f"{colorway['id']}_{suffix}")
    render_preview(colorway).save(out_dir / "preview.png")
    files.append("preview.png")
    manifest = {
        "game": "NBA 2K27",
        "type": "shoe",
        "id": colorway["id"],
        "name": colorway["name"],
        "brand": colorway.get("brand", "Custom"),
        "texture_size": size_note,
        "files": files,
        "spec_channels": {"R": "specular", "G": "gloss", "B": "height"},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="build textures from the built-in template")
    g.add_argument("colorways", nargs="+", type=Path)
    g.add_argument("--size", type=int, default=2048, choices=[512, 1024, 2048, 4096])

    r = sub.add_parser("recolor", help="recolor an exported game texture with a mask")
    r.add_argument("colorway", type=Path)
    r.add_argument("--texture", type=Path, required=True)
    r.add_argument("--mask", type=Path, required=True)

    p = sub.add_parser("preview", help="render preview images only")
    p.add_argument("colorways", nargs="+", type=Path)

    for parser in (g, r, p):
        parser.add_argument("--out", type=Path, default=Path("build"))
        parser.add_argument("--seed", type=int, default=27)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "generate":
            for path in args.colorways:
                cw = load_colorway(path)
                maps = build_generate(cw, args.size, args.seed)
                out = write_outputs(cw, maps, args.out, f"{args.size}x{args.size}")
                print(f"[ok] {cw['name']:<20} -> {out}")
        elif args.cmd == "recolor":
            cw = load_colorway(args.colorway)
            maps = build_recolor(cw, args.texture, args.mask, args.seed)
            out = write_outputs(cw, maps, args.out, "{}x{}".format(*maps[0].size))
            print(f"[ok] {cw['name']:<20} -> {out}")
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            for path in args.colorways:
                cw = load_colorway(path)
                dest = args.out / f"{cw['id']}_preview.png"
                render_preview(cw).save(dest)
                print(f"[ok] {cw['name']:<20} -> {dest}")
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

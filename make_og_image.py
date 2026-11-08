#!/usr/bin/env python3
"""Render static/og.png, the 1200x630 link-preview image for the site's home page:
a few posters on the brick wall under a black title band, in the site's style.

    uv run --with pillow make_og_image.py --font /path/to/LibreFranklin[wght].ttf
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H = 1200, 630
# Recognizable posters, one per slot, in reading order (poster id prefixes of thumbs/).
POSTERS = ["133433", "132950", "52437", "131827", "9718"]


def thumb(prefix):
    return next((ROOT / "thumbs").glob(f"{prefix}-*"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", required=True, help="Libre Franklin variable font (OFL), set to weight 900")
    args = ap.parse_args()

    img = Image.new("RGB", (W, H), "white")
    # The wall is a palette PNG with transparency: flatten it onto white.
    wall_rgba = Image.open(ROOT / "static" / "wall.png").convert("RGBA")
    wall = Image.new("RGB", wall_rgba.size, "white")
    wall.paste(wall_rgba, (0, 0), wall_rgba)
    for y in range(0, H, wall.height):
        img.paste(wall, (0, y))

    # Title band.
    band_h = 150
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, band_h], fill="black")
    title = ImageFont.truetype(args.font, 88)
    title.set_variation_by_axes([900])
    sub = ImageFont.truetype(args.font, 30)
    sub.set_variation_by_axes([500])
    d.text((60, 22), "POSTERARCHIEF", font=title, fill="white")
    d.text((64, 112), "Doorzoek ruim 7.900 Loesje-posters, ook op aantal woorden", font=sub, fill=(190, 205, 211))

    # Posters, slightly tilted like on the site.
    pw, gap = 196, 34
    ph = round(pw * 2560 / 1803)
    top = band_h + (H - band_h - ph) // 2
    x0 = (W - (len(POSTERS) * pw + (len(POSTERS) - 1) * gap)) // 2
    for i, (prefix, tilt) in enumerate(zip(POSTERS, [-1.6, 1.1, -0.7, 1.5, -1.2])):
        p = Image.open(thumb(prefix)).convert("RGB")
        p = p.resize((pw, round(p.height * pw / p.width)), Image.LANCZOS)
        framed = Image.new("RGB", (p.width + 6, p.height + 6), "black")
        framed.paste(p, (3, 3))
        rot = framed.rotate(tilt, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255, 0))
        mask = framed.convert("L").point(lambda _: 255).rotate(tilt, expand=True, resample=Image.BICUBIC)
        img.paste(rot, (x0 + i * (pw + gap), top), mask)

    out = ROOT / "static" / "og.png"
    img.save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

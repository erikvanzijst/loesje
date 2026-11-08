#!/usr/bin/env python3
"""Make 500px-wide JPEG thumbnails of images/ into thumbs/ for the search site.

    uv run --with pillow thumbs.py
"""

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC, DST = ROOT / "images", ROOT / "thumbs"
WIDTH = 500


def thumb(src: Path):
    dst = DST / src.name
    if dst.exists():
        return None
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > WIDTH:
                im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
            im.save(dst, "JPEG", quality=82, optimize=True, progressive=True)
    except Exception as e:  # keep going; the site falls back to the full image
        return f"{src.name}: {e}"
    return None


def main():
    DST.mkdir(exist_ok=True)
    files = sorted(p for p in SRC.iterdir() if p.is_file())
    with ProcessPoolExecutor() as ex:
        errors = [e for e in ex.map(thumb, files, chunksize=32) if e]
    for e in errors:
        print("FAILED", e, file=sys.stderr)
    print(f"{len(files) - len(errors)} thumbnails in {DST.name}/, {len(errors)} failed")


if __name__ == "__main__":
    main()

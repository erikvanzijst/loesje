#!/usr/bin/env python3
"""Stage 2: OCR the slogan text of every poster into posters.text.

    uv run --with pillow ocr.py                # all posters without text yet
    uv run --with pillow ocr.py --redo         # redo everything
    uv run --with pillow ocr.py --ids 8729 131827 --show   # debug a few

Loesje posters set the slogan in large, bold type, nearly always uppercase
(the 1980s posters used mixed case). Everything else is smaller, lowercase or
handwritten: the "Loesje" signature, credits such as "de opa van", the web
address and email, and "POSTBUS 1045 6801 BA ARNHEM". So after OCR we drop
footer lines, the signature, lines much smaller than the slogan, and (on
uppercase posters) any non-uppercase words.

Some posters are set sideways, and some have lines at an angle. If the
straight reading is poor, we also try the image rotated (90°, 270°, 180°,
±8°, ±15°) and keep the reading with the best confidence score.
"""

import argparse
import io
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "loesje.db"
IMG_DIR = ROOT / "images"

TARGET_WIDTH = 1400        # normalize resolution: old scans are 480px wide, new ones 1800+
GOOD_ENOUGH = 85           # mean confidence above which we don't try other rotations
ROTATIONS = [90, 270, 8, -8, 15, -15, 180]  # order matters: see ocr_image

NOISE_RE = re.compile(r"@|www|\.nl\b|\.com\b|https?:", re.I)
# Footer lines: the old postal address, giro number, website and email.
FOOTER_RE = re.compile(r"postbus|arnhem|\b6801\b|\b1045\b|\bgiro\b|loesje\.nl|@", re.I)
# Credits on (mostly older) posters: "het broertje van", "neef Karel van", "de opa van".
CREDIT_RE = re.compile(r"^(?:\S+\s+){0,3}van$")
# The handwritten "Loesje" signature as Tesseract tends to misread it.
SIGNATURE_RE = re.compile(r"^[LlI1][oO0][eEcC]?[sSkKgG]?[jJ]?[eEc]?$|^OCe$")
# Uppercase-looking misreads of the signature, which can come out with high confidence.
SIGNATURE_CAPS_RE = re.compile(r"^(?:OCO[/J]?E?|LOEYE|LOE/E)$")


def tesseract_tsv(img: Image.Image, psm: int):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "nld", "--psm", str(psm), "tsv"],
        input=buf.getvalue(), capture_output=True, check=True,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},  # we parallelize across images instead
    ).stdout.decode("utf-8", "replace")
    words = []
    for line in out.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        words.append({
            "block": int(f[2]), "par": int(f[3]), "line": int(f[4]),
            "left": int(f[6]), "top": int(f[7]), "width": int(f[8]), "height": int(f[9]),
            "conf": float(f[10]), "text": f[11].strip(),
        })
    return words


def letters_upper_ratio(t):
    letters = [c for c in t if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 1.0


def to_upper(t):
    """Uppercase an OCR'd word from an all-caps poster; a lowercase l there is an I."""
    return t.replace("l", "I").upper()


def group_lines(words):
    lines, key = [], None
    for w in words:  # TSV order is reading order
        k = (w["block"], w["par"], w["line"])
        if k != key:
            lines.append([])
            key = k
        lines[-1].append(w)
    return lines


def filter_words(words):
    """Keep the slogan; drop signature, credits, footer and OCR junk. Returns lines of words."""
    words = [w for w in words if any(c.isalnum() for c in w["text"]) and not NOISE_RE.search(w["text"])]
    # The signature is big handwriting, usually read with low confidence.
    words = [w for w in words if not (letters_upper_ratio(w["text"]) < 0.75
                                      and (w["conf"] < 50 or SIGNATURE_RE.match(w["text"])))]
    words = [w for w in words if not SIGNATURE_CAPS_RE.match(w["text"])]
    lines, footer_lines = [], []
    for ln in group_lines(words):
        ln = [w for w in ln if w["conf"] >= 30]
        if not ln:
            continue
        # Footer lines (address, giro, web/email) are dropped below, except for any
        # slogan-sized words Tesseract put on the same line ("VROUWENWERK" above the address).
        (footer_lines if FOOTER_RE.search(" ".join(w["text"] for w in ln)) else lines).append(ln)
    if not lines and not footer_lines:
        return []
    if not lines:
        lines, footer_lines = footer_lines, []

    # Size: compare each line's height with the slogan's (tall end of confident lines).
    height = lambda ln: max(w["height"] for w in ln)
    conf_lines = [ln for ln in lines if statistics.mean(w["conf"] for w in ln) >= 60] or lines
    hs = sorted(height(ln) for ln in conf_lines)
    ref = hs[int(len(hs) * 0.75)] if len(hs) > 1 else hs[0]
    lines = [ln for ln in lines if height(ln) >= 0.6 * ref]
    for ln in footer_lines:
        big = [w for w in ln if w["height"] >= 0.75 * ref and not FOOTER_RE.search(w["text"])]
        if big:
            lines.append(big)
    lines.sort(key=lambda ln: min(w["top"] for w in ln))

    # Case: most posters set the slogan in capitals; then anything else is a credit.
    all_text = "".join(w["text"] for ln in lines for w in ln)
    if letters_upper_ratio(all_text) >= 0.5:  # mixed-case 1980s posters are ~20% capitals
        # Tesseract sometimes reads a capital as lowercase ("Is", "ALs", "El" for "EI"),
        # so only judge case on words clearly smaller than the slogan, and normalize the rest.
        # Measured: credits are <= 0.7x the median capital-word height, misread slogan words >= 0.8x.
        caps = [w["height"] for ln in lines for w in ln
                if letters_upper_ratio(w["text"]) >= 0.75 and w["conf"] >= 60]
        cap_h = statistics.median(caps) if caps else ref
        lines = [[dict(w, text=to_upper(w["text"])) for w in ln
                  if letters_upper_ratio(w["text"]) >= 0.75 or w["height"] >= 0.75 * cap_h] for ln in lines]
        # The handwritten signature is taller than the capitals and read unsurely ("OCO/E" at
        # 1.4x, conf 66), whereas oversized slogan words are read confidently ("KUSSENGEVECHT"
        # at 2x, conf 87).
        lines = [[w for w in ln if w["height"] <= 1.25 * cap_h or w["conf"] >= 75] for ln in lines]
    else:  # mixed-case poster (mostly 1980s): drop "het broertje van"-style credits
        lines = [ln for ln in lines
                 if not (CREDIT_RE.match(" ".join(w["text"] for w in ln)) and ln[0]["text"][:1].islower())]

    # Junk: lines with hardly any letters read with low confidence ("Ê", "GN", "W/ NOP,").
    alnum = lambda ln: sum(c.isalnum() for w in ln for c in w["text"])
    lines = [ln for ln in lines if ln and not (
        alnum(ln) <= 3 and statistics.mean(w["conf"] for w in ln) < 85)]
    return lines


def score(lines):
    """Confidence-weighted amount of text; low-confidence words count against a reading."""
    return sum((w["conf"] - 50) * len(w["text"]) for ln in lines for w in ln)


def to_text(lines):
    return "\n".join(" ".join(w["text"] for w in ln) for ln in lines)


def prepare(path):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("L")
    scale = TARGET_WIDTH / img.width
    img = img.resize((TARGET_WIDTH, round(img.height * scale)), Image.LANCZOS)
    return img.point(lambda p: 255 if p > 150 else 0)  # posters are black on white


def ocr_image(path):
    img = prepare(path)
    best = None
    for angle in [0] + ROTATIONS:
        rotated = img if angle == 0 else img.rotate(angle, expand=True, fillcolor=255)
        # psm 3 (automatic layout) sometimes treats big type as a picture and skips it;
        # psm 6 (one uniform block) catches those. Keep whichever reads better.
        for psm in (3, 6):
            lines = filter_words(tesseract_tsv(rotated, psm=psm))
            words = [w for ln in lines for w in ln]
            cand = {"angle": angle, "psm": psm, "lines": lines, "score": score(lines),
                    "conf": statistics.mean(w["conf"] for w in words) if words else 0}
            if best is None or cand["score"] > best["score"]:
                best = cand
        # Upright (0°) and sideways (90°, 270°) are always tried: a sideways poster can yield a
        # few confident junk words when read upright. The tilts and 180° only if still poor.
        if angle == 270 and best["conf"] >= GOOD_ENOUGH and sum(map(len, best["lines"])) >= 2:
            break
    return {"text": to_text(best["lines"]), "angle": best["angle"], "psm": best["psm"],
            "conf": round(best["conf"], 1), "words": sum(map(len, best["lines"]))}


def work(row):
    pid, filename = row
    try:
        return pid, ocr_image(IMG_DIR / filename), None
    except Exception as e:
        return pid, None, f"{type(e).__name__}: {e}"


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posters)")}
    for name, typ in (("ocr_conf", "REAL"), ("ocr_angle", "INTEGER"), ("ocr_at", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE posters ADD COLUMN {name} {typ}")
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redo", action="store_true", help="re-OCR posters that already have text")
    ap.add_argument("--ids", type=int, nargs="*", help="only these poster ids")
    ap.add_argument("--show", action="store_true", help="print results instead of only storing them")
    ap.add_argument("--dry-run", action="store_true", help="don't write to the database")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)
    sql = "SELECT id, filename FROM posters WHERE filename IS NOT NULL"
    params = []
    if args.ids:
        sql += f" AND id IN ({','.join('?' * len(args.ids))})"
        params = args.ids
    elif not args.redo:
        sql += " AND ocr_at IS NULL"
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    print(f"{len(rows)} posters to OCR with {args.workers} workers", flush=True)

    done = failed = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for f in as_completed(futs):
            pid, res, err = f.result()
            if err:
                failed += 1
                print(f"  FAILED {pid}: {err}", file=sys.stderr)
                continue
            done += 1
            if args.show:
                title = conn.execute("SELECT title FROM posters WHERE id=?", (pid,)).fetchone()[0]
                print(f"=== {pid} angle={res['angle']} psm={res['psm']} conf={res['conf']}  title: {title}\n{res['text']}")
            if not args.dry_run:
                conn.execute(
                    "UPDATE posters SET text=?, ocr_conf=?, ocr_angle=?, ocr_at=datetime('now') WHERE id=?",
                    (res["text"] or None, res["conf"], res["angle"], pid),
                )
            if done % 200 == 0:
                conn.commit()
                print(f"  {done + failed}/{len(rows)} ({failed} failed)", flush=True)
    conn.commit()
    print(f"done: {done} OCR'd, {failed} failed")


if __name__ == "__main__":
    main()

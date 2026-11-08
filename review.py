#!/usr/bin/env python3
"""Manual word-count review for posters where the title and the OCR disagree.

    uv run --with pillow review.py sheets --max-words 6     # writes review/sheet-NN.png + review/sheet-NN.json
    python3 review.py set 52437=7 8462=4 ...                # record counts read by hand
    python3 review.py list                                  # show recorded overrides

Overrides are stored in word_overrides.csv (committed) and mirrored into
loesje.db's word_overrides table; pgload.py applies them on top of the
automatic choice.
"""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from textutil import count_words, decide_word_count  # noqa: E402

DB = ROOT / "loesje.db"
OUT = ROOT / "review"


OVERRIDES_CSV = ROOT / "word_overrides.csv"  # committed: the hand-read counts are the valuable part


def connect():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS word_overrides (
        poster_id INTEGER PRIMARY KEY, word_count INTEGER NOT NULL, note TEXT)""")
    import_overrides(conn)
    return conn


def import_overrides(conn):
    """Merge word_overrides.csv into the database (the CSV wins)."""
    if OVERRIDES_CSV.exists():
        with open(OVERRIDES_CSV, newline="", encoding="utf-8") as f:
            rows = [(int(r["poster_id"]), int(r["word_count"]), r["note"]) for r in csv.DictReader(f)]
        conn.executemany("INSERT OR REPLACE INTO word_overrides VALUES (?, ?, ?)", rows)
        conn.commit()


def export_overrides(conn):
    with open(OVERRIDES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["poster_id", "word_count", "note"])
        w.writerows(conn.execute("SELECT poster_id, word_count, note FROM word_overrides ORDER BY poster_id"))


def candidates(conn, max_words, min_diff=1):
    done = {r[0] for r in conn.execute("SELECT poster_id FROM word_overrides")}
    out = []
    for pid, title, text, filename in conn.execute(
            "SELECT id, title, text, filename FROM posters WHERE filename IS NOT NULL ORDER BY id"):
        if pid in done:
            continue
        wc, src = decide_word_count(text, title)
        ocr_n = count_words(text) if text else None
        if (ocr_n is not None and abs(ocr_n - wc) >= min_diff
                and min(wc, ocr_n) <= max_words):
            out.append({"id": pid, "auto": wc, "auto_src": src, "ocr": ocr_n, "title": title, "filename": filename})
    return out


def make_sheets(conn, max_words, per_sheet, min_diff=1):
    from PIL import Image, ImageDraw, ImageFont
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("sheet-*"):  # replace the previous batch
        old.unlink()
    items = candidates(conn, max_words, min_diff)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    W, H, cols = 330, 490, 6
    for s in range(0, len(items), per_sheet):
        chunk = items[s:s + per_sheet]
        rows = -(-len(chunk) // cols)
        sheet = Image.new("RGB", (W * cols, H * rows), (128, 128, 128))
        d = ImageDraw.Draw(sheet)
        for i, it in enumerate(chunk):
            im = Image.open(ROOT / "thumbs" / it["filename"]).convert("RGB")
            im.thumbnail((W - 6, H - 36))
            x, y = (i % cols) * W, (i // cols) * H
            sheet.paste(im, (x + 3, y + 34))
            d.rectangle([x, y, x + W, y + 32], fill="black")
            d.text((x + 8, y + 4), f"{i}: {it['id']}", fill="white", font=font)
        n = s // per_sheet
        sheet.save(OUT / f"sheet-{n:02d}.png")
        (OUT / f"sheet-{n:02d}.json").write_text(json.dumps(chunk, ensure_ascii=False, indent=1))
    print(f"{len(items)} posters to review in {-(-len(items) // per_sheet)} sheets under {OUT}/")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sheets")
    p.add_argument("--max-words", type=int, default=6)
    p.add_argument("--per-sheet", type=int, default=18)
    p.add_argument("--min-diff", type=int, default=1, help="min |title count - OCR count|")
    p = sub.add_parser("set")
    p.add_argument("pairs", nargs="+", help="poster_id=word_count")
    p.add_argument("--note", default="read by hand")
    sub.add_parser("list")
    args = ap.parse_args()
    conn = connect()
    if args.cmd == "sheets":
        make_sheets(conn, args.max_words, args.per_sheet, args.min_diff)
    elif args.cmd == "set":
        for pair in args.pairs:
            pid, n = (int(x) for x in pair.split("="))
            conn.execute("INSERT OR REPLACE INTO word_overrides VALUES (?, ?, ?)", (pid, n, args.note))
        conn.commit()
        export_overrides(conn)
        print(f"recorded {len(args.pairs)} overrides; "
              f"{conn.execute('SELECT count(*) FROM word_overrides').fetchone()[0]} in total")
    else:
        for row in conn.execute("SELECT * FROM word_overrides ORDER BY poster_id"):
            print(*row)


if __name__ == "__main__":
    main()

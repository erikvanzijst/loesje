#!/usr/bin/env python3
"""QA for stage 2: compare OCR text against the CMS titles.

    python3 ocr_report.py            # summary + worst matches
    python3 ocr_report.py --worst 50

The title is an independent (if imperfect) transcription of the same slogan,
so low similarity flags posters worth a look.
"""

import argparse
import difflib
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "loesje.db"


def words(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s)


def similarity(a, b):
    return difflib.SequenceMatcher(None, words(a), words(b), autojunk=False).ratio()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worst", type=int, default=25)
    args = ap.parse_args()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, text, ocr_conf, ocr_angle FROM posters WHERE ocr_at IS NOT NULL"
    ).fetchall()
    scored = [(similarity(text, title), pid, title, text, conf, angle) for pid, title, text, conf, angle in rows]
    buckets = Counter()
    for s, *_ in scored:
        buckets["1.00" if s == 1 else ">=0.9" if s >= .9 else ">=0.75" if s >= .75 else ">=0.5" if s >= .5 else "<0.5"] += 1
    print(f"{len(rows)} posters OCR'd; {sum(1 for r in rows if not r[2])} with no text found")
    print("title similarity:", dict(sorted(buckets.items(), reverse=True)))
    print("angles:", dict(Counter(r[4] for r in rows).most_common()))
    print(f"\nworst {args.worst}:")
    for s, pid, title, text, conf, angle in sorted(scored)[: args.worst]:
        print(f"{s:.2f} id={pid} angle={angle} conf={conf}\n   title: {title}\n   ocr:   {(text or '').replace(chr(10), ' / ')}")


if __name__ == "__main__":
    main()

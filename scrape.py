#!/usr/bin/env python3
"""Stage 1: download all Loesje posters plus their metadata into SQLite.

The www.loesje.nl site is a Nuxt front end over a WordPress backend at
shop.loesje.nl, which exposes the posters through the public WP REST API
(post type `poster`, taxonomies `source` and `subject`). We read that API
directly instead of scraping the infinite-scroll HTML.

Re-runnable: metadata is upserted, and images already on disk are skipped.

    python3 scrape.py                 # metadata + images
    python3 scrape.py --skip-images   # metadata only
    python3 scrape.py --images-only   # just fetch missing images
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://shop.loesje.nl/wp-json/wp/v2"
GRAPHQL = "https://shop.loesje.nl/graphql"
UA = "loesje-archive-indexer/1.0 (+python-urllib)"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "loesje.db"
IMG_DIR = ROOT / "images"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posters (
    id              INTEGER PRIMARY KEY,   -- WordPress post id
    slug            TEXT NOT NULL,
    title           TEXT,                  -- WP title; usually the slogan, '/' = line break
    link            TEXT,
    pub_date        TEXT,                  -- publication date (ACF 'date'), YYYY-MM-DD
    wp_date         TEXT,                  -- WP post date (often the import date for old posters)
    modified        TEXT,
    media_id        INTEGER,
    image_url       TEXT,
    filename        TEXT,                  -- relative to images/
    image_width     INTEGER,
    image_height    INTEGER,
    alt_text        TEXT,
    pdf_media_id    INTEGER,
    downloaded_at   TEXT,
    text            TEXT,                  -- stage 2: text read from the poster
    raw_json        TEXT
);
CREATE TABLE IF NOT EXISTS terms (
    id        INTEGER PRIMARY KEY,
    taxonomy  TEXT NOT NULL,               -- 'source' (bron) or 'subject' (onderwerp)
    slug      TEXT NOT NULL,
    name      TEXT NOT NULL,
    parent    INTEGER
);
CREATE TABLE IF NOT EXISTS poster_terms (
    poster_id INTEGER NOT NULL REFERENCES posters(id),
    term_id   INTEGER NOT NULL REFERENCES terms(id),
    PRIMARY KEY (poster_id, term_id)
);
CREATE INDEX IF NOT EXISTS idx_posters_pub_date ON posters(pub_date);
CREATE INDEX IF NOT EXISTS idx_poster_terms_term ON poster_terms(term_id);

CREATE VIEW IF NOT EXISTS poster_index AS
SELECT p.id, p.pub_date, p.title, p.text, p.filename,
       (SELECT group_concat(t.name, '; ') FROM poster_terms pt JOIN terms t ON t.id = pt.term_id
         WHERE pt.poster_id = p.id AND t.taxonomy = 'subject') AS subjects,
       (SELECT group_concat(t.name, '; ') FROM poster_terms pt JOIN terms t ON t.id = pt.term_id
         WHERE pt.poster_id = p.id AND t.taxonomy = 'source') AS sources
FROM posters p;
"""


def get(url, retries=5):
    """GET with retries/backoff. Returns (body_bytes, headers)."""
    for attempt in range(retries):
        try:
            # Some upload filenames contain non-ASCII characters (e.g. "ë"); percent-encode them.
            safe_url = urllib.parse.quote(url, safe=":/?&=%#+,;@~")
            req = urllib.request.Request(safe_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410):
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        wait = 2 ** attempt
        print(f"  retry {attempt + 1}/{retries} in {wait}s: {url} ({err})", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {err}")


def get_json(path, retries=5, **params):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body, headers = get(url, retries=retries)
    return json.loads(body), headers


def get_json_once(path, **params):
    return get_json(path, retries=2, **params)


def fetch_terms(conn):
    for taxonomy, endpoint in (("source", "sources"), ("subject", "subjects")):
        page, n = 1, 0
        while True:
            data, headers = get_json(endpoint, per_page=100, page=page, hide_empty="false")
            for t in data:
                conn.execute(
                    "INSERT OR REPLACE INTO terms (id, taxonomy, slug, name, parent) VALUES (?,?,?,?,?)",
                    (t["id"], taxonomy, t["slug"], html.unescape(t["name"]), t.get("parent") or None),
                )
            n += len(data)
            if page >= int(headers.get("X-WP-TotalPages", 1)):
                break
            page += 1
        print(f"{taxonomy}: {n} terms")
    conn.commit()


def ymd(s):
    """ACF date '20261103' -> '2026-11-03'."""
    if s and len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s or None


GQL_POSTER = """
  databaseId slug title date modified link
  featuredImage { node { databaseId sourceUrl altText mediaDetails { width height } } }
  sources { nodes { databaseId } }
  subjects { nodes { databaseId } }
"""


def gql(query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                GRAPHQL, data=body, headers={"User-Agent": UA, "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            if res.get("errors") and not res.get("data"):
                raise RuntimeError(res["errors"])
            return res["data"]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"graphql failed: {err}")


def gql_poster(pid):
    q = f"query($id: ID!) {{ poster(id: $id, idType: DATABASE_ID) {{ {GQL_POSTER} }} }}"
    return gql(q, id=str(pid))["poster"]


def media_for(post):
    """Featured media as a REST-like dict. Some attachments are not publicly readable
    through REST (401), but GraphQL still exposes their URL, so fall back to that."""
    emb = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    m = emb[0] if emb else None
    if m and m.get("source_url"):
        return m
    try:
        node = ((gql_poster(post["id"]) or {}).get("featuredImage") or {}).get("node")
    except Exception as e:
        print(f"  graphql lookup failed for poster {post['id']}: {e}", file=sys.stderr)
        return None
    if not node or not node.get("sourceUrl"):
        return None
    md = node.get("mediaDetails") or {}
    return {"source_url": node["sourceUrl"], "alt_text": node.get("altText"),
            "media_details": {"width": md.get("width"), "height": md.get("height")}}


def rest_from_gql(g):
    """Build a REST-shaped poster record from GraphQL, for posters the REST API
    can't serialize (it crashes on some posters whose image attachment is broken)."""
    pub = None
    mo = re.fullmatch(r"nl(\d\d)(\d\d)_\d+", g["slug"])
    if mo:  # old archive slugs encode the series: nlYYMM_n
        yy, mm = int(mo[1]), mo[2]
        pub = f"{1900 + yy if yy >= 80 else 2000 + yy}{mm}01"
    node = (g.get("featuredImage") or {}).get("node") or {}
    return {
        "id": g["databaseId"], "slug": g["slug"], "title": {"rendered": g["title"] or ""},
        "link": g.get("link"), "date": g.get("date"), "modified": g.get("modified"),
        "featured_media": node.get("databaseId"), "acf": {"date": pub},
        "sources": [n["databaseId"] for n in g["sources"]["nodes"]],
        "subjects": [n["databaseId"] for n in g["subjects"]["nodes"]],
        "_source": "graphql",
    }


def backfill_from_graphql(conn):
    """List every poster id via GraphQL and fetch any the REST pass missed."""
    ids, after = set(), None
    while True:
        d = gql("query($after: String) { posters(first: 100, after: $after) "
                "{ pageInfo { hasNextPage endCursor } nodes { databaseId } } }", after=after)["posters"]
        ids.update(n["databaseId"] for n in d["nodes"])
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]
    have = {r[0] for r in conn.execute("SELECT id FROM posters")}
    missing = sorted(ids - have)
    print(f"graphql lists {len(ids)} posters; {len(missing)} missing from REST: {missing}")
    for pid in missing:
        store_poster(conn, rest_from_gql(gql_poster(pid)))
    conn.commit()


def store_poster(conn, p):
    m = media_for(p)
    url = m.get("source_url") if m else None
    md = (m or {}).get("media_details") or {}
    # NFC: presigned S3 URLs for keys with combining characters (NFD "ë") fail on
    # the bucket, so keep filenames (and hence object keys) composed.
    filename = unicodedata.normalize("NFC", f"{p['id']}-{url.rsplit('/', 1)[-1]}") if url else None
    acf = p.get("acf") or {}
    raw = {k: v for k, v in p.items() if k not in ("_embedded", "_links")}
    conn.execute(
        """INSERT INTO posters (id, slug, title, link, pub_date, wp_date, modified, media_id,
               image_url, filename, image_width, image_height, alt_text, pdf_media_id, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               slug=excluded.slug, title=excluded.title, link=excluded.link,
               pub_date=excluded.pub_date, wp_date=excluded.wp_date, modified=excluded.modified,
               media_id=excluded.media_id, image_url=excluded.image_url,
               filename=excluded.filename, image_width=excluded.image_width,
               image_height=excluded.image_height, alt_text=excluded.alt_text,
               pdf_media_id=excluded.pdf_media_id, raw_json=excluded.raw_json,
               downloaded_at=CASE WHEN posters.image_url IS excluded.image_url
                                  THEN posters.downloaded_at END""",
        (
            p["id"], p["slug"], html.unescape(p["title"]["rendered"]), p.get("link"),
            ymd(acf.get("date")), p.get("date"), p.get("modified"), p.get("featured_media") or None,
            url, filename, md.get("width"), md.get("height"),
            html.unescape(m.get("alt_text") or "") if m else None,
            acf.get("pdf") or None, json.dumps(raw, ensure_ascii=False),
        ),
    )
    conn.execute("DELETE FROM poster_terms WHERE poster_id = ?", (p["id"],))
    for tid in (p.get("sources") or []) + (p.get("subjects") or []):
        conn.execute("INSERT OR IGNORE INTO poster_terms VALUES (?,?)", (p["id"], tid))


def fetch_range(offset, count, bad):
    """Fetch `count` posters starting at `offset` (ordered by id). Some individual
    posters make the server return HTTP 500; on error, bisect the range down to
    single posters and record the offsets that can't be fetched in `bad`."""
    params = dict(per_page=count, offset=offset, orderby="id", order="asc", _embed="wp:featuredmedia")
    try:
        data, headers = get_json_once("posters", **params)
        return data, int(headers.get("X-WP-Total", 0))
    except (RuntimeError, urllib.error.HTTPError) as e:
        if count == 1:
            # Try once more without the embed, which is what usually breaks.
            try:
                params.pop("_embed")
                data, headers = get_json_once("posters", **params)
                return data, int(headers.get("X-WP-Total", 0))
            except Exception:
                bad.append(offset)
                print(f"  skipping poster at offset {offset}: {e}", file=sys.stderr)
                return [], 0
        half = count // 2
        a, t1 = fetch_range(offset, half, bad)
        b, t2 = fetch_range(offset + half, count - half, bad)
        return a + b, max(t1, t2)


def fetch_posters(conn):
    offset, total, n, bad, seen = 0, None, 0, [], set()
    while total is None or offset < total:
        data, t = fetch_range(offset, 100, bad)
        total = t or total or 0
        for p in data:
            store_poster(conn, p)
            seen.add(p["id"])
        conn.commit()
        n += len(data)
        offset += 100
        print(f"offset {offset}/{total}: {n} posters", flush=True)
    if bad:
        print(f"{len(bad)} posters failed over REST (offsets {bad}); will try GraphQL")
    return seen


def download_one(pid, url, filename):
    dest = IMG_DIR / filename
    if dest.exists() and dest.stat().st_size > 0:
        return pid, None
    body, _ = get(url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(body)
    tmp.rename(dest)
    return pid, None


def download_images(conn, workers):
    IMG_DIR.mkdir(exist_ok=True)
    rows = conn.execute(
        "SELECT id, image_url, filename FROM posters WHERE image_url IS NOT NULL AND downloaded_at IS NULL"
    ).fetchall()
    print(f"{len(rows)} images to fetch")
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_one, *r): r for r in rows}
        for f in as_completed(futs):
            pid = futs[f][0]
            try:
                f.result()
                conn.execute("UPDATE posters SET downloaded_at = datetime('now') WHERE id = ?", (pid,))
                done += 1
            except Exception as e:
                failed += 1
                print(f"  FAILED poster {pid}: {e}", file=sys.stderr)
            if (done + failed) % 200 == 0:
                conn.commit()
                print(f"  {done + failed}/{len(rows)} ({failed} failed)")
    conn.commit()
    print(f"images: {done} ok, {failed} failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-images", action="store_true")
    ap.add_argument("--images-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4, help="parallel image downloads (default 4)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if not args.images_only:
        fetch_terms(conn)
        fetch_posters(conn)
        backfill_from_graphql(conn)
    if not args.skip_images:
        download_images(conn, args.workers)
    missing = conn.execute("SELECT count(*) FROM posters WHERE image_url IS NULL").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM posters").fetchone()[0]
    print(f"done: {total} posters in {DB_PATH.name}, {missing} without an image")


if __name__ == "__main__":
    main()

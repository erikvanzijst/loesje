#!/usr/bin/env python3
"""Search site for the Loesje poster archive.

    DATABASE_URL=postgresql://... python3 server.py          # binds 0.0.0.0:$PORT (default 8080)

Data lives in Postgres (load it with pgload.py). Poster images live in the S3
bucket under images/ and thumbs/; requests for them are redirected to short-lived
presigned URLs, so the bytes never pass through this process. Without S3
credentials in the environment (local development) they are served from the
local images/ and thumbs/ directories instead.
"""

import argparse
import csv
import html
import io
import json
import mimetypes
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import psycopg
from psycopg_pool import ConnectionPool

from textutil import search_terms

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PAGE_SIZE = 30
PRESIGN_SECONDS = 6 * 3600

pool: ConnectionPool = None
s3 = None  # boto3 client when running with a bucket


def parse_words(spec):
    """'3' | '3,5' | '3-6' | '8+' | '-4' (or combinations, comma-separated) ->
    SQL condition on word_count plus params. Returns (None, []) if empty/invalid."""
    parts, params = [], []
    for tok in re.split(r"[,\s;]+", (spec or "").strip()):
        if not tok:
            continue
        if m := re.fullmatch(r"(\d+)", tok):
            parts.append("p.word_count = %s")
            params.append(int(m[1]))
        elif m := re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", tok):
            lo, hi = sorted((int(m[1]), int(m[2])))
            parts.append("p.word_count BETWEEN %s AND %s")
            params += [lo, hi]
        elif m := re.fullmatch(r"(\d+)\+", tok):
            parts.append("p.word_count >= %s")
            params.append(int(m[1]))
        elif m := re.fullmatch(r"[-–](\d+)", tok):
            parts.append("p.word_count <= %s")
            params.append(int(m[1]))
    if not parts:
        return None, []
    return "(" + " OR ".join(parts) + ")", params


def valid_date(s):
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or "") else None


SUMMARY_COLS = """p.id, p.slug, p.title, p.text, p.sort_date::text, p.filename,
                  p.image_width, p.image_height, p.word_count, p.word_count_source"""


def filter_sql(qs):
    """The search filters from a query string -> (WHERE clause, params, ORDER direction).
    Shared by the paginated JSON search and the CSV export."""
    where, params = [], []
    # Every search word must occur. Words of 3+ letters match anywhere inside a word
    # (trigram-indexed substring), so "verandering" finds "klimaatverandering". Shorter
    # words ("de", "ik") only match as whole words, or they'd match nearly everything.
    for t in search_terms(qs.get("q", [""])[0]):
        if len(t) >= 3:
            where.append("p.search_text LIKE %s")
            params.append("%" + t + "%")  # terms are alphanumeric, so no LIKE escaping needed
        else:
            where.append("p.search @@ to_tsquery('simple', %s)")
            params.append(t)
    # Within a taxonomy any selected term matches (OR); across taxonomies all must (AND).
    for key in ("source", "subject"):
        ids = [int(x) for v in qs.get(key, []) for x in v.split(",") if x.isdigit()]
        if ids:
            where.append("p.id IN (SELECT poster_id FROM poster_terms WHERE term_id = ANY(%s))")
            params.append(ids)
    if d := valid_date(qs.get("from", [""])[0]):
        where.append("p.sort_date >= %s")
        params.append(d)
    if d := valid_date(qs.get("to", [""])[0]):
        where.append("p.sort_date <= %s")
        params.append(d)
    cond, wparams = parse_words(qs.get("words", [""])[0])
    if cond:
        where.append(cond)
        params += wparams
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ASC" if qs.get("order", [""])[0] == "oldest" else "DESC"
    return sql_where, params, order


def search(conn, qs):
    sql_where, params, order = filter_sql(qs)
    try:
        page = max(1, int(qs.get("page", ["1"])[0] or 1))
    except ValueError:
        page = 1

    total = conn.execute(f"SELECT count(*) FROM posters p {sql_where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT {SUMMARY_COLS} FROM posters p {sql_where}
            ORDER BY p.sort_date {order} NULLS LAST, p.id {order}
            LIMIT %s OFFSET %s""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return {"total": total, "page": page, "pageSize": PAGE_SIZE, "items": [poster_summary(r) for r in rows]}


def poster_summary(r):
    pid, slug, title, text, date, filename, w, h, wc, wcs = r
    return {
        "id": pid, "slug": slug, "title": title, "text": text, "date": date,
        "image": f"/images/{filename}" if filename else None,
        "thumb": f"/thumbs/{filename}" if filename else None,
        "width": w, "height": h, "words": wc, "wordsSource": wcs,
    }


CSV_COLUMNS = [
    "id", "date", "title", "word_count", "word_count_source", "sources", "subjects",
    "image_url", "poster_url", "loesje_url",
]


def export_csv(conn, qs, base_url):
    """All posters matching the search filters (no pagination) as CSV text.
    Image links point at this site's /images/ path, which redirects to a freshly
    signed bucket URL, so they keep working (a presigned URL would expire)."""
    sql_where, params, order = filter_sql(qs)
    rows = conn.execute(
        f"""SELECT p.id, p.sort_date::text, p.title, p.word_count, p.word_count_source,
                   (SELECT string_agg(t.name, '; ' ORDER BY t.name) FROM poster_terms pt
                     JOIN terms t ON t.id = pt.term_id WHERE pt.poster_id = p.id AND t.taxonomy = 'source'),
                   (SELECT string_agg(t.name, '; ' ORDER BY t.name) FROM poster_terms pt
                     JOIN terms t ON t.id = pt.term_id WHERE pt.poster_id = p.id AND t.taxonomy = 'subject'),
                   p.filename, p.slug
            FROM posters p {sql_where}
            ORDER BY p.sort_date {order} NULLS LAST, p.id {order}""",
        params,
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for pid, date, title, wc, wcs, sources, subjects, filename, slug in rows:
        w.writerow([
            pid, date, title, wc, wcs, sources or "", subjects or "",
            f"{base_url}/images/{quote(filename)}" if filename else "",
            f"{base_url}/?poster={pid}",
            f"https://www.loesje.nl/posters/{slug}/",
        ])
    return buf.getvalue(), len(rows)


def poster_detail(conn, pid):
    r = conn.execute(f"SELECT {SUMMARY_COLS}, p.pub_date IS NOT NULL FROM posters p WHERE p.id = %s",
                     (pid,)).fetchone()
    if not r:
        return None
    d = poster_summary(r[:-1])
    d["pubDateKnown"] = r[-1]
    d["terms"] = [
        {"id": tid, "taxonomy": tax, "name": name}
        for tid, tax, name in conn.execute(
            "SELECT t.id, t.taxonomy, t.name FROM poster_terms pt JOIN terms t ON t.id = pt.term_id "
            "WHERE pt.poster_id = %s ORDER BY t.taxonomy, t.name",
            (pid,),
        )
    ]
    d["url"] = f"https://www.loesje.nl/posters/{r[1]}/"
    return d


def filters(conn):
    terms = conn.execute(
        """SELECT t.id, t.taxonomy, t.name, count(pt.poster_id) FROM terms t
           LEFT JOIN poster_terms pt ON pt.term_id = t.id GROUP BY t.id ORDER BY t.name"""
    ).fetchall()
    lo, hi = conn.execute("SELECT min(sort_date)::text, max(sort_date)::text FROM posters").fetchone()
    hist = conn.execute("SELECT word_count, count(*) FROM posters GROUP BY 1 ORDER BY 1").fetchall()
    return {
        "sources": [{"id": i, "name": n, "count": c} for i, t, n, c in terms if t == "source"],
        "subjects": [{"id": i, "name": n, "count": c} for i, t, n, c in terms if t == "subject"],
        "dateMin": lo, "dateMax": hi,
        "wordHistogram": [{"words": w, "count": c} for w, c in hist],
    }


SITE_NAME = "Posterarchief"
SITE_DESCRIPTION = ("Doorzoek ruim 7.900 Loesje-posters op tekst, onderwerp, bron en datum, "
                    "en op het aantal woorden.")
INDEX_TEMPLATE = None  # static/index.html, loaded at startup


def describe_search(conn, qs):
    """Human-readable Dutch summary of the search filters, or None without filters."""
    parts = []
    if q := qs.get("q", [""])[0].strip():
        parts.append(f"met “{q}”")
    if words := qs.get("words", [""])[0].strip():
        parts.append(f"met {words} {'woord' if words == '1' else 'woorden'}")
    ids = [int(x) for k in ("source", "subject") for v in qs.get(k, []) for x in v.split(",") if x.isdigit()]
    if ids:
        names = [r[0] for r in conn.execute("SELECT name FROM terms WHERE id = ANY(%s) ORDER BY name", (ids,))]
        if names:
            parts.append("over " + ", ".join(names))
    if d := valid_date(qs.get("from", [""])[0]):
        parts.append(f"vanaf {d}")
    if d := valid_date(qs.get("to", [""])[0]):
        parts.append(f"tot {d}")
    return ("Loesje-posters " + ", ".join(parts)) if parts else None


def render_index(conn, qs, base_url):
    """index.html with a <title> and link-preview (Open Graph / Twitter) tags for
    this URL. Crawlers don't run JavaScript, so these are rendered on the server."""
    url = base_url + "/"
    title, desc = f"{SITE_NAME} – Loesje-posters doorzoeken", SITE_DESCRIPTION
    image, image_w, image_h, image_alt = f"{base_url}/static/og.png", 1200, 630, "Loesje-posters op een muur"

    pid = qs.get("poster", [""])[0]
    poster = None
    if pid.isdigit():
        poster = conn.execute(
            "SELECT id, title, text, filename, image_width, image_height, word_count, sort_date::text "
            "FROM posters WHERE id = %s", (int(pid),)).fetchone()
    if poster:
        pid, ptitle, ptext, filename, w, h, wc, date = poster
        # The CMS title is the slogan as Loesje's editors typed it; OCR text only as a fallback.
        slogan = (ptitle or "").strip() or " / ".join((ptext or "").split("\n")) or f"Poster {pid}"
        subjects = [r[0] for r in conn.execute(
            "SELECT t.name FROM poster_terms pt JOIN terms t ON t.id = pt.term_id "
            "WHERE pt.poster_id = %s AND t.taxonomy = 'subject' ORDER BY t.name", (pid,))]
        title = f"{slogan} – {SITE_NAME}"
        desc = " · ".join(filter(None, [
            f"Loesje-poster{' uit ' + date[:4] if date else ''}",
            f"{wc} {'woord' if wc == 1 else 'woorden'}",
            ("Onderwerpen: " + ", ".join(subjects)) if subjects else "",
        ]))
        url = f"{base_url}/?poster={pid}"
        if filename:
            image, image_w, image_h, image_alt = f"{base_url}/images/{quote(filename)}", w, h, slogan
    elif summary := describe_search(conn, qs):
        sql_where, params, _ = filter_sql(qs)
        n = conn.execute(f"SELECT count(*) FROM posters p {sql_where}", params).fetchone()[0]
        title = f"{summary} – {SITE_NAME}"
        desc = f"{n} {'poster' if n == 1 else 'posters'} gevonden. {SITE_DESCRIPTION}"
        query = urlencode([(k, v) for k in ("q", "words", "source", "subject", "from", "to", "order")
                           for v in qs.get(k, [])])
        url = f"{base_url}/?{query}"

    e = lambda v: html.escape(str(v), quote=True)
    tags = [
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(desc)}">',
        f'<link rel="canonical" href="{e(url)}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{SITE_NAME}">',
        '<meta property="og:locale" content="nl_NL">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:url" content="{e(url)}">',
        f'<meta property="og:image" content="{e(image)}">',
        f'<meta property="og:image:alt" content="{e(image_alt)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(title)}">',
        f'<meta name="twitter:description" content="{e(desc)}">',
        f'<meta name="twitter:image" content="{e(image)}">',
    ]
    if image_w and image_h:
        tags[10:10] = [f'<meta property="og:image:width" content="{image_w}">',
                       f'<meta property="og:image:height" content="{image_h}">']
    return INDEX_TEMPLATE.replace("<!--HEAD-->", "\n  ".join(tags))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the platform logs requests; keep stdout for errors

    def send_body(self, status, body: bytes, ctype, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=HTTPStatus.OK):
        self.send_body(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def not_found(self):
        self.send_body(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def send_file(self, path: Path, cache: str):
        try:
            data = path.read_bytes()
        except OSError:
            return self.not_found()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_body(HTTPStatus.OK, data, ctype, [("Cache-Control", cache)])

    def send_image(self, kind, name):
        name = unquote(name)
        if not name or "/" in name or name.startswith("."):
            return self.not_found()
        if s3:
            url = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": os.environ["S3_BUCKET"], "Key": f"{kind}/{name}",
                    # The objects were uploaded without a content type; set it on the response.
                    "ResponseContentType": mimetypes.guess_type(name)[0] or "application/octet-stream",
                },
                ExpiresIn=PRESIGN_SECONDS,
            )
            # Let the browser reuse the redirect for a while (well within the URL's lifetime).
            self.send_body(HTTPStatus.FOUND, b"", "text/plain", [("Location", url), ("Cache-Control", "private, max-age=3600")])
        else:
            p = ROOT / kind / name
            if kind == "thumbs" and not p.exists():
                p = ROOT / "images" / name
            self.send_file(p, "public, max-age=86400")

    def base_url(self):
        """This site's own origin, as the browser sees it (the platform terminates TLS)."""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        proto = self.headers.get("X-Forwarded-Proto") or ("http" if host.startswith(("localhost", "127.")) else "https")
        return f"{proto}://{host}"

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path == "/healthz":
                with pool.connection() as conn:
                    conn.execute("SELECT 1")
                return self.send_body(HTTPStatus.OK, b"ok\n", "text/plain")
            if path.startswith("/api/"):
                with pool.connection() as conn:
                    if path == "/api/posters":
                        return self.send_json(search(conn, qs))
                    if m := re.fullmatch(r"/api/posters/(\d+)", path):
                        d = poster_detail(conn, int(m[1]))
                        return self.send_json(d) if d else self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    if path == "/api/filters":
                        return self.send_json(filters(conn))
                    if path == "/api/posters.csv":
                        body, n = export_csv(conn, qs, self.base_url())
                        # UTF-8 with a BOM, so spreadsheet apps read the accents correctly.
                        return self.send_body(
                            HTTPStatus.OK, ("\ufeff" + body).encode("utf-8"), "text/csv; charset=utf-8",
                            [("Content-Disposition", 'attachment; filename="loesje-posters.csv"'),
                             ("Cache-Control", "no-store")],
                        )
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            print(f"ERROR {self.command} {self.path}: {type(e).__name__}: {e}", flush=True)
            return self.send_json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        for kind in ("thumbs", "images"):
            if path.startswith(f"/{kind}/"):
                return self.send_image(kind, path[len(kind) + 2:])
        if path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
            if "/" in name or name.startswith("."):
                return self.not_found()
            return self.send_file(STATIC / name, "public, max-age=300")
        if path == "/" or path.startswith("/posters"):
            try:
                with pool.connection() as conn:
                    page = render_index(conn, qs, self.base_url())
            except Exception as e:  # never fail the page over its preview tags
                print(f"ERROR rendering head for {self.path}: {type(e).__name__}: {e}", flush=True)
                page = INDEX_TEMPLATE.replace("<!--HEAD-->", f"<title>{SITE_NAME}</title>")
            return self.send_body(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8",
                                  [("Cache-Control", "no-cache")])
        self.not_found()


def main():
    global pool, s3, INDEX_TEMPLATE
    INDEX_TEMPLATE = (STATIC / "index.html").read_text(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = ap.parse_args()

    # prepare_threshold=None: the platform connects through a transaction-pooling
    # PgBouncer, where server-side prepared statements don't survive.
    pool = ConnectionPool(
        os.environ["DATABASE_URL"], min_size=1, max_size=8, open=True,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
    # The database login can fail briefly while a release rolls out (the pooler caches a
    # failed login); retry for a while instead of crashing the container.
    for attempt in range(25):
        try:
            with pool.connection() as conn:
                conn.execute((ROOT / "schema.sql").read_text())  # create tables if missing
                n = conn.execute("SELECT count(*) FROM posters").fetchone()[0]
            break
        except psycopg.OperationalError as e:
            if attempt == 24:
                raise
            print(f"database not ready ({type(e).__name__}: {str(e)[:120]}); retrying in 5s", flush=True)
            time.sleep(5)
    if n == 0:
        print("WARNING: the posters table is empty; load it with pgload.py", flush=True)

    if os.environ.get("S3_BUCKET"):
        import boto3
        from botocore.config import Config
        s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    print(f"{n} posters; images from {'bucket ' + os.environ['S3_BUCKET'] if s3 else 'local disk'}", flush=True)
    print(f"listening on http://{args.host}:{args.port}/", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

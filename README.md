# Loesje poster archive index

Indexes every poster on https://www.loesje.nl/posters/ into `loesje.db` (SQLite).

## Stage 1 — metadata + images (`scrape.py`)

www.loesje.nl is a Nuxt front end over a WordPress backend at `shop.loesje.nl`.
Rather than scraping the infinite-scroll HTML, `scrape.py` reads the public
WordPress REST API (`/wp-json/wp/v2/posters`, plus the `sources` and `subjects`
taxonomies) and downloads each poster's full-size image to `images/`.

```sh
python3 scrape.py                # metadata + images (no dependencies, stdlib only)
python3 scrape.py --skip-images  # refresh metadata only
python3 scrape.py --images-only  # fetch images not yet downloaded
```

It is safe to re-run: posters are upserted, and images already downloaded are skipped.

Quirks it handles:

- A few posters crash the REST API with HTTP 500 (their image attachment is
  broken). The scraper bisects the failing page to isolate them, then fetches
  them from WPGraphQL (`/graphql`) instead.
- Some image attachments return 401 over REST but are exposed through GraphQL.
- The publication date is the ACF `date` field, not the WordPress post date:
  most posters before 2013 were bulk-imported on 2013-11-19. For the three
  GraphQL-only posters, `pub_date` is derived from the `nlYYMM_n` slug.

## Schema

- `posters`: one row per poster. Columns: `id`, `slug`, `title` (the slogan as
  entered in the CMS; `/` marks a line break), `pub_date`, `wp_date`,
  `image_url`, `filename` (relative to `images/`), `alt_text`, `text` (filled in
  stage 2), and `raw_json`.
- `terms`: sources ("bronnen") and subjects ("onderwerpen"), with `taxonomy` set
  to `source` or `subject`.
- `poster_terms`: links posters to terms (many-to-many).
- `poster_index` (view): one flat row per poster with subjects and sources
  joined as `;`-separated strings.

## Stage 2 — poster text (`ocr.py`) and word counts

```sh
uv run --with pillow ocr.py           # Tesseract (nld) OCR into posters.text; --redo, --ids, --show
python3 ocr_report.py                 # compare OCR with the CMS titles
uv run --with pillow review.py sheets # contact sheets of posters where title and OCR disagree
python3 review.py set 52437=7 ...     # record word counts read by hand (word_overrides.csv)
```

OCR keeps the large slogan type and drops the signature, credits ("de opa van")
and footer. It tries the poster upright, sideways and slightly tilted, and keeps
the best reading.

The **word count** (`textutil.decide_word_count`) is based on the CMS title,
cleaned of credits ("– oom Herman", "het broertje van") and notes ("(tekening)").
The OCR text is the cross-check: it's used instead when the title is empty or a
placeholder, or when it contains the whole title plus more words. Counts read by
hand, kept in `word_overrides.csv`, take precedence over both. 696 posters have
been reviewed this way: every poster with 1–6 words where the title and OCR counts
disagreed, and every poster where they differed by 2 or more words. The posters
whose two counts differ by exactly one word have not been reviewed yet.

A word is a run of letters or digits. Hyphenated words, contractions, dotted
abbreviations ("I.D.") and "m/v" each count as one word. The handwritten signature
doesn't count.

## Search site (`server.py`), deployed on Freepod

Postgres (`DATABASE_URL`) holds the data and the deployment's S3 bucket holds the
images. Image requests are redirected to presigned bucket URLs. Locally, without
S3 variables set, images are served from `images/` and `thumbs/` instead.

```sh
python3 pgload.py                      # loesje.db -> loesje.sql (schema + full reload, one transaction)
freepod shell psql < loesje.sql        # update the live data; no redeploy needed
freepod bucket cp ./images :images     # images and thumbnails (once; keys are NFC-normalized filenames)
freepod bucket cp ./thumbs :thumbs
freepod deploy                         # only for code changes
```

The site has the same search options as loesje.nl/posters: text, Bronnen,
Onderwerpen, and Van/Voor dates. It adds a **Woorden** filter that accepts `3`,
`3, 5`, `3-6`, `16+` or `-4`; the chips below the filters toggle exact counts.
Filters are kept in the URL.

- **CSV export** (`/api/posters.csv`, the "Exporteer naar CSV" link) exports
  every poster matching the current search, or the whole archive when nothing is
  filtered. Columns: id, date, title, word_count, word_count_source,
  sources, subjects, image_url, poster_url and loesje_url. The image URL points
  at this site's `/images/` path, which redirects to a freshly signed bucket
  URL. The file is UTF-8 with a byte-order mark (BOM), so spreadsheets read the
  accents correctly.
- **Link previews**: the server writes `<title>` and Open Graph/Twitter tags
  (`og:locale` nl_NL) for each URL. A poster link (`/?poster=ID`) shows that
  poster. A search link describes the search. The home page uses
  `static/og.png`, which `make_og_image.py` renders (font: Libre Franklin, OFL).

**Text search** covers the title, the OCR text and the alt text, ignoring case
and accents. Every search word must occur, in any order. Words of 3 or more
letters match anywhere inside a word (using a `pg_trgm` trigram index), so
"verandering" finds "klimaatverandering". Shorter words such as "de" or "ik"
only match as whole words. There's no stemming, so "liep" doesn't find "lopen".

Stack: Python standard library HTTP server, psycopg, boto3, Postgres (`pg_trgm`),
and plain HTML/CSS/JS in `static/`.

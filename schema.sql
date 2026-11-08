-- Postgres schema for the poster search site. Idempotent: the server runs it at
-- startup, and pgload.py puts it at the top of the data file.

-- Trigram index for substring search (Dutch compounds: "verandering" in "klimaatverandering").
-- pg_trgm is a trusted extension, so the database owner may create it.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS posters (
    id                 integer PRIMARY KEY,   -- WordPress post id
    slug               text NOT NULL,
    title              text,                  -- CMS title; '/' marks a line break
    text               text,                  -- slogan as read from the poster (stage 2 OCR)
    pub_date           date,                  -- publication date, if known
    sort_date          date,                  -- pub_date, else the WordPress post date
    filename           text,                  -- object key below images/ and thumbs/
    image_width        integer,
    image_height       integer,
    alt_text           text,
    word_count         integer NOT NULL,
    word_count_source  text NOT NULL,         -- 'title', 'text' (OCR) or 'manual'
    search_text        text NOT NULL,         -- lowercased, accents removed (see pgload.py)
    search             tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);

CREATE TABLE IF NOT EXISTS terms (
    id        integer PRIMARY KEY,
    taxonomy  text NOT NULL,                  -- 'source' (bron) or 'subject' (onderwerp)
    slug      text NOT NULL,
    name      text NOT NULL
);

CREATE TABLE IF NOT EXISTS poster_terms (
    poster_id integer NOT NULL REFERENCES posters(id) ON DELETE CASCADE,
    term_id   integer NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    PRIMARY KEY (poster_id, term_id)
);

CREATE INDEX IF NOT EXISTS posters_search_idx     ON posters USING gin (search);
CREATE INDEX IF NOT EXISTS posters_search_trgm_idx ON posters USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS posters_sort_date_idx  ON posters (sort_date, id);
CREATE INDEX IF NOT EXISTS posters_word_count_idx ON posters (word_count);
CREATE INDEX IF NOT EXISTS poster_terms_term_idx  ON poster_terms (term_id);

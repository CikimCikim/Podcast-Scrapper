-- schema.sql - Cloudflare D1 (run once: Actions -> init_db)
CREATE TABLE IF NOT EXISTS topics (
  site_id    TEXT PRIMARY KEY,
  topic_id   INTEGER NOT NULL,
  title      TEXT,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS episodes (
  site_id    TEXT NOT NULL,
  uid        TEXT NOT NULL,
  status     TEXT NOT NULL,          -- sent | skipped (old item marked seen on the first run)
  title      TEXT,
  link       TEXT,                   -- page of the episode (e.g. bbc.co.uk/programmes/...)
  pub_ts     INTEGER,                -- release date (unix)
  media_url  TEXT,
  extras     TEXT,                   -- JSON list of extra file URLs (pdf ...)
  tg_msg_id  INTEGER,                -- message id of the audio post
  tg_doc_id  INTEGER,                -- Telegram document id of the audio file
  extra_msg_ids TEXT,                -- JSON list of message ids of the extra files
  sent_at    INTEGER,
  page_url   TEXT,                   -- lesson page with the PDFs (e.g. bbc.co.uk/learningenglish/...)
  PRIMARY KEY (site_id, uid)
);
CREATE INDEX IF NOT EXISTS ep_pub ON episodes(site_id, pub_ts);

-- catalog written by scripts/scan.py (Actions -> scan)
CREATE TABLE IF NOT EXISTS cat_sites (
  site_id   TEXT PRIMARY KEY,
  name      TEXT,
  base      TEXT,
  tags      TEXT,
  status    TEXT,                    -- complete | partial | blocked
  last_scan INTEGER,
  pages     INTEGER,
  files     INTEGER
);
CREATE TABLE IF NOT EXISTS cat_pages (
  site_id  TEXT NOT NULL,
  url      TEXT NOT NULL,
  section  TEXT,
  title    TEXT,
  descr    TEXT,
  body     TEXT,                     -- first ~1200 chars of page text (for /search)
  via      TEXT,                     -- links | sitemap | probe
  found_at INTEGER,
  PRIMARY KEY (site_id, url)
);
CREATE INDEX IF NOT EXISTS cp_sec ON cat_pages(site_id, section);
CREATE TABLE IF NOT EXISTS cat_files (
  site_id  TEXT NOT NULL,
  url      TEXT NOT NULL,
  page_url TEXT,
  section  TEXT,
  ext      TEXT,
  name     TEXT,
  bytes    INTEGER,
  found_at INTEGER,
  PRIMARY KEY (site_id, url)
);
CREATE INDEX IF NOT EXISTS cf_ext ON cat_files(site_id, ext);

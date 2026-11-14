CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('locanto', 'doublelist', 'squirt', 'reddit', 'reddit-r4r', 'grindr', 'grindr-via-notification-bridge')),
    dedupe_key TEXT NOT NULL,
    source_id TEXT,
    canonical_url TEXT,
    title TEXT NOT NULL DEFAULT '',
    price_text TEXT,
    text_excerpt TEXT NOT NULL DEFAULT '',
    thumb_url TEXT,
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    locality TEXT,
    distance_miles REAL CHECK (distance_miles IS NULL OR distance_miles >= 0),
    tags_json TEXT NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'new' CHECK (review_status IN ('new', 'seen', 'saved', 'hidden', 'contacted')),
    UNIQUE (source, dedupe_key)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    scored_at TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    geographic_points INTEGER NOT NULL CHECK (geographic_points BETWEEN 0 AND 40),
    recency_points INTEGER NOT NULL CHECK (recency_points BETWEEN 0 AND 25),
    preference_points INTEGER NOT NULL CHECK (preference_points BETWEEN 0 AND 20),
    intent_points INTEGER NOT NULL CHECK (intent_points BETWEEN 0 AND 10),
    completeness_points INTEGER NOT NULL CHECK (completeness_points BETWEEN 0 AND 5),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    CHECK (score = geographic_points + recency_points + preference_points + intent_points + completeness_points),
    UNIQUE (listing_id, scored_at)
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    contacted_at TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'replied', 'ghosted', 'met')),
    ts TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'discarded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('locanto', 'doublelist', 'squirt', 'reddit', 'reddit-r4r', 'grindr', 'grindr-via-notification-bridge', 'scheduler')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'blocked', 'failed')),
    listings_seen INTEGER NOT NULL DEFAULT 0 CHECK (listings_seen >= 0),
    listings_added INTEGER NOT NULL DEFAULT 0 CHECK (listings_added >= 0),
    listings_updated INTEGER NOT NULL DEFAULT 0 CHECK (listings_updated >= 0),
    error_summary TEXT,
    CHECK ((status = 'running' AND finished_at IS NULL) OR (status <> 'running' AND finished_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS http_cache (
    source TEXT NOT NULL CHECK (source IN ('locanto', 'doublelist', 'squirt', 'reddit', 'reddit-r4r', 'grindr', 'grindr-via-notification-bridge')),
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status_code INTEGER NOT NULL CHECK (status_code BETWEEN 0 AND 599),
    final_url TEXT NOT NULL,
    headers_json TEXT NOT NULL DEFAULT '{}',
    body TEXT NOT NULL DEFAULT '',
    error_summary TEXT,
    PRIMARY KEY (source, url)
);

CREATE INDEX IF NOT EXISTS idx_listings_source_last_seen_at ON listings (source, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_dedupe_key ON listings (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen_at ON listings (first_seen_at);
CREATE INDEX IF NOT EXISTS idx_scores_listing_scored_at ON scores (listing_id, scored_at);
CREATE INDEX IF NOT EXISTS idx_contacts_listing_contacted_at ON contacts (listing_id, contacted_at);
CREATE INDEX IF NOT EXISTS idx_contacts_status_ts ON contacts (status, ts);
CREATE INDEX IF NOT EXISTS idx_drafts_listing_updated_at ON drafts (listing_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_runs_source_started_at ON runs (source, started_at);
CREATE INDEX IF NOT EXISTS idx_http_cache_source_fetched_at ON http_cache (source, fetched_at);

CREATE TABLE IF NOT EXISTS score_cache (
    record_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    components_json TEXT NOT NULL,
    why TEXT NOT NULL,
    model_used INTEGER NOT NULL DEFAULT 0 CHECK (model_used IN (0, 1)),
    scored_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_score_cache_expires_at ON score_cache (expires_at);

CREATE TABLE IF NOT EXISTS blocked_domains (
    domain TEXT PRIMARY KEY,
    blocked_at TEXT NOT NULL
);

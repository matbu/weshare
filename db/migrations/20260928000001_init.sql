-- migrate:up

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE webcam_status AS ENUM ('pending', 'approved', 'rejected');
CREATE TYPE endpoint_type AS ENUM ('image', 'mjpeg', 'hls', 'youtube', 'iframe', 'page');
CREATE TYPE user_role AS ENUM ('user', 'moderator', 'admin');

-- ---------------------------------------------------------------------------
-- Users (accounts able to submit webcams)
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          user_role NOT NULL DEFAULT 'user',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Canonical webcams: one row per physical camera, whatever the number of sources
-- ---------------------------------------------------------------------------
CREATE TABLE webcams (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    name         TEXT,
    description  TEXT,

    -- geometry (not geography): native for bbox (&&), vector tiles and KNN.
    -- Metric queries use the (geom::geography) expression index below.
    geom         geometry(Point, 4326) NOT NULL,
    latitude     DOUBLE PRECISION GENERATED ALWAYS AS (ST_Y(geom)) STORED,
    longitude    DOUBLE PRECISION GENERATED ALWAYS AS (ST_X(geom)) STORED,

    country_code TEXT CHECK (char_length(country_code) = 2),
    region       TEXT,
    city         TEXT,

    status       webcam_status NOT NULL DEFAULT 'approved',

    -- Maintained by the health checker: true when at least one media endpoint works.
    is_live             BOOLEAN NOT NULL DEFAULT false,
    preview_endpoint_id BIGINT,

    -- Pre-computed random key: O(log n) random sampling for the swipe feed
    -- instead of ORDER BY random() (full scan).
    rand         DOUBLE PRECISION NOT NULL DEFAULT random(),

    submitted_by BIGINT REFERENCES users(id) ON DELETE SET NULL,

    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX webcams_geom_idx ON webcams USING GIST (geom);
CREATE INDEX webcams_geog_idx ON webcams USING GIST ((geom::geography));
CREATE INDEX webcams_feed_idx ON webcams (rand) WHERE is_live AND status = 'approved';
CREATE INDEX webcams_country_idx ON webcams (country_code);
CREATE INDEX webcams_status_idx ON webcams (status) WHERE status = 'pending';
CREATE INDEX webcams_submitted_by_idx ON webcams (submitted_by) WHERE submitted_by IS NOT NULL;
CREATE INDEX webcams_name_trgm_idx ON webcams USING GIN (name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Where we learned about a webcam (OSM, Windy, open data, user...)
-- ---------------------------------------------------------------------------
CREATE TABLE webcam_sources (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    webcam_id   BIGINT NOT NULL REFERENCES webcams(id) ON DELETE CASCADE,

    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,

    source_url  TEXT,
    webpage_url TEXT,
    raw         JSONB,

    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source, source_id)
);

CREATE INDEX webcam_sources_webcam_idx ON webcam_sources (webcam_id);
CREATE INDEX webcam_sources_stale_idx ON webcam_sources (source, last_seen);

-- ---------------------------------------------------------------------------
-- How to actually watch a webcam (JPEG snapshot, HLS, MJPEG, embed, web page)
-- ---------------------------------------------------------------------------
CREATE TABLE webcam_endpoints (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    webcam_id     BIGINT NOT NULL REFERENCES webcams(id) ON DELETE CASCADE,

    type          endpoint_type NOT NULL,
    url           TEXT NOT NULL,
    origin        TEXT NOT NULL,           -- osm / windy / user / discovery

    width         INTEGER,
    height        INTEGER,

    -- Health checking (NULL = never checked)
    is_working    BOOLEAN,
    http_status   INTEGER,
    last_error    TEXT,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_checked  TIMESTAMPTZ,
    last_success  TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Conditional GET + frozen image detection
    etag          TEXT,
    last_modified TEXT,
    content_hash  TEXT,
    last_change   TIMESTAMPTZ,

    -- Page endpoints: when we last looked inside the page for a media URL
    discovered_at TIMESTAMPTZ,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A given URL belongs to exactly one webcam (also our strongest dedup key).
CREATE UNIQUE INDEX webcam_endpoints_url_idx ON webcam_endpoints (md5(url));
CREATE INDEX webcam_endpoints_webcam_idx ON webcam_endpoints (webcam_id);
CREATE INDEX webcam_endpoints_health_idx ON webcam_endpoints (next_check_at)
    WHERE type IN ('image', 'mjpeg', 'hls');
CREATE INDEX webcam_endpoints_discovery_idx ON webcam_endpoints (discovered_at NULLS FIRST)
    WHERE type = 'page';

ALTER TABLE webcams
    ADD CONSTRAINT webcams_preview_endpoint_fk
    FOREIGN KEY (preview_endpoint_id) REFERENCES webcam_endpoints(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- Collector monitoring
-- ---------------------------------------------------------------------------
CREATE TABLE source_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      TEXT NOT NULL,

    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,

    discovered  INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    duplicates  INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    stats       JSONB NOT NULL DEFAULT '{}',

    status      TEXT NOT NULL DEFAULT 'running',  -- running / success / partial / failed / aborted
    error       TEXT
);

CREATE INDEX source_runs_source_idx ON source_runs (source, started_at DESC);

-- migrate:down

DROP TABLE source_runs;
ALTER TABLE webcams DROP CONSTRAINT webcams_preview_endpoint_fk;
DROP TABLE webcam_endpoints;
DROP TABLE webcam_sources;
DROP TABLE webcams;
DROP TABLE users;
DROP TYPE user_role;
DROP TYPE endpoint_type;
DROP TYPE webcam_status;

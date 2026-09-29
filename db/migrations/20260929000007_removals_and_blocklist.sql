-- migrate:up

-- Owners asking us to stop showing their webcam(s). A 'webcam' request hides the webcam at
-- once (status -> pending) until a moderator decides; a 'site' request is reviewed first and,
-- if accepted, blocks the whole host.
CREATE TABLE removal_requests (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    webcam_id    BIGINT REFERENCES webcams(id) ON DELETE SET NULL,
    url          TEXT,
    scope        TEXT NOT NULL CHECK (scope IN ('webcam', 'site')),
    email        TEXT NOT NULL,
    requester    TEXT,
    message      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dismissed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_at   TIMESTAMPTZ,
    handled_by   BIGINT REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX removal_requests_pending_idx ON removal_requests (created_at) WHERE status = 'pending';

-- Hosts we never collect, embed or proxy again (owner opt-out, abuse...).
CREATE TABLE blocked_hosts (
    host        TEXT PRIMARY KEY CHECK (host = lower(host)),
    reason      TEXT,
    request_id  BIGINT REFERENCES removal_requests(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- migrate:down

DROP TABLE blocked_hosts;
DROP TABLE removal_requests;

-- migrate:up

-- Sources can be switched off without deleting anything: their webcams are hidden (unless
-- another enabled source also reports them), their endpoints are not used for display, and
-- the collector stops querying them.
CREATE TABLE sources (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sources (name, label) VALUES
    ('osm', 'OpenStreetMap'),
    ('windy', 'Windy Webcams API'),
    ('user', 'Ajouts des utilisateurs');

-- Denormalised so every public query stays a simple indexed filter.
ALTER TABLE webcams ADD COLUMN source_enabled BOOLEAN NOT NULL DEFAULT true;

DROP INDEX webcams_feed_idx;
CREATE INDEX webcams_feed_idx ON webcams (rand) WHERE is_live AND status = 'approved' AND source_enabled;

-- Single source of truth for "what does this webcam show", used by the collector and the API.
CREATE FUNCTION refresh_webcams(ids BIGINT[]) RETURNS void LANGUAGE sql AS $$
    UPDATE webcams w SET
        source_enabled = p.source_enabled,
        preview_endpoint_id = p.endpoint_id,
        is_live = p.endpoint_id IS NOT NULL,
        updated_at = now()
    FROM (
        SELECT wid,
               EXISTS (
                   SELECT 1 FROM webcam_sources s JOIN sources x ON x.name = s.source
                   WHERE s.webcam_id = wid AND x.enabled
               ) OR NOT EXISTS (
                   SELECT 1 FROM webcam_sources s WHERE s.webcam_id = wid
               ) AS source_enabled,
               (
                   SELECT e.id FROM webcam_endpoints e
                   WHERE e.webcam_id = wid AND e.is_working
                     AND NOT EXISTS (SELECT 1 FROM sources x WHERE x.name = e.origin AND NOT x.enabled)
                   ORDER BY CASE e.type WHEN 'image' THEN 0 WHEN 'hls' THEN 1 WHEN 'mjpeg' THEN 2
                                        WHEN 'dash' THEN 3 WHEN 'youtube' THEN 4 WHEN 'mp4' THEN 5
                                        WHEN 'iframe' THEN 6 ELSE 7 END, e.id
                   LIMIT 1
               ) AS endpoint_id
        FROM unnest(ids) AS wid
    ) p
    WHERE w.id = p.wid
      AND (w.preview_endpoint_id IS DISTINCT FROM p.endpoint_id
           OR w.is_live IS DISTINCT FROM (p.endpoint_id IS NOT NULL)
           OR w.source_enabled IS DISTINCT FROM p.source_enabled);
$$;

-- migrate:down

DROP FUNCTION refresh_webcams(BIGINT[]);
DROP INDEX webcams_feed_idx;
CREATE INDEX webcams_feed_idx ON webcams (rand) WHERE is_live AND status = 'approved';
ALTER TABLE webcams DROP COLUMN source_enabled;
DROP TABLE sources;

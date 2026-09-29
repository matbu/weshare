-- migrate:up

-- true: live stream (MJPEG, live HLS/DASH) · false: recording (MP4, HLS with #EXT-X-ENDLIST)
-- NULL: not a stream, or not probed yet. Set by the health checker.
ALTER TABLE webcam_endpoints ADD COLUMN live BOOLEAN;

DROP INDEX webcam_endpoints_health_idx;
CREATE INDEX webcam_endpoints_health_idx ON webcam_endpoints (next_check_at)
    WHERE type IN ('image', 'mjpeg', 'hls', 'mp4', 'dash');

-- Re-probe existing streams with the stricter checks right away.
UPDATE webcam_endpoints SET next_check_at = now() WHERE type IN ('mjpeg', 'hls');

-- migrate:down

DROP INDEX webcam_endpoints_health_idx;
CREATE INDEX webcam_endpoints_health_idx ON webcam_endpoints (next_check_at)
    WHERE type IN ('image', 'mjpeg', 'hls');
ALTER TABLE webcam_endpoints DROP COLUMN live;

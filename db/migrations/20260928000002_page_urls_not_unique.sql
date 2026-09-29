-- migrate:up

-- A web page often lists several cameras (road networks, ski resorts): a shared *page* URL
-- does not mean "same webcam". Only media URLs (image, stream...) stay globally unique.
DROP INDEX webcam_endpoints_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_media_url_idx ON webcam_endpoints (md5(url)) WHERE type <> 'page';
CREATE UNIQUE INDEX webcam_endpoints_webcam_url_idx ON webcam_endpoints (webcam_id, md5(url));

-- migrate:down

DROP INDEX webcam_endpoints_webcam_url_idx;
DROP INDEX webcam_endpoints_media_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_url_idx ON webcam_endpoints (md5(url));

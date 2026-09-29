-- migrate:up

-- The same URL can be both the webcam's web page ('page') and its embedded player ('iframe').
DROP INDEX webcam_endpoints_webcam_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_webcam_url_idx ON webcam_endpoints (webcam_id, type, md5(url));

-- migrate:down

DELETE FROM webcam_endpoints e USING webcam_endpoints p
WHERE e.type = 'iframe' AND p.type = 'page' AND p.webcam_id = e.webcam_id AND p.url = e.url;
DROP INDEX webcam_endpoints_webcam_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_webcam_url_idx ON webcam_endpoints (webcam_id, md5(url));

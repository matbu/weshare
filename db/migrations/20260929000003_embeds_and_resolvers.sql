-- migrate:up

-- Some providers (e.g. Skaping) have no stable image URL: the endpoint then stores the
-- provider page and `resolver` tells how to find the current image in it, at fetch time.
ALTER TABLE webcam_endpoints ADD COLUMN resolver TEXT;

-- An embedded provider page (iframe) is not an identity either: one page can show several
-- cameras. Keep global URL uniqueness for real media only.
DROP INDEX webcam_endpoints_media_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_media_url_idx ON webcam_endpoints (md5(url))
    WHERE type NOT IN ('page', 'iframe');

-- migrate:down

DROP INDEX webcam_endpoints_media_url_idx;
CREATE UNIQUE INDEX webcam_endpoints_media_url_idx ON webcam_endpoints (md5(url)) WHERE type <> 'page';
ALTER TABLE webcam_endpoints DROP COLUMN resolver;

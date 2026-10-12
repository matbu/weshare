-- migrate:up
INSERT INTO sources (name, label) VALUES ('nzta', 'NZ Transport Agency (NZ)')
ON CONFLICT (name) DO NOTHING;

-- migrate:down
DELETE FROM sources WHERE name = 'nzta';

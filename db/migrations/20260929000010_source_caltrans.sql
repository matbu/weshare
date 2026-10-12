-- migrate:up
INSERT INTO sources (name, label) VALUES ('caltrans', 'Caltrans (Californie)')
ON CONFLICT (name) DO NOTHING;

-- migrate:down
DELETE FROM sources WHERE name = 'caltrans';

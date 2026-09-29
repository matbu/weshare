#!/usr/bin/env bash
# Quick view of what the collector has done so far.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
echo
docker compose exec -T postgres psql -U webcams -d webcams <<'SQL'
SELECT count(*)                                   AS webcams,
       count(*) FILTER (WHERE is_live)            AS live,
       count(*) FILTER (WHERE status = 'pending') AS pending,
       count(DISTINCT country_code)               AS countries
FROM webcams;

SELECT type, count(*) AS endpoints,
       count(*) FILTER (WHERE is_working)         AS working,
       count(*) FILTER (WHERE last_checked IS NULL) AS unchecked
FROM webcam_endpoints GROUP BY type ORDER BY 2 DESC;

SELECT id, source, status, to_char(started_at, 'MM-DD HH24:MI') AS started,
       to_char(finished_at - started_at, 'HH24:MI:SS') AS took,
       discovered, inserted, updated, duplicates, errors
FROM source_runs ORDER BY id DESC LIMIT 12;
SQL

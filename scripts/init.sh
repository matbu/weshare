#!/usr/bin/env bash
# Initialise (or update) the World Webcams stack on a Docker host. Idempotent: safe to re-run.
#
#   scripts/init.sh                          # generate .env if missing, build, start, check
#   ADMIN_EMAIL=me@example.com scripts/init.sh   # + create/promote an admin account
#   scripts/init.sh --no-build               # restart without rebuilding images
#
# Variables used only when .env is generated (otherwise edit .env):
#   DATA_DIR (/data/webcams)  DOMAIN (:80)  HTTP_PUBLISH (80)  HTTPS_PUBLISH (443)
#   PG_PUBLISH (127.0.0.1:5432)  BOT_CONTACT (url or e-mail put in the bot User-Agent)
#   PG_SHARED_BUFFERS  PG_EFFECTIVE_CACHE_SIZE  API_WORKERS  WINDY_API_KEY
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

# ---------------------------------------------------------------------------
step "Prerequisites"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is missing"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is $(whoami) in the docker group?)"
command -v openssl >/dev/null || die "openssl is required to generate secrets"
ok "docker $(docker version --format '{{.Server.Version}}'), compose $(docker compose version --short)"

# ---------------------------------------------------------------------------
step "Configuration (.env)"
if [[ -f .env ]]; then
  ok ".env already exists, kept as is"
else
  contact=${BOT_CONTACT:-}
  [[ -n "$contact" ]] || warn "BOT_CONTACT not set: add a real URL/e-mail to BOT_USER_AGENT in .env (never a placeholder like example.com: Overpass rejects it with HTTP 406)"
  cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
DOMAIN=${DOMAIN:-:80}
DATA_DIR=${DATA_DIR:-/data/webcams}
HTTP_PUBLISH=${HTTP_PUBLISH:-80}
HTTPS_PUBLISH=${HTTPS_PUBLISH:-443}
PG_PUBLISH=${PG_PUBLISH:-127.0.0.1:5432}
BOT_USER_AGENT=WorldWebcamsBot/1.0${contact:+ (+$contact)}
WINDY_API_KEY=${WINDY_API_KEY:-}
CORS_ORIGINS=*
LOG_LEVEL=INFO
API_WORKERS=${API_WORKERS:-2}
PG_SHARED_BUFFERS=${PG_SHARED_BUFFERS:-512MB}
PG_EFFECTIVE_CACHE_SIZE=${PG_EFFECTIVE_CACHE_SIZE:-1536MB}
EOF
  chmod 600 .env
  ok ".env generated with random secrets (mode 600)"
fi
for key in POSTGRES_PASSWORD JWT_SECRET; do
  value=$(env_get "$key")
  [[ -n "$value" && "$value" != change-me* ]] || die "$key must be set in .env"
done
docker compose config --quiet || die "compose.yaml / .env are invalid"
ok "compose configuration valid"

# ---------------------------------------------------------------------------
step "Data directories"
data_dir=$(env_get DATA_DIR)
data_dir=${data_dir:-/data/webcams}
for d in "$data_dir/postgres" "$data_dir/caddy"; do
  if [[ ! -d "$d" ]]; then
    mkdir -p "$d" 2>/dev/null || sudo mkdir -p "$d"
  fi
done
ok "$data_dir"

# ---------------------------------------------------------------------------
if (( BUILD )); then
  step "Building images"
  docker compose build --pull
fi

step "Database"
docker compose up -d --wait postgres >/dev/null
ok "postgres healthy"

step "Database migrations"
# Before touching api/collector: if a migration fails, the running version keeps serving.
if docker compose run --rm -T migrate >/tmp/webcams-migrate.log 2>&1; then
  grep -E "^Applied" /tmp/webcams-migrate.log | sed 's/^/    /' || true
  ok "schema up to date"
else
  tail -20 /tmp/webcams-migrate.log
  die "migrations failed: api and collector were left untouched"
fi

step "Starting the stack"
docker compose up -d --remove-orphans

step "Health checks"
for _ in $(seq 1 60); do
  if docker compose exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" 2>/dev/null; then
    ok "API answers /health"
    break
  fi
  sleep 2
done || true
docker compose exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" 2>/dev/null \
  || { docker compose logs api | tail -30; die "API is not healthy"; }

http_publish=$(env_get HTTP_PUBLISH)
http_publish=${http_publish:-80}
[[ "$http_publish" == *:* ]] && web="http://$http_publish" || web="http://localhost:$http_publish"
if command -v curl >/dev/null; then
  code=$(curl -s -o /dev/null -w '%{http_code}' "$web/api/stats" || true)
  [[ "$code" == 200 ]] && ok "web entrypoint $web/api/stats -> 200" || warn "$web/api/stats returned '$code' (DOMAIN set to a real name? then use https://DOMAIN)"
  code=$(curl -s -o /dev/null -w '%{http_code}' "$web/" || true)
  [[ "$code" == 200 ]] && ok "website $web/ -> 200" || warn "$web/ returned '$code'"
fi

[[ "$(docker compose ps collector --format '{{.State}}')" == running ]] \
  && ok "collector running" || { docker compose logs collector | tail -30; die "collector is not running"; }

# ---------------------------------------------------------------------------
if [[ -n "${ADMIN_EMAIL:-}" ]]; then
  step "Admin account"
  admin_email=$(tr '[:upper:]' '[:lower:]' <<<"$ADMIN_EMAIL")
  admin_password=${ADMIN_PASSWORD:-$(openssl rand -base64 18)}
  status=$(docker compose exec -T -e EMAIL="$admin_email" -e PASSWORD="$admin_password" api python -c '
import json, os, urllib.request, urllib.error
req = urllib.request.Request("http://localhost:8000/auth/register", method="POST",
    data=json.dumps({"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}).encode(),
    headers={"Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)')
  docker compose exec -T postgres psql -q -U webcams -d webcams \
    -c "UPDATE users SET role = 'admin' WHERE email = '${admin_email//\'/}'" >/dev/null
  if [[ "$status" == 201 ]]; then
    ok "admin created: $admin_email"
    [[ -n "${ADMIN_PASSWORD:-}" ]] || ok "generated password: $admin_password  (change it after first login)"
  else
    ok "account $admin_email already existed, promoted to admin (password unchanged)"
  fi
fi

# ---------------------------------------------------------------------------
step "Done"
cat <<EOF
    Website       $web/
    API docs      $web/api/docs
    Stats         $web/api/stats

    The collector starts the world OSM import by itself (about 30-60 min), then health
    checks every 10 min and page discovery every 30 min. Follow it with:

      cd $ROOT && docker compose logs -f collector
      cd $ROOT && scripts/status.sh
EOF

#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# Config (edit if you need to)
# -----------------------------
API_PORT="${API_PORT:-8080}"
LLAMA_PORT="${LLAMA_PORT:-8081}"
PG_PORT="${PG_PORT:-5432}"
REDIS_PORT="${REDIS_PORT:-6379}"
MINIO_PORT_RANGE="${MINIO_PORT_RANGE:-9000-9001}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_ROOT}/.ENV"
BASE="http://127.0.0.1:${API_PORT}"

# -----------------------------
# Helpers
# -----------------------------
log() { printf "\033[1;36m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[!]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[✗]\033[0m %s\n" "$*"; }

wait_for_port() {
  local host="${1}" port="${2}" name="${3:-service}" secs="${4:-60}"
  for i in $(seq 1 "$secs"); do
    if nc -z "${host}" "${port}" >/dev/null 2>&1; then
      log "${name} is ready on ${host}:${port}"
      return 0
    fi
    sleep 1
  done
  err "Timeout waiting for ${name} on ${host}:${port}"
  return 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing required command: $1"; exit 1; }
}

# -----------------------------
# 0) Basic prerequisites
# -----------------------------
require_cmd docker
require_cmd jq
require_cmd psql
require_cmd nc

# -----------------------------
# 1) Ensure Docker daemon is up
# -----------------------------
ensure_docker() {
  if docker info >/dev/null 2>&1; then
    log "Docker daemon reachable."
    return 0
  fi

  warn "Docker daemon not reachable. Trying Colima…"
  if command -v colima >/dev/null 2>&1; then
    docker context use colima >/dev/null 2>&1 || true
    if ! colima status >/dev/null 2>&1; then
      log "Starting Colima…"
      colima start --cpu 4 --memory 8 --disk 60 >/dev/null
    fi
    # Re-check
    if docker info >/dev/null 2>&1; then
      log "Docker via Colima is up."
      return 0
    fi
  fi

  warn "Colima not available or failed. Trying Docker Desktop (macOS)…"
  if [[ "$OSTYPE" == "darwin"* ]]; then
    open -a Docker || true
    # Wait up to 60s
    for _ in $(seq 1 60); do
      if docker info >/dev/null 2>&1; then
        log "Docker Desktop is up."
        return 0
      fi
      sleep 1
    done
  fi

  err "Could not reach a Docker daemon. Start Docker Desktop or Colima and rerun."
  exit 1
}
ensure_docker

# -----------------------------
# 2) Load environment
# -----------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  err "Missing .ENV at $ENV_FILE"
  exit 1
fi
# shellcheck disable=SC2046
set -a; source "$ENV_FILE"; set +a

if [[ -z "${DATABASE_URL:-}" ]]; then
  err "DATABASE_URL not set in .ENV"
  exit 1
fi

# -----------------------------
# 3) Kill anything bound to app ports (optional)
# -----------------------------
log "Freeing ports :${API_PORT} and :${LLAMA_PORT} (if occupied)…"
lsof -ti tcp:${API_PORT} | xargs -r kill -9 || true
lsof -ti tcp:${LLAMA_PORT} | xargs -r kill -9 || true

# -----------------------------
# 4) Restart infra (compose)
# -----------------------------
log "Recreating docker infra (Postgres/Redis/MinIO)…"
./dev down || true
./dev up

# Confirm containers we expect
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | grep -E 'infra-(postgres|redis|minio)-1' || {
  err "Infra containers did not start correctly."
  exit 1
}

# Wait for Postgres and Redis containers to accept TCP
wait_for_port 127.0.0.1 "${PG_PORT}" "Postgres"
wait_for_port 127.0.0.1 "${REDIS_PORT}" "Redis"

# -----------------------------
# 5) (Re)create database & schema
# -----------------------------
log "Creating database (agentstack) if missing…"
PGURL="postgresql://postgres:postgres@127.0.0.1:${PG_PORT}/postgres"
DBNAME="agentstack"
psql "$PGURL" -v ON_ERROR_STOP=1 -c "DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${DBNAME}') THEN
    PERFORM dblink_exec('dbname=' || current_database(), ''); -- noop
    EXECUTE format('CREATE DATABASE %I', '${DBNAME}');
  END IF;
END \$\$;" >/dev/null 2>&1 || true

# Ensure extensions/tables/seeds — Non-minimal full reset
log "Applying SQL schema & seed…"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- Core tables (users, agents, tools, agent_tools, files, usage)
CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email CITEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT 'local-chat',
  temperature REAL NOT NULL DEFAULT 0.2,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS tools (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,               -- 'rag.search', 'http', etc.
  config JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS agent_tools (
  agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  tool_id  UUID NOT NULL REFERENCES tools(id)  ON DELETE CASCADE,
  PRIMARY KEY (agent_id, tool_id)
);

-- Optional RAG store registry (files)
CREATE TABLE IF NOT EXISTS rag_files (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  chunk_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Usage ledger (optional)
CREATE TABLE IF NOT EXISTS usage_events (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, -- 'chat', 'embed', 'tool.run'
  tokens INT NOT NULL DEFAULT 0,
  meta JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Global catalog & templates
CREATE TABLE IF NOT EXISTS global_tools_catalog (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_templates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT 'local-chat',
  temperature REAL NOT NULL DEFAULT 0.2,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_template_tools (
  template_id UUID NOT NULL REFERENCES agent_templates(id) ON DELETE CASCADE,
  tool_id UUID NOT NULL REFERENCES global_tools_catalog(id) ON DELETE CASCADE,
  PRIMARY KEY (template_id, tool_id)
);

-- Seed global tool catalog (idempotent)
INSERT INTO global_tools_catalog (name, kind, config) VALUES
  ('weather-now', 'http', '{"url":"https://wttr.in/{{city}}?format=j1","method":"GET"}'),
  ('btc-price',   'http', '{"url":"https://api.coindesk.com/v1/bpi/currentprice.json","method":"GET"}'),
  ('hn-top',      'http', '{"url":"https://hn.algolia.com/api/v1/search?tags=front_page","method":"GET"}'),
  ('my-doc-search','rag.search','{"collection":"default"}')
ON CONFLICT (name) DO NOTHING;

-- Seed Jeeves template
INSERT INTO agent_templates (slug, name, system_prompt, model, temperature)
VALUES (
  'jeeves',
  'jeeves',
  'You are Jeeves, a concise assistant. Prefer installed tools for live data and document search.',
  'local-chat',
  0.2
)
ON CONFLICT (slug) DO NOTHING;

-- Link Jeeves => tools
WITH tpl AS (SELECT id FROM agent_templates WHERE slug='jeeves'),
     t AS (SELECT id,name FROM global_tools_catalog WHERE name IN ('weather-now','btc-price','hn-top','my-doc-search'))
INSERT INTO agent_template_tools (template_id, tool_id)
SELECT tpl.id, t.id FROM tpl, t
ON CONFLICT DO NOTHING;
SQL

# -----------------------------
# 6) Start API & llama.cpp (project's existing launcher)
# -----------------------------
log "Launching llama.cpp + API via ./dev up (already running should be fine)…"
# ./dev up already executed infra & llama/api in your setup; if you have separate commands, call them here.

# Wait for API + llama.cpp
wait_for_port 127.0.0.1 "${LLAMA_PORT}" "llama.cpp" 120 || true
wait_for_port 127.0.0.1 "${API_PORT}" "API" 120

# -----------------------------
# 7) Create a seed user (API) and/or via SQL fallback
# -----------------------------
SEED_EMAIL="seed+$(date +%s)@example.org"
SEED_PASS="Passw0rd!"
log "Creating seed user via API… (${SEED_EMAIL})"
JWT="$(curl -s -X POST "${BASE}/v1/auth/signup" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${SEED_EMAIL}\",\"password_hash\":\"${SEED_PASS}\",\"name\":\"Seeded User\"}" | jq -r '.jwt // .access_token // empty' || true)"

if [[ -z "${JWT}" ]]; then
  warn "API signup failed; falling back to direct SQL user insert…"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO users (email, password_hash, name)
VALUES (lower('${SEED_EMAIL}'), crypt('${SEED_PASS}', gen_salt('bf', 12)), 'Seeded User')
ON CONFLICT (email) DO NOTHING;
SQL
  # Try API login again
  JWT="$(curl -s -X POST "${BASE}/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"${SEED_EMAIL}\",\"password_hash\":\"${SEED_PASS}\"}" | jq -r '.jwt // .access_token // empty' || true)"
fi

if [[ -z "${JWT}" ]]; then
  err "Could not obtain JWT. Check API logs."
  exit 1
fi
log "JWT acquired."

# -----------------------------
# 8) Provision the user from Jeeves template
# -----------------------------
log "Provisioning default agent + tools from template (via admin endpoint if available)…"
if curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/v1/admin/provision" -H "Authorization: Bearer ${JWT}" | grep -qE '^(200|204)$'; then
  log "Admin provision endpoint succeeded."
else
  warn "Admin provision endpoint missing/failed; provisioning directly in SQL…"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
  the_user_id UUID;
  tpl RECORD;
  tool RECORD;
  agent_id UUID;
  cfg JSONB;
BEGIN
  SELECT id INTO the_user_id FROM users ORDER BY created_at DESC LIMIT 1;

  SELECT id, name, system_prompt, model, temperature INTO tpl
  FROM agent_templates WHERE slug='jeeves' LIMIT 1;

  IF NOT FOUND THEN
    RAISE NOTICE 'Template jeeves not found; skipping user provision.';
    RETURN;
  END IF;

  INSERT INTO agents (user_id, name, system_prompt, model, temperature)
  VALUES (the_user_id, tpl.name, tpl.system_prompt, tpl.model, tpl.temperature)
  ON CONFLICT (user_id, name) DO UPDATE
    SET system_prompt=EXCLUDED.system_prompt,
        model=EXCLUDED.model,
        temperature=EXCLUDED.temperature,
        updated_at=now()
  RETURNING id INTO agent_id;

  FOR tool IN
    SELECT g.name, g.kind, g.config
    FROM agent_template_tools att
    JOIN global_tools_catalog g ON g.id = att.tool_id
    WHERE att.template_id = tpl.id
  LOOP
    -- Simple {{city}} macro -> chicago
    cfg := to_jsonb( tool.config )::jsonb;
    cfg := to_jsonb( replace(cfg::text, '{{city}}', 'chicago') )::jsonb;

    INSERT INTO tools (user_id, name, kind, config)
    VALUES (the_user_id, tool.name, tool.kind, cfg)
    ON CONFLICT (user_id, name) DO UPDATE SET kind=EXCLUDED.kind, config=EXCLUDED.config
    RETURNING id INTO tool.id;

    INSERT INTO agent_tools (agent_id, tool_id)
    VALUES (agent_id, tool.id)
    ON CONFLICT DO NOTHING;
  END LOOP;
END$$;
SQL
fi

# -----------------------------
# 9) Smoke tests
# -----------------------------
log "Smoke: /v1/agents"
curl -s "${BASE}/v1/agents" -H "Authorization: Bearer ${JWT}" | jq

log "Smoke: /v1/tools"
curl -s "${BASE}/v1/tools" -H "Authorization: Bearer ${JWT}" | jq

log "Smoke: run 'weather-now' (Chicago)"
curl -s -X POST "${BASE}/v1/tools/run" \
  -H "Authorization: Bearer ${JWT}" -H "Content-Type: application/json" \
  -d '{"tool":"weather-now","args":{"city":"chicago"}}' | jq || true

log "Smoke: run 'btc-price'"
curl -s -X POST "${BASE}/v1/tools/run" \
  -H "Authorization: Bearer ${JWT}" -H "Content-Type: application/json" \
  -d '{"tool":"btc-price","args":{}}' | jq || true

log "All done."

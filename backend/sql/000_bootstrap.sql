-- backend/sql/000_bootstrap.sql
-- One-shot schema + seed that matches your code paths

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Users (auth depends on this)
CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agents (per-user)
CREATE TABLE IF NOT EXISTS agents (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT 'local-chat',
  temperature   REAL NOT NULL DEFAULT 0.2,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- one agent name per user (unique index)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND indexname = 'agents_user_id_name_key'
  ) THEN
    CREATE UNIQUE INDEX agents_user_id_name_key ON agents(user_id, name);
  END IF;
END$$;

-- Tools (per-user)
CREATE TABLE IF NOT EXISTS tools (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- 'http' | 'rag.search' | etc
  config     JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- one tool name per user
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND indexname = 'tools_user_id_name_key'
  ) THEN
    CREATE UNIQUE INDEX tools_user_id_name_key ON tools(user_id, name);
  END IF;
END$$;

-- Agent <-> tools
CREATE TABLE IF NOT EXISTS agent_tools (
  agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  tool_id  UUID NOT NULL REFERENCES tools(id)  ON DELETE CASCADE,
  PRIMARY KEY (agent_id, tool_id)
);

-- Global tool catalog (for provisioning templates)
CREATE TABLE IF NOT EXISTS global_tools_catalog (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL UNIQUE,
  kind       TEXT NOT NULL,
  config     JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent templates (global)
CREATE TABLE IF NOT EXISTS agent_templates (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT 'local-chat',
  temperature   REAL NOT NULL DEFAULT 0.2,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Template -> catalog tools mapping
CREATE TABLE IF NOT EXISTS agent_template_tools (
  template_id UUID NOT NULL REFERENCES agent_templates(id) ON DELETE CASCADE,
  tool_id     UUID NOT NULL REFERENCES global_tools_catalog(id) ON DELETE CASCADE,
  PRIMARY KEY (template_id, tool_id)
);

-- Seed the global catalog with a couple of HTTP tools you’ve used
INSERT INTO global_tools_catalog (name, kind, config) VALUES
  ('weather-now','http','{"url":"https://wttr.in/{{city}}?format=j1","method":"GET"}'),
  ('btc-price',  'http','{"url":"https://api.coindesk.com/v1/bpi/currentprice.json","method":"GET"}'),
  ('hn-top',     'http','{"url":"https://hn.algolia.com/api/v1/search?tags=front_page","method":"GET"}')
ON CONFLICT (name) DO NOTHING;

-- Seed an agent template: 'jeeves'
INSERT INTO agent_templates (slug, name, system_prompt, model, temperature)
VALUES (
  'jeeves',
  'jeeves',
  'You are Jeeves, a concise assistant. Prefer installed tools for live data and document search.',
  'local-chat',
  0.2
)
ON CONFLICT (slug) DO NOTHING;

-- Link template -> tools
WITH tpl AS (SELECT id FROM agent_templates WHERE slug='jeeves'),
     gc  AS (SELECT id,name FROM global_tools_catalog WHERE name IN ('weather-now','btc-price','hn-top'))
INSERT INTO agent_template_tools (template_id, tool_id)
SELECT tpl.id, gc.id FROM tpl, gc
ON CONFLICT DO NOTHING;

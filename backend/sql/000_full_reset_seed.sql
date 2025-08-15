-- backend/sql/000_full_reset_seed.sql
-- FULL RESET + SEED (idempotent)
-- Safe to re-run: uses IF NOT EXISTS / ON CONFLICT

-------------------------
-- EXTENSIONS
-------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-------------------------
-- CORE TABLES
-------------------------

-- Users
CREATE TABLE IF NOT EXISTS public.users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agents (per-user)
CREATE TABLE IF NOT EXISTS public.agents (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT 'local-chat',
  temperature   REAL NOT NULL DEFAULT 0.2,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique: one agent name per user
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public' AND indexname='agents_user_id_name_key'
  ) THEN
    CREATE UNIQUE INDEX agents_user_id_name_key ON public.agents(user_id, name);
  END IF;
END$$;

-- Tools (per-user)
CREATE TABLE IF NOT EXISTS public.tools (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- 'http' | 'rag.search' | 'gmail' | ...
  config     JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique: one tool name per user
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public' AND indexname='tools_user_id_name_key'
  ) THEN
    CREATE UNIQUE INDEX tools_user_id_name_key ON public.tools(user_id, name);
  END IF;
END$$;

-- Agent <-> Tools (many-to-many)
CREATE TABLE IF NOT EXISTS public.agent_tools (
  agent_id UUID NOT NULL REFERENCES public.agents(id) ON DELETE CASCADE,
  tool_id  UUID NOT NULL REFERENCES public.tools(id)  ON DELETE CASCADE,
  PRIMARY KEY (agent_id, tool_id)
);

-- RAG file manifest (for listing/searching sources)
-- (Pairs with your filesystem/vector store; keeps counts & source names)
CREATE TABLE IF NOT EXISTS public.rag_files (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,               -- filename or logical source
  chunks      INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public' AND indexname='rag_files_user_id_source_key'
  ) THEN
    CREATE UNIQUE INDEX rag_files_user_id_source_key ON public.rag_files(user_id, source);
  END IF;
END$$;

-------------------------
-- GLOBAL CATALOG + TEMPLATES
-------------------------

-- Global tool catalog (for provisioning)
CREATE TABLE IF NOT EXISTS public.global_tools_catalog (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL UNIQUE,   -- catalog name handle
  kind       TEXT NOT NULL,          -- 'http', 'rag.search', 'gmail', 'calendar', ...
  config     JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent templates (global)
CREATE TABLE IF NOT EXISTS public.agent_templates (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          TEXT NOT NULL UNIQUE,       -- 'jeeves'
  name          TEXT NOT NULL,              -- default agent name (per-user unique)
  system_prompt TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT 'local-chat',
  temperature   REAL NOT NULL DEFAULT 0.2,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Template -> Catalog tools mapping
CREATE TABLE IF NOT EXISTS public.agent_template_tools (
  template_id UUID NOT NULL REFERENCES public.agent_templates(id) ON DELETE CASCADE,
  tool_id     UUID NOT NULL REFERENCES public.global_tools_catalog(id) ON DELETE CASCADE,
  PRIMARY KEY (template_id, tool_id)
);

-------------------------
-- SEED GLOBAL CATALOG
-------------------------

-- Popular HTTP tools
INSERT INTO public.global_tools_catalog (name, kind, config) VALUES
  ('weather-now', 'http', '{"url":"https://wttr.in/{{city}}?format=j1","method":"GET"}'),
  ('btc-price',   'http', '{"url":"https://api.coindesk.com/v1/bpi/currentprice.json","method":"GET"}'),
  ('hn-top',      'http', '{"url":"https://hn.algolia.com/api/v1/search?tags=front_page","method":"GET"}')
ON CONFLICT (name) DO NOTHING;

-- Built-in RAG search tool (no external URL)
INSERT INTO public.global_tools_catalog (name, kind, config) VALUES
  ('my-doc-search', 'rag.search', '{"collection":"default"}')
ON CONFLICT (name) DO NOTHING;

-- Gmail / Calendar (placeholders; your code treats kind='http' and others in tool runtime)
INSERT INTO public.global_tools_catalog (name, kind, config) VALUES
  ('gmail-search',   'gmail',    '{"scopes":["gmail.readonly"]}'),
  ('calendar-list',  'calendar', '{"scopes":["calendar.readonly"]}')
ON CONFLICT (name) DO NOTHING;

-------------------------
-- SEED TEMPLATE: JEEVES
-------------------------
INSERT INTO public.agent_templates (slug, name, system_prompt, model, temperature)
VALUES (
  'jeeves',
  'jeeves',
  'You are Jeeves, a concise assistant. Prefer installed tools for live data and document search. For questions that can be answered from uploaded files, call the RAG search tool.',
  'local-chat',
  0.2
)
ON CONFLICT (slug) DO NOTHING;

-- Link Jeeves to a curated set of tools
WITH tpl AS (SELECT id FROM public.agent_templates WHERE slug='jeeves'),
     gc  AS (
        SELECT id, name FROM public.global_tools_catalog
        WHERE name IN ('weather-now','btc-price','hn-top','my-doc-search')
     )
INSERT INTO public.agent_template_tools (template_id, tool_id)
SELECT tpl.id, gc.id FROM tpl, gc
ON CONFLICT DO NOTHING;

-------------------------
-- USEFUL INDEXES
-------------------------
-- Speed up lookups
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='idx_agents_user_id') THEN
    CREATE INDEX idx_agents_user_id ON public.agents(user_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='idx_tools_user_id') THEN
    CREATE INDEX idx_tools_user_id ON public.tools(user_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='idx_agent_tools_agent') THEN
    CREATE INDEX idx_agent_tools_agent ON public.agent_tools(agent_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='idx_agent_tools_tool') THEN
    CREATE INDEX idx_agent_tools_tool ON public.agent_tools(tool_id);
  END IF;
END $$;

-------------------------
-- DEMO/OPS VIEW (optional)
-------------------------
CREATE OR REPLACE VIEW public.v_user_overview AS
SELECT
  u.id                        AS user_id,
  u.email,
  COUNT(DISTINCT a.id)        AS agents,
  COUNT(DISTINCT t.id)        AS tools,
  COALESCE(SUM(rf.chunks),0)  AS rag_chunks
FROM public.users u
LEFT JOIN public.agents a ON a.user_id = u.id
LEFT JOIN public.tools  t ON t.user_id  = u.id
LEFT JOIN public.rag_files rf ON rf.user_id = u.id
GROUP BY u.id, u.email
ORDER BY u.created_at DESC;

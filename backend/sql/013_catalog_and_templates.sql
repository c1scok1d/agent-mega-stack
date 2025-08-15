-- 013_catalog_and_templates.sql  (schema only)

-- Ensure pgcrypto for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Global catalog of tools (replaces any old table names)
CREATE TABLE IF NOT EXISTS global_tools_catalog (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,           -- 'http', 'rag.search', etc.
  config       JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent templates (global) e.g., "jeeves"
CREATE TABLE IF NOT EXISTS agent_templates (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL UNIQUE,
  system_prompt TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT 'local-chat',
  temperature   REAL NOT NULL DEFAULT 0.2,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- M:N template <-> tools
CREATE TABLE IF NOT EXISTS agent_template_tools (
  template_id UUID NOT NULL REFERENCES agent_templates(id) ON DELETE CASCADE,
  tool_id     UUID NOT NULL REFERENCES global_tools_catalog(id) ON DELETE CASCADE,
  PRIMARY KEY (template_id, tool_id)
);

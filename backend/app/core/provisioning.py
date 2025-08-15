from __future__ import annotations
import json
import os
import psycopg
from psycopg.rows import dict_row

DEFAULT_TEMPLATE_SLUG = os.getenv("DEFAULT_AGENT_TEMPLATE", "jeeves")
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "chicago")

def _cfg_city(cfg: dict, city: str) -> dict:
    try:
        s = json.dumps(cfg or {})
        return json.loads(s.replace("{{city}}", city))
    except Exception:
        return cfg or {}

def provision_user_defaults(conn: psycopg.Connection, the_user_id: str,
                            template_slug: str | None = None,
                            default_city: str | None = None) -> str | None:
    """
    Clone template agent + its tools into this user's scope.
    Idempotent. Returns agent_id or None if template not found.
    """
    slug = (template_slug or DEFAULT_TEMPLATE_SLUG).strip()
    city = (default_city or DEFAULT_CITY).strip()

    with conn.cursor(row_factory=dict_row) as cur:
        # 1) template
        cur.execute(
            "SELECT id, name, system_prompt, model, temperature "
            "FROM agent_templates WHERE slug = %s LIMIT 1",
            (slug,)
        )
        tpl = cur.fetchone()
        if not tpl:
            return None

        # 2) upsert agent for user (name unique per user)
        cur.execute(
            """
            INSERT INTO agents (user_id, name, system_prompt, model, temperature)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, name) DO UPDATE
              SET system_prompt = EXCLUDED.system_prompt,
                  model         = EXCLUDED.model,
                  temperature   = EXCLUDED.temperature,
                  updated_at    = now()
            RETURNING id
            """,
            (the_user_id, tpl["name"], tpl["system_prompt"], tpl["model"], tpl["temperature"])
        )
        agent_id = cur.fetchone()["id"]

        # 3) template tools
        cur.execute(
            """
            SELECT g.id   AS catalog_id,
                   g.name AS name,
                   g.kind AS kind,
                   g.config AS config
            FROM agent_template_tools att
            JOIN global_tools_catalog g ON g.id = att.tool_id
            WHERE att.template_id = %s
            """,
            (tpl["id"],)
        )
        tools = cur.fetchall()

        # 4) upsert user tools
        for t in tools:
            cfg = _cfg_city(t["config"], city)
            cur.execute(
                """
                INSERT INTO tools (user_id, name, kind, config)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (user_id, name) DO UPDATE
                  SET kind   = EXCLUDED.kind,
                      config = EXCLUDED.config
                RETURNING id
                """,
                (the_user_id, t["name"], t["kind"], json.dumps(cfg))
            )
            tool_id = cur.fetchone()["id"]

            # 5) link agent->tool
            cur.execute(
                """
                INSERT INTO agent_tools (agent_id, tool_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (agent_id, tool_id)
            )

        conn.commit()
        return agent_id

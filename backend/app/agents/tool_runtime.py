# backend/app/agents/tool_runtime.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from psycopg.rows import dict_row


from app.agents.http_tool import HttpTool  # you already have this
from app.rag.index import rag_search
from app.core.db import get_conn


class ToolProto:
    """Tiny protocol: tool objects only need .name and .run(**kwargs)."""
    name: str
    def run(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class RagSearchTool(ToolProto):
    name = "rag.search"

    def __init__(self, user_id: str):
        self.user_id = user_id

    def run(self, query: str, k: int = 4, **_: Any):
        # returns [{text, metadata}, ...]
        return rag_search(self.user_id, query=query, k=k)


def _ensure_dict(cfg: Any) -> dict:
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return cfg
    # Accept JSON string in DB
    if isinstance(cfg, str):
        try:
            return json.loads(cfg)
        except Exception:
            return {}
    return {}


def build_tools_for_user(
    user_id: str,
    *,
    include_defaults: bool = True,
    tool_rows: Optional[List[dict]] = None,
) -> Dict[str, ToolProto]:
    """
    Build the tool map for this user.

    - include_defaults: add built-in tools (currently rag.search)
    - tool_rows: rows from DB (each row must include 'kind', 'name', 'config')
    """
    tools: Dict[str, ToolProto] = {}

    # 0) Load DB rows on demand if not provided
    if tool_rows is None:
        with get_conn(cursor_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT name, kind, config
                    FROM tools
                    WHERE user_id = %s
                    ORDER BY name
                    """,
                    (user_id,),
                )
                tool_rows = cur.fetchall()

    # 1) built-ins
    if include_defaults:
        tools["rag.search"] = RagSearchTool(user_id)

    # 2) DB-driven tools
    for row in tool_rows or ():
        kind = row.get("kind")
        name = row.get("name")
        cfg = _ensure_dict(row.get("config"))

        if not kind or not name:
            continue

        if kind == "http":
            # HttpTool should implement .run(**kwargs)
            tools[name] = HttpTool(cfg)
        elif kind == "rag.search":
            # allow custom-named alias of rag.search if desired
            tools[name] = RagSearchTool(user_id)
        # elif kind == "your_future_kind": tools[name] = ...
        else:
            # unknown kind: skip silently or log
            continue

    return tools

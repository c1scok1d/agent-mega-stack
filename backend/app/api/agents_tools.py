# app/api/agents_tools.py
from __future__ import annotations

import uuid
from typing import List, Optional

# app/api/agents_tools.py
from app.core.db import get_conn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from psycopg.rows import dict_row
from app.core.db import get_conn  # uses psycopg3 connection factory returning dict rows
from app.core.security import Authed, get_current_user
from app.billing.deps import require_active_subscription, record_usage
from app.billing.limits import enforce_rate_limit

from app.agents.graph import make_graph
from app.memory.history import trim_and_summarize

# If you created a helper to build tool instances from DB rows:
#   from app.agents.build_tools import build_tools_for_user
# If not, we'll include a tiny fallback for RAG only:
from app.rag.index import rag_search

router = APIRouter()

# ---------------------------
# Pydantic models
# ---------------------------

class ToolIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field(..., min_length=1, max_length=64)  # e.g. "rag.search"
    config: dict = Field(default_factory=dict)

class ToolOut(BaseModel):
    id: str
    name: str
    kind: str
    config: dict
    created_at: str

class AgentIn(BaseModel):
    name: str
    system_prompt: str = ""
    model: str = "local-chat"
    temperature: float = 0.2

class AgentPatch(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None

class AgentOut(BaseModel):
    id: str
    name: str
    system_prompt: str
    model: str
    temperature: float
    created_at: str
    updated_at: str

class AgentToolLinkIn(BaseModel):
    agent_id: str
    tool_id: str

class ChatIn(BaseModel):
    # single-turn convenience
    message: Optional[str] = None
    # or multi-turn
    messages: Optional[List[dict]] = None
    session_id: str = "default"

# ---------------------------
# Tools builder (minimal fallback)
# ---------------------------

class _RagTool:
    """Simple callable wrapper so graph can `t.run(query=...)`."""
    name: str = "rag.search"
    def __init__(self, user_id: str, config: dict | None = None):
        self.user_id = user_id

    def run(self, query: str, k: int = 4):
        docs = rag_search(self.user_id, query=query, k=k)
        return {"results": docs}

def _build_tools_for_user(user_id: str, tool_rows: List[dict]) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for t in tool_rows:
        kind = t["kind"]
        name = t["name"]
        cfg = t.get("config") or {}
        if kind == "rag.search":
            mapping[name] = _RagTool(user_id, cfg)
        # future kinds: http, code, etc.
    return mapping

# ---------------------------
# Tools: create/list/delete
# ---------------------------

@router.post("/v1/tools", response_model=ToolOut)
def create_tool(body: ToolIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tools (user_id, name, kind, config)
            VALUES (%s, %s, %s, %s)
            RETURNING id, name, kind, config, created_at
            """,
            (user.user_id, body.name, body.kind, body.config),
        )
        row = cur.fetchone()
        conn.commit()
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "kind": row["kind"],
        "config": row["config"],
        "created_at": row["created_at"].isoformat(),
    }

@router.get("/v1/tools", response_model=List[ToolOut])
def list_tools(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, kind, config, created_at
               FROM tools WHERE user_id=%s
               ORDER BY created_at DESC""",
            (user.user_id,),
        )
        rows = cur.fetchall()
    out: List[ToolOut] = []
    for r in rows:
        out.append(
            ToolOut(
                id=str(r["id"]),
                name=r["name"],
                kind=r["kind"],
                config=r["config"],
                created_at=r["created_at"].isoformat(),
            )
        )
    return out

@router.delete("/v1/tools/{tool_id}")
def delete_tool(tool_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tools WHERE id=%s AND user_id=%s RETURNING 1", (tool_id, user.user_id))
        ok = cur.fetchone()
        conn.commit()
    if not ok:
        raise HTTPException(404, "Tool not found")
    return {"ok": True}

# ---------------------------
# Agents: create/list/get/patch/delete
# ---------------------------

@router.post("/v1/agents", response_model=AgentOut)
def create_agent(body: AgentIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agents (user_id, name, system_prompt, model, temperature)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, name, system_prompt, model, temperature, created_at, updated_at
            """,
            (user.user_id, body.name, body.system_prompt, body.model, body.temperature),
        )
        row = cur.fetchone()
        conn.commit()
    return AgentOut(
        id=str(row["id"]),
        name=row["name"],
        system_prompt=row["system_prompt"],
        model=row["model"],
        temperature=float(row["temperature"]),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )

@router.get("/v1/agents", response_model=List[AgentOut])
def list_agents(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, system_prompt, model, temperature, created_at, updated_at
               FROM agents WHERE user_id=%s
               ORDER BY created_at DESC""",
            (user.user_id,),
        )
        rows = cur.fetchall()
    return [
        AgentOut(
            id=str(r["id"]),
            name=r["name"],
            system_prompt=r["system_prompt"],
            model=r["model"],
            temperature=float(r["temperature"]),
            created_at=r["created_at"].isoformat(),
            updated_at=r["updated_at"].isoformat(),
        )
        for r in rows
    ]

@router.get("/v1/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, system_prompt, model, temperature, created_at, updated_at
               FROM agents WHERE id=%s AND user_id=%s""",
            (agent_id, user.user_id),
        )
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "Agent not found")
    return AgentOut(
        id=str(r["id"]),
        name=r["name"],
        system_prompt=r["system_prompt"],
        model=r["model"],
        temperature=float(r["temperature"]),
        created_at=r["created_at"].isoformat(),
        updated_at=r["updated_at"].isoformat(),
    )

@router.patch("/v1/agents/{agent_id}", response_model=AgentOut)
def patch_agent(agent_id: str, body: AgentPatch, user: Authed = Depends(get_current_user)):
    fields = []
    values = []
    if body.name is not None:
        fields.append("name=%s"); values.append(body.name)
    if body.system_prompt is not None:
        fields.append("system_prompt=%s"); values.append(body.system_prompt)
    if body.model is not None:
        fields.append("model=%s"); values.append(body.model)
    if body.temperature is not None:
        fields.append("temperature=%s"); values.append(body.temperature)
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields.append("updated_at=now()")
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            f"""UPDATE agents SET {', '.join(fields)}
                WHERE id=%s AND user_id=%s
                RETURNING id, name, system_prompt, model, temperature, created_at, updated_at""",
            (*values, agent_id, user.user_id),
        )
        r = cur.fetchone()
        conn.commit()
    if not r:
        raise HTTPException(404, "Agent not found")
    return AgentOut(
        id=str(r["id"]),
        name=r["name"],
        system_prompt=r["system_prompt"],
        model=r["model"],
        temperature=float(r["temperature"]),
        created_at=r["created_at"].isoformat(),
        updated_at=r["updated_at"].isoformat(),
    )

@router.delete("/v1/agents/{agent_id}")
def delete_agent(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agents WHERE id=%s AND user_id=%s RETURNING 1", (agent_id, user.user_id))
        ok = cur.fetchone()
        conn.commit()
    if not ok:
        raise HTTPException(404, "Agent not found")
    return {"ok": True}

# ---------------------------
# Agent <-> Tools linking
# ---------------------------

@router.post("/v1/agent-tools")
def attach_tool(body: AgentToolLinkIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        # Ensure both belong to user
        cur.execute("SELECT 1 FROM agents WHERE id=%s AND user_id=%s", (body.agent_id, user.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Agent not found")
        cur.execute("SELECT 1 FROM tools WHERE id=%s AND user_id=%s", (body.tool_id, user.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Tool not found")
        # Link (ignore if exists)
        cur.execute(
            """INSERT INTO agent_tools (agent_id, tool_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (body.agent_id, body.tool_id),
        )
        conn.commit()
    return {"ok": True}

@router.delete("/v1/agents/{agent_id}/tools/{tool_id}")
def detach_tool(agent_id: str, tool_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        # check ownership
        cur.execute("SELECT 1 FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Agent not found")
        cur.execute("DELETE FROM agent_tools USING tools\
                     WHERE agent_tools.agent_id=%s\
                       AND agent_tools.tool_id=%s\
                       AND tools.id=agent_tools.tool_id\
                       AND tools.user_id=%s\
                     RETURNING 1",
                    (agent_id, tool_id, user.user_id))
        ok = cur.fetchone()
        conn.commit()
    if not ok:
        raise HTTPException(404, "Link not found")
    return {"ok": True}

@router.get("/v1/agents/{agent_id}/tools", response_model=List[ToolOut])
def list_agent_tools(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT t.id, t.name, t.kind, t.config, t.created_at
               FROM agent_tools at
               JOIN tools t ON t.id = at.tool_id
               JOIN agents a ON a.id = at.agent_id
              WHERE at.agent_id=%s AND a.user_id=%s
              ORDER BY t.created_at DESC""",
            (agent_id, user.user_id),
        )
        rows = cur.fetchall()
    return [
        ToolOut(
            id=str(r["id"]),
            name=r["name"],
            kind=r["kind"],
            config=r["config"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]

# ---------------------------
# Agent chat (uses attached tools)
# ---------------------------

# in-memory sessions
_SESS: dict[tuple[str, str, str], dict] = {}

@router.post("/v1/agents/{agent_id}/chat")
def chat_with_agent(
    agent_id: str,
    body: ChatIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    # Load agent
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, system_prompt, model, temperature
               FROM agents WHERE id=%s AND user_id=%s""",
            (agent_id, user.user_id),
        )
        agent = cur.fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")
        # Load attached tools
        cur.execute(
            """SELECT t.id, t.name, t.kind, t.config
               FROM agent_tools at
               JOIN tools t ON t.id = at.tool_id
              WHERE at.agent_id=%s AND t.user_id=%s""",
            (agent_id, user.user_id),
        )
        tool_rows = cur.fetchall()

    tools = _build_tools_for_user(user.user_id, tool_rows)

    # Prep state
    key = (user.user_id, agent_id, body.session_id)
    st = _SESS.get(key, {"messages": [], "summary": None})
    # If full message array provided, use it; else append single turn
    if body.messages:
        st["messages"] = body.messages
    elif body.message:
        st["messages"].append({"role": "user", "content": body.message})

    # trim/summarize memory
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    # build graph with agent-specific system prompt and temperature
    graph = make_graph(
        tools=tools,
        n_predict=limits.get("n_predict"),
        system_override=agent["system_prompt"],
        temperature=float(agent["temperature"]),
        #model_name=agent["model"],
    )

    # invoke and collect reply
    out = graph.invoke(st); _SESS[key] = out
    reply = next(m for m in reversed(out["messages"]) if m["role"] == "assistant")["content"]

    # Basic usage tally
    user_text = body.message or (body.messages[-1]["content"] if body.messages else "")
    tokens = max(1, len(user_text) // 4 + len(reply) // 4)
    record_usage(user.user_id, tokens=tokens, requests=1)

    return {"reply": reply, "agent_id": agent_id}

@router.patch("/v1/agents/{agent_id}")
def update_agent(agent_id: str, body: dict, user: Authed = Depends(get_current_user)):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        fields = []
        values = []
        for key in ("name", "system_prompt", "model", "temperature"):
            if key in body:
                fields.append(f"{key} = %s")
                values.append(body[key])
        if not fields:
            return {"updated": 0}
        values += [user.user_id, agent_id]
        cur.execute(
            f"UPDATE agents SET {', '.join(fields)}, updated_at = now() "
            "WHERE user_id = %s AND id = %s RETURNING id",
            values,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        return {"updated": 1, "id": row["id"]}


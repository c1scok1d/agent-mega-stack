from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from psycopg.rows import dict_row

from app.core.db import get_conn
from app.core.security import Authed, get_current_user
from app.agents.graph import make_graph
from app.agents.tool_runtime import build_tools_for_user
from app.memory.history import trim_and_summarize
from app.billing.deps import require_active_subscription
from app.billing.limits import enforce_rate_limit
from app.billing.deps import record_usage  # you already had record_usage usage pattern


router = APIRouter()
SESS: dict[tuple[str, str], dict] = {}

class AgentIn(BaseModel):
    name: str = Field(..., min_length=1)
    system_prompt: str = ""
    model: str = "local-chat"
    temperature: float = 0.2

class AgentOut(BaseModel):
    id: str
    name: str
    system_prompt: str
    model: str
    temperature: float

class AttachToolsIn(BaseModel):
    tool_ids: List[str]

class RunIn(BaseModel):
    session_id: str
    message: str

@router.post("/v1/agents", response_model=AgentOut)
def create_agent(body: AgentIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (user_id,name,system_prompt,model,temperature) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (user.user_id, body.name, body.system_prompt, body.model, body.temperature),
        )
        row = cur.fetchone(); conn.commit()
    return {"id": str(row["id"]), **body.model_dump()}

@router.get("/v1/agents", response_model=List[AgentOut])
def list_agents(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,name,system_prompt,model,temperature FROM agents WHERE user_id=%s ORDER BY created_at DESC", (user.user_id,))
        rows = cur.fetchall()
    for r in rows: r["id"] = str(r["id"])
    return rows

@router.get("/v1/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,name,system_prompt,model,temperature FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        r = cur.fetchone()
    if not r: raise HTTPException(404, "Not found")
    r["id"] = str(r["id"]); return r

@router.patch("/v1/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, body: AgentIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents SET name=%s, system_prompt=%s, model=%s, temperature=%s, updated_at=now() WHERE id=%s AND user_id=%s RETURNING id",
            (body.name, body.system_prompt, body.model, body.temperature, agent_id, user.user_id),
        )
        row = cur.fetchone(); conn.commit()
    if not row: raise HTTPException(404, "Not found")
    return {"id": str(row["id"]), **body.model_dump()}

@router.delete("/v1/agents/{agent_id}")
def delete_agent(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id)); conn.commit()
    return {"ok": True}

@router.post("/v1/agents/{agent_id}/tools")
def attach_tools(agent_id: str, body: AttachToolsIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        # ensure agent belongs to user
        cur.execute("SELECT 1 FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        if not cur.fetchone(): raise HTTPException(404, "Agent not found")
        # ensure tools belong to user
        for tid in body.tool_ids:
            cur.execute("SELECT 1 FROM tools WHERE id=%s AND user_id=%s", (tid, user.user_id))
            if not cur.fetchone(): raise HTTPException(400, f"Tool {tid} not found")
            cur.execute("INSERT INTO agent_tools (agent_id, tool_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (agent_id, tid))
        conn.commit()
    return {"ok": True}

@router.get("/v1/agents/{agent_id}/tools")
def list_agent_tools(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.name, t.kind, t.config, t.created_at
            FROM agent_tools at
            JOIN tools t ON t.id = at.tool_id
            WHERE at.agent_id=%s AND t.user_id=%s
            ORDER BY t.created_at DESC
        """, (agent_id, user.user_id))
        rows = cur.fetchall()
    for r in rows: r["id"] = str(r["id"])
    return rows

@router.post("/v1/agents/{agent_id}/run")
def run_agent(
    agent_id: str,
    body: RunIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    # load agent + tools
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,name,system_prompt,model,temperature FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        agent = cur.fetchone()
        if not agent: raise HTTPException(404, "Agent not found")
        cur.execute("""
            SELECT t.id, t.name, t.kind, t.config
            FROM agent_tools at
            JOIN tools t ON t.id = at.tool_id
            WHERE at.agent_id=%s AND t.user_id=%s
        """, (agent_id, user.user_id))
        tool_rows = cur.fetchall()

    tools = build_tools_for_user(user.user_id, tool_rows)

    # session state
    key = (f"{agent_id}:{user.user_id}", body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    # prime with custom system prompt (if any)
    if agent["system_prompt"]:
        # only store once at session start
        if not st["messages"] or st["messages"][0].get("role") != "system":
            st["messages"].append({"role": "system", "content": agent["system_prompt"]})

    st["messages"].append({"role": "user", "content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    graph = make_graph(tools, n_predict=limits.get("n_predict"))
    out = graph.invoke(st); SESS[key] = out
    reply = next(m for m in reversed(out["messages"]) if m["role"] == "assistant")["content"]

    # naive usage approx
    tokens = max(1, len(body.message)//4 + len(reply)//4)
    record_usage(user.user_id, tokens=tokens, requests=1)

    return {
        "agent_id": agent_id,
        "reply": reply,
        "tools_used": list(tools.keys()),
        "model": agent["model"],
        "temperature": agent["temperature"],
    }

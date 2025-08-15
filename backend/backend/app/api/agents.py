
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import Authed, get_current_user
from app.agents.graph import make_graph
from app.agents.tool_runtime import build_tools_for_user
from app.memory.history import trim_and_summarize
from app.billing.deps import require_active_subscription, record_usage
from app.billing.limits import enforce_rate_limit
from app.agents.policy import SYSTEM

router = APIRouter()
SESS: dict[tuple[str,str,str], dict] = {}

class AgentIn(BaseModel):
    name: str
    system_prompt: str | None = None
    model: str | None = None

@router.post("/v1/agents")
def create_agent(body: AgentIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO agents (user_id, name, system_prompt, model) VALUES (%s,%s,%s,%s) RETURNING id, created_at",
                (user.user_id, body.name, body.system_prompt, body.model),
            )
            row = cur.fetchone(); conn.commit()
        except Exception:
            raise HTTPException(400, "Duplicate name or invalid")
    return {"id": row["id"], "name": body.name, "system_prompt": body.system_prompt, "model": body.model, "created_at": row["created_at"]}

@router.get("/v1/agents")
def list_agents(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, system_prompt, model, created_at FROM agents WHERE user_id=%s ORDER BY created_at DESC", (user.user_id,))
        return cur.fetchall()

class AttachToolsIn(BaseModel):
    tool_ids: list[str]

@router.post("/v1/agents/{agent_id}/tools")
def attach_tools(agent_id: str, body: AttachToolsIn, user: Authed = Depends(get_current_user)):
    if not body.tool_ids:
        return {"ok": True, "attached": 0}
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Agent not found")
        attached = 0
        for tid in body.tool_ids:
            try:
                cur.execute("INSERT INTO agent_tools (agent_id, tool_id) VALUES (%s,%s)", (agent_id, tid))
                attached += 1
            except Exception:
                pass
        conn.commit()
    return {"ok": True, "attached": attached}

@router.get("/v1/agents/{agent_id}/tools")
def list_agent_tools(agent_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.name, t.kind, t.config, t.created_at
            FROM agent_tools at
            JOIN tools t ON t.id = at.tool_id
            JOIN agents a ON a.id = at.agent_id
            WHERE at.agent_id=%s AND a.user_id=%s
            ORDER BY t.created_at DESC
        """, (agent_id, user.user_id))
        return cur.fetchall()

class ChatIn(BaseModel):
    session_id: str
    message: str

@router.post("/v1/agents/{agent_id}/chat")
def agent_chat(agent_id: str, body: ChatIn, user: Authed = Depends(require_active_subscription), limits: dict = Depends(enforce_rate_limit)):
    key = (user.user_id, agent_id, body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.name, t.kind, t.config
            FROM agent_tools at
            JOIN tools t ON t.id = at.tool_id
            JOIN agents a ON a.id = at.agent_id
            WHERE at.agent_id=%s AND a.user_id=%s
        """, (agent_id, user.user_id))
        tool_rows = cur.fetchall()

        cur.execute("SELECT system_prompt FROM agents WHERE id=%s AND user_id=%s", (agent_id, user.user_id))
        row = cur.fetchone()
        sys_prompt = row["system_prompt"] if row and row["system_prompt"] else SYSTEM

    tools = build_tools_for_user(user.user_id, tool_rows)

    st["messages"].append({"role":"user","content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))
    graph = make_graph(tools, n_predict=limits.get("n_predict"))
    out = graph.invoke(st); SESS[key] = out
    reply = next(m for m in reversed(out["messages"]) if m["role"]=="assistant")["content"]
    tokens = max(1, len(body.message)//4 + len(reply)//4)
    record_usage(user.user_id, tokens=tokens, requests=1)
    return {"reply": reply}

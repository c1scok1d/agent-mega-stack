from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, Literal
from psycopg.rows import dict_row
from psycopg.types.json import Json
from app.core.db import get_conn
from app.core.security import Authed, get_current_user
# If you already have these, import them. Adjust paths/names to match your project.
from app.agents.tool_runtime import build_tools_for_user

router = APIRouter()

class ToolIn(BaseModel):
    name: str = Field(..., min_length=1)
    kind: Literal["rag.search", "http"]  # extend later
    config: dict = Field(default_factory=dict)

@router.post("/v1/tools")
def create_tool(body: ToolIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tools (user_id,name,kind,config) VALUES (%s,%s,%s,%s) RETURNING id,created_at",
            (user.user_id, body.name, body.kind, Json(body.config)),
        )
        row = cur.fetchone(); conn.commit()
    return {"id": str(row["id"]), "name": body.name, "kind": body.kind, "config": body.config, "created_at": row["created_at"]}

@router.get("/v1/tools")
def list_tools(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,name,kind,config,created_at FROM tools WHERE user_id=%s ORDER BY created_at DESC", (user.user_id,))
        rows = cur.fetchall()
    # normalize UUIDs to string
    for r in rows: r["id"] = str(r["id"])
    return rows

@router.delete("/v1/tools/{tool_id}")
def delete_tool(tool_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tools WHERE id=%s AND user_id=%s", (tool_id, user.user_id)); conn.commit()
    return {"ok": True}

class ToolRunIn(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    
@router.post("/v1/tools/run")
def run_tool(payload: ToolRunIn, user: Authed = Depends(get_current_user)):
    """
    Execute a tool directly by name. Looks up the user's installed tools and runs the match.
    """
    # Build runtime map of tools for this user (includes HTTP tools, rag.search, etc.)
    tools = build_tools_for_user(user.user_id, include_defaults=True)

    impl = tools.get(payload.tool)
    if impl is None:
        raise HTTPException(404, detail=f"Tool '{payload.tool}' not found for this user")

    try:
        # Support either a `.run(**kwargs)` style or a simple callable(dict) style
        if hasattr(impl, "run") and callable(getattr(impl, "run")):
            out = impl.run(**payload.args)
        elif callable(impl):
            out = impl(**payload.args)
        else:
            raise RuntimeError(f"Tool '{payload.tool}' is not executable")

        return {
            "ok": True,
            "tool": payload.tool,
            "args": payload.args,
            "result": out,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"Tool execution failed: {e}")

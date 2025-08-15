from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import Authed, get_current_user

router = APIRouter()

class ToolIn(BaseModel):
    name: str = Field(..., min_length=1)
    kind: Literal["rag.search"]  # extend later
    config: dict = Field(default_factory=dict)

@router.post("/v1/tools")
def create_tool(body: ToolIn, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tools (user_id,name,kind,config) VALUES (%s,%s,%s,%s) RETURNING id,created_at",
            (user.user_id, body.name, body.kind, body.config),
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

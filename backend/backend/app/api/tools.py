
from __future__ import annotations
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import Authed, get_current_user

router = APIRouter()

class ToolIn(BaseModel):
    name: str
    kind: Literal["rag.search", "http"]
    config: dict = Field(default_factory=dict)

def _validate_config(kind: str, cfg: dict):
    if kind == "http":
        if "url" not in cfg:
            raise HTTPException(400, "http config requires 'url'")
        method = str(cfg.get("method", "GET")).upper()
        if method not in {"GET","POST","PUT","PATCH","DELETE"}:
            raise HTTPException(400, "http method must be GET/POST/PUT/PATCH/DELETE")

@router.post("/v1/tools")
def create_tool(body: ToolIn, user: Authed = Depends(get_current_user)):
    _validate_config(body.kind, body.config)
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO tools (user_id, name, kind, config) VALUES (%s,%s,%s,%s) RETURNING id, created_at",
                (user.user_id, body.name, body.kind, body.config),
            )
            row = cur.fetchone(); conn.commit()
        except Exception:
            raise HTTPException(400, "Duplicate name or invalid config")
    return {"id": row["id"], "name": body.name, "kind": body.kind, "config": body.config, "created_at": row["created_at"]}

@router.get("/v1/tools")
def list_tools(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, kind, config, created_at FROM tools WHERE user_id=%s ORDER BY created_at DESC", (user.user_id,))
        return cur.fetchall()

@router.delete("/v1/tools/{tool_id}")
def delete_tool(tool_id: str, user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tools WHERE id=%s AND user_id=%s", (tool_id, user.user_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        conn.commit()
    return {"ok": True}

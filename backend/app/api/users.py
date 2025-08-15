# backend/app/api/users.py
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import Authed, get_current_user

router = APIRouter()

class Profile(BaseModel):
    name: Optional[str] = None
    birthday: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    profession: Optional[str] = None
    business_name: Optional[str] = None
    business_address: Optional[str] = None

@router.get("/v1/users/me")
def get_me(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id::text as id, email, name, birthday, profession, business_name, business_address, created_at
                FROM users WHERE id=%s
            """, (user.user_id,))
            row = cur.fetchone()
            if not row: raise HTTPException(404, "User not found")
    return row

@router.put("/v1/users/me")
def update_me(body: Profile, user: Authed = Depends(get_current_user)):
    fields = ["name","birthday","profession","business_name","business_address"]
    updates = {k:getattr(body,k) for k in fields if getattr(body,k) is not None}
    if not updates:
        return {"updated": False}
    sets = ", ".join([f"{k}=%s" for k in updates.keys()])
    vals = list(updates.values()) + [user.user_id]
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {sets} WHERE id=%s", vals)
            conn.commit()
    return {"updated": True}

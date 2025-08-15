from __future__ import annotations
from fastapi import APIRouter, Depends
from app.core.db import get_conn
from app.core.security import get_current_user, Authed
from app.core.provisioning import provision_user_defaults

router = APIRouter(prefix="/v1/admin", tags=["admin"])

@router.post("/provision")
def reprovision_me(user: Authed = Depends(get_current_user)):
    with get_conn() as conn:
        agent_id = provision_user_defaults(conn, user.user_id)
        return {"ok": True, "agent_id": agent_id}

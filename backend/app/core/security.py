# backend/app/core/security.py
import time, secrets, hashlib, jwt
from pydantic import BaseModel
from typing import Optional

JWT_ALG = "HS256"
JWT_TTL_MIN = 60
JWT_SECRET = "dev-secret-change-me"

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def create_jwt(sub: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "iat": now, "exp": now + JWT_TTL_MIN*60}, JWT_SECRET, algorithm=JWT_ALG)

def create_refresh() -> str:
    return "rt_" + secrets.token_urlsafe(24)

class Authed(BaseModel):
    user_id: str
    email: Optional[str] = None

# JWT dependency
from fastapi import Header, HTTPException
import jwt as pyjwt

def get_current_user(authorization: str = Header(default="")) -> Authed:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid token")
    token = authorization.split(" ",1)[1]
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(401, "Invalid token")
    return Authed(user_id=str(payload.get("sub")))

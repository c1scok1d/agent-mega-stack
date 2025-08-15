# backend/app/api/auth.py
import re
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import create_jwt, create_refresh, Authed, get_current_user, _hash
from app.core.provisioning import provision_user_defaults

router = APIRouter()

class SignUpIn(BaseModel):
    email: str
    password: str

@router.post("/v1/auth/signup")
def signup(body: SignUpIn):
    import re
    from psycopg.errors import UniqueViolation, UndefinedTable, ForeignKeyViolation
    from psycopg.rows import dict_row

    email = body.email.strip().lower()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        raise HTTPException(400, "Invalid email")

    ph = _hash(body.password)

    rt_plain = None
    uid = None

    with get_conn(cursor_factory=dict_row) as conn:
        try:
            with conn.cursor() as cur:
                # insert user
                cur.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s,%s) RETURNING id",
                    (email, ph),
                )
                uid = str(cur.fetchone()["id"])

                # insert refresh token (hash only)
                rt_plain = create_refresh()
                cur.execute(
                    "INSERT INTO refresh_tokens (user_id, token_hash) VALUES (%s,%s)",
                    (uid, _hash(rt_plain)),
                )

            conn.commit()

        except UniqueViolation:
            conn.rollback()
            # email unique constraint fires here
            raise HTTPException(400, "Email already exists")
        except (UndefinedTable, ForeignKeyViolation) as e:
            conn.rollback()
            # db isn’t initialized (missing tables / fks)
            raise HTTPException(500, f"Database not initialized: {e.__class__.__name__}")
        except Exception as e:
            conn.rollback()
            # in dev you may want full detail; in prod keep generic:
            raise HTTPException(400, f"Invalid signup request")

        # Provision AFTER commit; never block signup if this fails.
        try:
            provision_user_defaults(conn, uid)
        except Exception as e:
            # log but don’t fail the request
            print(f"[provision] warning: {e}")

    return {"jwt": create_jwt(uid), "user_id": uid, "refresh_token": rt_plain}


@router.post("/v1/auth/login")
def login(body: SignUpIn):
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, password_hash FROM users WHERE email=%s", (body.email,))
            row = cur.fetchone()
            if not row or row["password_hash"] != _hash(body.password):
                raise HTTPException(401, "Invalid credentials")
            uid = str(row["id"])
            rt_plain = create_refresh()
            cur.execute("INSERT INTO refresh_tokens (user_id, token_hash) VALUES (%s,%s)",
                        (uid, _hash(rt_plain)))
            conn.commit()
    return {"jwt": create_jwt(uid), "user_id": uid, "refresh_token": rt_plain}

class RefreshIn(BaseModel):
    refresh_token: str

@router.post("/v1/auth/refresh")
def refresh(body: RefreshIn):
    t_hash = _hash(body.refresh_token)
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM refresh_tokens WHERE token_hash=%s RETURNING user_id", (t_hash,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(401, "Invalid refresh token")
            uid = str(row["user_id"])
            new_plain = create_refresh()
            cur.execute("INSERT INTO refresh_tokens (user_id, token_hash) VALUES (%s,%s)",
                        (uid, _hash(new_plain)))
            conn.commit()
    return {"jwt": create_jwt(uid), "refresh_token": new_plain}

class APIKeyIn(BaseModel):
    name: str = "default"

@router.post("/v1/apikeys")
def create_apikey(user: Authed = Depends(get_current_user), body: APIKeyIn = APIKeyIn()):
    secret = "sk_" + _hash(create_refresh())[:32]
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO api_keys (user_id, name, secret) VALUES (%s,%s,%s) RETURNING id, created_at",
                        (user.user_id, body.name, secret))
            row = cur.fetchone(); conn.commit()
    return {"id": str(row["id"]), "name": body.name, "created_at": row["created_at"], "last_used_at": None, "plaintext": secret}

@router.get("/v1/apikeys")
def list_apikeys(user: Authed = Depends(get_current_user)):
    with get_conn(cursor_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at, last_used_at FROM api_keys WHERE user_id=%s ORDER BY created_at DESC",
                        (user.user_id,))
            rows = cur.fetchall()
    # Cast UUIDs to strings for JSON
    for r in rows:
        r["id"] = str(r["id"])
    return rows

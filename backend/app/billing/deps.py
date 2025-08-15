
import os, datetime as dt
from fastapi import HTTPException, Depends
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import get_current_user, Authed

BILLING_ENABLED = os.getenv("BILLING_ENABLED", "false").lower() == "true"
FREE_TOKENS = int(os.getenv("BILLING_FREE_TOKENS", "25000"))

def _month_start_utc(now: dt.datetime) -> dt.date:
    return now.astimezone(dt.timezone.utc).date().replace(day=1)

def require_active_subscription(user: Authed = Depends(get_current_user)):
    if not BILLING_ENABLED: return user
    now = dt.datetime.utcnow(); start = _month_start_utc(now)
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT tokens_used FROM usage_counters WHERE user_id=%s AND period_start=%s", (user.user_id, start))
        row = cur.fetchone(); used = row["tokens_used"] if row else 0
    if used < FREE_TOKENS: return user
    raise HTTPException(402, "Payment required")

def record_usage(user_id: str, tokens: int, requests: int = 1):
    now = dt.datetime.utcnow(); start = _month_start_utc(now)
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS usage_counters (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL, period_start DATE NOT NULL, tokens_used BIGINT NOT NULL DEFAULT 0, requests_used BIGINT NOT NULL DEFAULT 0, UNIQUE(user_id, period_start))")
        cur.execute("INSERT INTO usage_counters (user_id, period_start, tokens_used, requests_used) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, period_start) DO UPDATE SET tokens_used=usage_counters.tokens_used+EXCLUDED.tokens_used, requests_used=usage_counters.requests_used+EXCLUDED.requests_used",
                    (user_id, start, tokens, requests)); conn.commit()

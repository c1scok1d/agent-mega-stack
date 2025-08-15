
import datetime as dt
from fastapi import APIRouter, Depends
from psycopg.rows import dict_row
from app.core.db import get_conn
from app.core.security import Authed, get_current_user
from app.billing.limits import plan_limits_for_user
router = APIRouter()
def _month_start(now: dt.datetime) -> dt.date: return now.astimezone(dt.timezone.utc).date().replace(day=1)
@router.get("/v1/usage")
def usage(user: Authed = Depends(get_current_user)):
    limits = plan_limits_for_user(user); start = _month_start(dt.datetime.utcnow())
    with get_conn(cursor_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS usage_counters (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL, period_start DATE NOT NULL, tokens_used BIGINT NOT NULL DEFAULT 0, requests_used BIGINT NOT NULL DEFAULT 0, UNIQUE(user_id, period_start))")
        cur.execute("SELECT tokens_used, requests_used FROM usage_counters WHERE user_id=%s AND period_start=%s", (user.user_id, start))
        row = cur.fetchone() or {"tokens_used": 0, "requests_used": 0}
    return {"plan": limits["plan"], "n_predict": limits["n_predict"], "rate_limit_per_min": limits["rpm"], "tokens_used": row["tokens_used"], "requests_used": row["requests_used"], "period_start": str(start)}

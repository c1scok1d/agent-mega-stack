
import os, time
from fastapi import HTTPException, Depends
from app.core.redis_client import get_redis
from app.core.security import Authed, get_current_user

FREE_N_PREDICT = int(os.getenv("FREE_N_PREDICT", "192"))
PRO_N_PREDICT  = int(os.getenv("PRO_N_PREDICT", "512"))
RATE_LIMIT_PER_MIN_FREE = int(os.getenv("RATE_LIMIT_PER_MIN_FREE", "20"))
RATE_LIMIT_PER_MIN_PRO  = int(os.getenv("RATE_LIMIT_PER_MIN_PRO", "120"))

def plan_limits_for_user(user: Authed):
    # simple: everyone is free unless BILLING_ENABLED with active sub (left out for brevity)
    return {"plan":"free","n_predict":FREE_N_PREDICT,"rpm":RATE_LIMIT_PER_MIN_FREE}

def enforce_rate_limit(user: Authed = Depends(get_current_user)):
    limits = plan_limits_for_user(user)
    rpm = limits["rpm"]; key = f"ratelimit:{user.user_id}:{int(time.time()//60)}"
    r = get_redis(); cur = r.incr(key, 1)
    if cur == 1: r.expire(key, 70)
    if cur > rpm: raise HTTPException(429, f"Rate limit exceeded ({rpm}/min for {limits['plan']})")
    return limits

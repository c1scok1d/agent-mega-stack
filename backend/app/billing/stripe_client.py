
import os, stripe
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
BILLING_ENABLED = os.getenv("BILLING_ENABLED", "false").lower() == "true"
def get_client() -> stripe:
    class Dummy: pass
    if not BILLING_ENABLED:
        d = Dummy(); d.api_key = ""; return d
    stripe.api_key = STRIPE_API_KEY; return stripe

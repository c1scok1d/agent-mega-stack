
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from app.core.security import get_current_user, Authed
from app.billing.stripe_client import get_client
router = APIRouter()
class CheckoutIn(BaseModel):
    price_id: str | None = None
    success_url: str = "https://example.com/success"
    cancel_url: str = "https://example.com/cancel"
@router.post("/v1/billing/checkout")
def create_checkout(body: CheckoutIn, user: Authed = Depends(get_current_user)):
    stripe = get_client(); price_id = body.price_id or os.getenv("STRIPE_PRICE_PRO")
    if not price_id: raise HTTPException(500, "Stripe price id not configured")
    if not hasattr(stripe, "checkout"): return {"dev": True, "checkout_url": "https://example.com/dev"}
    customer = stripe.Customer.create(email=user.email, metadata={"user_id": user.user_id})
    session = stripe.checkout.Session.create(mode="subscription", customer=customer["id"], line_items=[{"price": price_id, "quantity": 1}], success_url=body.success_url, cancel_url=body.cancel_url)
    return {"checkout_url": session["url"]}
@router.post("/v1/billing/webhook")
async def stripe_webhook(request: Request):
    stripe = get_client()
    if not hasattr(stripe, "Webhook"): return {"dev": True}
    payload = await request.body(); sig = request.headers.get("stripe-signature",""); secret = os.getenv("STRIPE_WEBHOOK_SECRET","")
    try: event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as e: raise HTTPException(400, f"Webhook error: {e}")
    return {"ok": True}

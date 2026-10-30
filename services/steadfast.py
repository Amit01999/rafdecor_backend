"""Steadfast Courier integration. Configure STEADFAST_API_KEY / STEADFAST_SECRET_KEY to enable.
API base per Steadfast's own developer docs (not the https://steadfast.com.bd marketing site, which has no API) —
verify this is still current before going live, since it isn't publicly documented in a way this environment
could confirm: https://portal.packzy.com/api/v1/create_order.
"""
import os
import httpx
from fastapi import HTTPException

BASE_URL = os.environ.get("STEADFAST_API_BASE", "https://portal.packzy.com/api/v1")


def configured() -> bool:
    return bool(os.environ.get("STEADFAST_API_KEY") and os.environ.get("STEADFAST_SECRET_KEY"))


def _recipient_address(addr: dict) -> str:
    return ", ".join(p for p in (addr.get("address"), addr.get("area"), addr.get("district")) if p)


async def create_order(order: dict) -> dict:
    """Books a delivery with Steadfast for this order. Returns {tracking_code, consignment_id, raw}."""
    if not configured():
        raise HTTPException(503, "Steadfast is not configured — set STEADFAST_API_KEY and STEADFAST_SECRET_KEY")
    addr = order["shipping_address"]
    payload = {
        "invoice": order["order_number"],
        "recipient_name": order["customer"]["name"],
        "recipient_phone": order["customer"]["phone"],
        "recipient_address": _recipient_address(addr),
        # Only the unpaid remainder is COD — an order with an advance payment already recorded isn't fully COD.
        "cod_amount": round(max(0.0, order["total"] - (order.get("payment") or {}).get("amount_paid", 0)), 2),
        "note": addr.get("notes") or "",
    }
    headers = {
        "Api-Key": os.environ["STEADFAST_API_KEY"],
        "Secret-Key": os.environ["STEADFAST_SECRET_KEY"],
        "Content-Type": "application/json",
    }
    try:
        # Kept comfortably under Vercel's default serverless function timeout (10s on Hobby) so a slow
        # Steadfast response surfaces as our own clean error instead of a raw platform 504.
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(f"{BASE_URL}/create_order", json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach Steadfast: {e}")
    if res.status_code != 200:
        raise HTTPException(502, f"Steadfast rejected the request ({res.status_code}): {res.text[:300]}")
    try:
        data = res.json()
    except ValueError:
        raise HTTPException(502, "Steadfast returned an unexpected (non-JSON) response")
    # Steadfast's documented shape nests the result under "consignment"; fall back to a top-level
    # tracking_code in case that's what your account's API version actually returns.
    consignment = data.get("consignment") or {}
    tracking_code = consignment.get("tracking_code") or data.get("tracking_code")
    if not tracking_code:
        raise HTTPException(502, f"Steadfast did not return a tracking code: {data}")
    return {"tracking_code": tracking_code, "consignment_id": consignment.get("consignment_id"), "raw": data}

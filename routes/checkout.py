import re
import secrets
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, EmailStr, field_validator
from pymongo.errors import DuplicateKeyError
from core.db import db
from core.utils import now, new_id, rate_limit
from core.security import get_current_user, optional_user
from services.pricing import build_quote, require_valid_quote, get_settings
from services.inventory import reserve_lines, release_lines
from services.payments import get_provider, enabled_methods
from services.notifications import notify

router = APIRouter(tags=["cart & checkout"])

PHONE_RE = re.compile(r"^(?:\+?88)?01[3-9]\d{8}$")
CUSTOMER_STATUS_LABELS = {
    "placed": "Order placed", "confirmed": "Order confirmed", "processing": "Processing", "packaging": "Preparing & packaging",
    "dispatched": "Dispatched", "out_for_delivery": "Out for delivery", "delivered": "Delivered", "cancelled": "Cancelled",
    "returned": "Returned", "refunded": "Refunded",
}


class CartItem(BaseModel):
    product_id: str = Field(max_length=64)
    variant_id: Optional[str] = Field(default=None, max_length=64)
    quantity: int = Field(ge=1, le=99)


class QuoteIn(BaseModel):
    items: List[CartItem] = Field(max_length=50)
    coupon_code: Optional[str] = Field(default=None, max_length=40)


class CartIn(BaseModel):
    items: List[CartItem] = Field(max_length=50)


class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str
    email: Optional[EmailStr] = None

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v):
        v = re.sub(r"[\s-]", "", v)
        if not PHONE_RE.match(v):
            raise ValueError("Enter a valid Bangladeshi mobile number (e.g. 01XXXXXXXXX)")
        return v[-11:]


class AddressIn(BaseModel):
    address: str = Field(min_length=5, max_length=300)
    district: str = Field(min_length=2, max_length=60)
    area: str = Field(default="", max_length=80)
    notes: str = Field(default="", max_length=500)


class OrderIn(BaseModel):
    items: List[CartItem] = Field(min_length=1, max_length=50)
    customer: CustomerIn
    shipping_address: AddressIn
    payment_method: str = Field(max_length=30)
    coupon_code: Optional[str] = Field(default=None, max_length=40)
    idempotency_key: str = Field(min_length=16, max_length=64)


def customer_order(o: dict) -> dict:
    hist = [{"status": h["status"], "label": CUSTOMER_STATUS_LABELS.get(h["status"], h["status"]), "at": h["at"], "note": h.get("note") if h.get("visible") else None}
            for h in o.get("status_history", [])]
    pay = o.get("payment", {})
    return {
        "id": o["id"], "order_number": o["order_number"], "status": o["status"], "status_label": CUSTOMER_STATUS_LABELS.get(o["status"]),
        "items": [{k: i.get(k) for k in ("product_id", "slug", "name", "variant_label", "image", "unit_price", "quantity", "line_total")} for i in o["items"]],
        "subtotal": o["subtotal"], "discount": o["discount"], "coupon_code": o.get("coupon_code"), "shipping": o["shipping"], "total": o["total"],
        "customer": o["customer"], "shipping_address": o["shipping_address"], "created_at": o["created_at"], "history": hist,
        "payment": {"method": pay.get("method"), "method_label": pay.get("method_label"), "status": pay.get("status"), "amount_due": pay.get("amount_due"),
                    "amount_paid": pay.get("amount_paid"), "instructions": pay.get("instructions"), "bank_details": pay.get("bank_details")},
        "courier": o.get("courier"), "customer_notes": [n for n in o.get("notes", []) if n.get("visible")],
    }


async def _order_number() -> str:
    for _ in range(5):
        n = f"RD{now():%y%m%d}-{secrets.randbelow(900000) + 100000}"
        if not await db.orders.find_one({"order_number": n}):
            return n
    return f"RD{now():%y%m%d}-{secrets.token_hex(4).upper()}"


@router.post("/cart/quote")
async def quote(body: QuoteIn, request: Request):
    user = await optional_user(request)
    return await build_quote([i.model_dump() for i in body.items], body.coupon_code, user["id"] if user else None)


@router.get("/cart")
async def get_cart(user=Depends(get_current_user)):
    c = await db.carts.find_one({"user_id": user["id"]}, {"_id": 0})
    return {"items": (c or {}).get("items", [])}


@router.put("/cart")
async def put_cart(body: CartIn, user=Depends(get_current_user)):
    items = [i.model_dump() for i in body.items]
    await db.carts.update_one({"user_id": user["id"]}, {"$set": {"items": items, "updated_at": now()}}, upsert=True)
    return {"items": items}


@router.get("/checkout/options")
async def checkout_options():
    s = await get_settings()
    return {"payment_methods": enabled_methods(s), "bank_instructions": s.get("payments", {}).get("bank_instructions"),
            "shipping": s.get("shipping", {}), "districts": s.get("districts", [])}


@router.post("/checkout/orders")
async def place_order(body: OrderIn, request: Request):
    rate_limit(request, "checkout", 10, 60)
    existing = await db.orders.find_one({"idempotency_key": body.idempotency_key}, {"_id": 0})
    if existing:
        return {"order": customer_order(existing), "duplicate": True}
    user = await optional_user(request)
    settings = await get_settings()
    provider = get_provider(body.payment_method, settings)
    items = [i.model_dump() for i in body.items]
    q = await require_valid_quote(items, body.coupon_code, user["id"] if user else None)
    order_number = await _order_number()
    await reserve_lines(q["lines"], order_number)
    coupon_code = q["coupon"]["code"] if q.get("coupon") else None
    if coupon_code:
        ok = await db.coupons.find_one_and_update(
            {"code": coupon_code, "$or": [{"usage_limit": None}, {"usage_limit": 0}, {"$expr": {"$lt": ["$used_count", "$usage_limit"]}}]},
            {"$inc": {"used_count": 1}})
        if not ok:
            await release_lines(q["lines"], "order_failed", order_number)
            raise HTTPException(409, {"message": "This coupon has reached its usage limit.", "issues": []})
    t = now()
    order = {
        "id": new_id(), "order_number": order_number, "idempotency_key": body.idempotency_key, "user_id": user["id"] if user else None,
        "items": q["lines"], "subtotal": q["subtotal"], "discount": q["discount"], "coupon_code": coupon_code, "shipping": q["shipping"],
        "total": q["total"], "currency": "BDT", "customer": body.customer.model_dump(), "shipping_address": body.shipping_address.model_dump(),
        "status": "placed", "status_history": [{"status": "placed", "at": t, "by": "customer", "visible": True}],
        "payment": {"method": provider.key, "method_label": provider.label, "status": "unpaid", "amount_due": q["total"], "amount_paid": 0.0,
                    "amount_refunded": 0.0, "transaction_ref": None, "paid_at": None},
        "notes": [], "courier": None, "inventory_released": False, "source": "web", "created_at": t, "updated_at": t,
    }
    init = await provider.initiate(order, settings)
    order["payment"].update({"status": init["status"], "instructions": init.get("instructions"), "bank_details": init.get("bank_details")})
    try:
        await db.orders.insert_one(order)
    except DuplicateKeyError:
        await release_lines(q["lines"], "order_duplicate", order_number)
        if coupon_code:
            await db.coupons.update_one({"code": coupon_code}, {"$inc": {"used_count": -1}})
        existing = await db.orders.find_one({"idempotency_key": body.idempotency_key}, {"_id": 0})
        return {"order": customer_order(existing), "duplicate": True}
    for l in q["lines"]:
        await db.products.update_one({"id": l["product_id"]}, {"$inc": {"sales_count": l["quantity"]}})
    await db.events.insert_one({"type": "order_completed", "order_id": order["id"], "product_ids": [l["product_id"] for l in q["lines"]],
                                "session_id": request.headers.get("x-session-id", "")[:64], "created_at": t})
    if user:
        await db.carts.update_one({"user_id": user["id"]}, {"$set": {"items": []}})
    await notify("order_placed", body.customer.email or "", f"Order {order_number} received", f"Total ৳{q['total']:,.2f}", order["id"])
    await notify("admin_new_order", settings.get("contact", {}).get("email", ""), f"New order {order_number}", f"Total ৳{q['total']:,.2f}", order["id"])
    order.pop("_id", None)
    return {"order": customer_order(order), "duplicate": False}


class TrackIn(BaseModel):
    order_number: str = Field(max_length=40)
    phone: str = Field(max_length=20)


@router.post("/orders/track")
async def track(body: TrackIn, request: Request):
    rate_limit(request, "track", 15, 60)
    phone = re.sub(r"\D", "", body.phone)[-11:]
    o = await db.orders.find_one({"order_number": body.order_number.strip().upper(), "customer.phone": phone}, {"_id": 0})
    if not o:
        raise HTTPException(404, "No order matches this order number and phone number")
    return customer_order(o)


@router.get("/orders")
async def my_orders(user=Depends(get_current_user), page: int = 1):
    q = {"user_id": user["id"]}
    total = await db.orders.count_documents(q)
    docs = await db.orders.find(q, {"_id": 0}).sort("created_at", -1).skip((max(page, 1) - 1) * 20).limit(20).to_list(20)
    return {"items": [customer_order(o) for o in docs], "total": total}


@router.get("/orders/{order_number}")
async def my_order(order_number: str, user=Depends(get_current_user)):
    o = await db.orders.find_one({"order_number": order_number, "user_id": user["id"]}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Order not found")
    return customer_order(o)

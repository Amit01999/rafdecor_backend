import csv
import io
from datetime import datetime
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from core.db import db
from core.utils import now, safe_regex, page_params, audit
from core.security import require_perm
from services.inventory import release_lines, refresh_flags
from services.payments import record_transaction, payment_status, PROVIDERS
from services.notifications import notify
from routes.checkout import CUSTOMER_STATUS_LABELS

router = APIRouter(prefix="/admin/orders", tags=["admin orders"])
pay_router = APIRouter(prefix="/payments", tags=["payments"])

TRANSITIONS = {
    "placed": ["confirmed", "cancelled"],
    "confirmed": ["processing", "packaging", "dispatched", "cancelled"],
    "processing": ["packaging", "dispatched", "cancelled"],
    "packaging": ["dispatched", "cancelled"],
    "dispatched": ["out_for_delivery", "delivered", "returned"],
    "out_for_delivery": ["delivered", "returned"],
    "delivered": ["returned"],
    "returned": ["refunded"],
    "cancelled": [],
    "refunded": [],
}


def _range(date_from, date_to):
    q = {}
    if date_from:
        q["$gte"] = date_from
    if date_to:
        q["$lte"] = date_to
    return q


@router.get("")
async def list_orders(q: Optional[str] = None, status: Optional[str] = None, payment_status: Optional[str] = None,
                      payment_method: Optional[str] = None, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                      page: int = 1, limit: int = 20, user=Depends(require_perm("orders.read"))):
    query = {}
    if q:
        r = safe_regex(q)
        query["$or"] = [{"order_number": r}, {"customer.name": r}, {"customer.phone": r}, {"customer.email": r}, {"items.name": r}]
    if status:
        query["status"] = status
    if payment_status:
        query["payment.status"] = payment_status
    if payment_method:
        query["payment.method"] = payment_method
    if date_from or date_to:
        query["created_at"] = _range(date_from, date_to)
    skip, limit = page_params(page, limit)
    total = await db.orders.count_documents(query)
    items = await db.orders.find(query, {"_id": 0, "idempotency_key": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return {"items": items, "total": total, "transitions": TRANSITIONS, "labels": CUSTOMER_STATUS_LABELS}


@router.get("/export.csv")
async def export(status: Optional[str] = None, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                 user=Depends(require_perm("orders.read"))):
    query = {}
    if status:
        query["status"] = status
    if date_from or date_to:
        query["created_at"] = _range(date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order_number", "created_at", "status", "customer", "phone", "district", "items", "subtotal", "discount", "shipping", "total", "payment_method", "payment_status", "amount_paid"])
    async for o in db.orders.find(query, {"_id": 0}).sort("created_at", -1).limit(5000):
        w.writerow([o["order_number"], o["created_at"].isoformat(), o["status"], o["customer"]["name"], o["customer"]["phone"], o["shipping_address"]["district"],
                    "; ".join(f"{i['name']} x{i['quantity']}" for i in o["items"]), o["subtotal"], o["discount"], o["shipping"], o["total"],
                    o["payment"]["method"], o["payment"]["status"], o["payment"]["amount_paid"]])
    await audit(user, "order.export", "order", "*", {"status": status})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=orders.csv"})


async def _get(oid):
    o = await db.orders.find_one({"$or": [{"id": oid}, {"order_number": oid}]}, {"_id": 0, "idempotency_key": 0})
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@router.get("/{oid}")
async def get_order(oid: str, user=Depends(require_perm("orders.read"))):
    o = await _get(oid)
    o["allowed_transitions"] = TRANSITIONS.get(o["status"], [])
    o["transactions"] = await db.payment_transactions.find({"order_id": o["id"]}, {"_id": 0}).sort("created_at", 1).to_list(100)
    o["audit"] = await db.audit_logs.find({"entity": "order", "entity_id": o["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    if o.get("user_id"):
        c = await db.users.find_one({"id": o["user_id"]}, {"_id": 0, "id": 1, "email": 1, "name": 1})
        o["account"] = c
    return o


class StatusIn(BaseModel):
    status: str = Field(max_length=30)
    note: str = Field(default="", max_length=500)
    visible_to_customer: bool = True
    restock: bool = False
    notify_customer: bool = True


@router.post("/{oid}/status")
async def update_status(oid: str, body: StatusIn, user=Depends(require_perm("orders.write"))):
    o = await _get(oid)
    if body.status not in TRANSITIONS.get(o["status"], []):
        raise HTTPException(422, f"Cannot change status from {o['status']} to {body.status}")
    entry = {"status": body.status, "at": now(), "by": user["email"], "note": body.note or None, "visible": body.visible_to_customer}
    upd = {"$set": {"status": body.status, "updated_at": now()}, "$push": {"status_history": entry}}
    res = await db.orders.update_one({"id": o["id"], "status": o["status"]}, upd)
    if not res.modified_count:
        raise HTTPException(409, "Order was updated by someone else. Please refresh.")
    restocked = False
    if body.status in ("cancelled", "returned") and body.restock and not o.get("inventory_released"):
        await release_lines(o["items"], f"order_{body.status}", o["order_number"], user)
        for i in o["items"]:
            await refresh_flags(i["product_id"])
        await db.orders.update_one({"id": o["id"]}, {"$set": {"inventory_released": True}})
        restocked = True
    if body.status == "cancelled":
        if o.get("coupon_code"):
            await db.coupons.update_one({"code": o["coupon_code"], "used_count": {"$gt": 0}}, {"$inc": {"used_count": -1}})
        for i in o["items"]:
            await db.products.update_one({"id": i["product_id"]}, {"$inc": {"sales_count": -i["quantity"]}})
    await audit(user, "order.status", "order", o["id"], {"from": o["status"], "to": body.status, "restocked": restocked, "note": body.note})
    if body.notify_customer:
        await notify("order_status", o["customer"].get("email") or o["customer"]["phone"], f"Order {o['order_number']}: {CUSTOMER_STATUS_LABELS.get(body.status)}", body.note, o["id"])
    return await get_order(o["id"], user)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    visible: bool = False


@router.post("/{oid}/notes")
async def add_note(oid: str, body: NoteIn, user=Depends(require_perm("orders.write"))):
    o = await _get(oid)
    await db.orders.update_one({"id": o["id"]}, {"$push": {"notes": {"text": body.text, "visible": body.visible, "by": user["email"], "at": now()}}})
    await audit(user, "order.note", "order", o["id"], {"visible": body.visible})
    return {"ok": True}


class CourierIn(BaseModel):
    name: str = Field(max_length=80)
    tracking_number: str = Field(default="", max_length=80)
    tracking_url: str = Field(default="", max_length=300)


@router.post("/{oid}/courier")
async def courier(oid: str, body: CourierIn, user=Depends(require_perm("orders.write"))):
    o = await _get(oid)
    await db.orders.update_one({"id": o["id"]}, {"$set": {"courier": body.model_dump(), "updated_at": now()}})
    await audit(user, "order.courier", "order", o["id"], body.model_dump())
    return {"ok": True}


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    reference: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=300)


@router.post("/{oid}/payments")
async def record_payment(oid: str, body: PaymentIn, user=Depends(require_perm("orders.finance"))):
    o = await _get(oid)
    pay = o["payment"]
    if pay["amount_paid"] + body.amount > o["total"] + 0.01:
        raise HTTPException(422, "Payment exceeds the order total")
    if body.reference and await db.payment_transactions.find_one({"provider": pay["method"], "reference": body.reference}):
        raise HTTPException(409, "This transaction reference was already recorded")
    await record_transaction(o, pay["method"], "payment", body.amount, body.reference, user, body.note)
    paid = round(pay["amount_paid"] + body.amount, 2)
    st = payment_status(o["total"], paid, pay.get("amount_refunded", 0), pay["status"])
    await db.orders.update_one({"id": o["id"]}, {"$set": {"payment.amount_paid": paid, "payment.status": st, "payment.transaction_ref": body.reference or pay.get("transaction_ref"),
                                                          "payment.paid_at": now() if st == "paid" else pay.get("paid_at"), "payment.verification_notes": body.note or None,
                                                          "payment.verified_by": user["email"], "updated_at": now()}})
    await audit(user, "order.payment_recorded", "order", o["id"], {"amount": body.amount, "reference": body.reference, "status": st})
    return await get_order(o["id"], user)


class RefundIn(BaseModel):
    amount: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=300)
    reference: str = Field(default="", max_length=100)


@router.post("/{oid}/refunds")
async def refund(oid: str, body: RefundIn, user=Depends(require_perm("orders.finance"))):
    o = await _get(oid)
    pay = o["payment"]
    refunded = round(pay.get("amount_refunded", 0) + body.amount, 2)
    if refunded > pay["amount_paid"] + 0.01:
        raise HTTPException(422, "Refund cannot exceed the amount paid")
    await record_transaction(o, pay["method"], "refund", body.amount, body.reference or None, user, body.reason)
    st = payment_status(o["total"], pay["amount_paid"], refunded, pay["status"])
    await db.orders.update_one({"id": o["id"]}, {"$set": {"payment.amount_refunded": refunded, "payment.status": st, "updated_at": now()},
                                                 "$push": {"refunds": {"amount": body.amount, "reason": body.reason, "by": user["email"], "at": now()}}})
    await audit(user, "order.refund", "order", o["id"], {"amount": body.amount, "reason": body.reason})
    return await get_order(o["id"], user)


@pay_router.get("/providers")
async def providers():
    return {"items": [{"key": p.key, "label": p.label, "online": p.online, "configured": p.configured()} for p in PROVIDERS.values()]}


@pay_router.post("/{provider}/callback")
async def callback(provider: str, request: Request):
    """Server-to-server notifications (IPN). Frontend redirects are never treated as proof of payment."""
    p = PROVIDERS.get(provider)
    if not p or not p.online:
        raise HTTPException(404, "Unknown payment provider")
    form = dict(await request.form())
    return await p.handle_callback(form)

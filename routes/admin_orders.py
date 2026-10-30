import csv
import io
import os
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
from services import steadfast
from routes.checkout import CUSTOMER_STATUS_LABELS

router = APIRouter(prefix="/admin/orders", tags=["admin orders"])
pay_router = APIRouter(prefix="/payments", tags=["payments"])
courier_router = APIRouter(prefix="/couriers", tags=["couriers"])

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


@router.post("/{oid}/steadfast")
async def send_to_steadfast(oid: str, user=Depends(require_perm("orders.write"))):
    o = await _get(oid)
    result = await steadfast.create_order(o)
    # consignment_id is saved here specifically so the webhook below (which identifies parcels by
    # consignment_id, not tracking_code) has something reliable to match against.
    courier_info = {"name": "Steadfast Courier", "tracking_number": result["tracking_code"], "tracking_url": "",
                    "consignment_id": str(result["consignment_id"]) if result.get("consignment_id") is not None else None}
    upd = {"$set": {"courier": courier_info, "updated_at": now()}}
    # "dispatched" is this app's existing status for "handed to a courier" — reuse it (only when the
    # order's current status actually allows that transition) instead of inventing a separate, unvalidated
    # status string that the rest of the admin/customer status machinery wouldn't recognize.
    if "dispatched" in TRANSITIONS.get(o["status"], []):
        upd["$set"]["status"] = "dispatched"
        upd["$push"] = {"status_history": {"status": "dispatched", "at": now(), "by": user["email"],
                                           "note": f"Booked with Steadfast — tracking {result['tracking_code']}", "visible": True}}
    await db.orders.update_one({"id": o["id"]}, upd)
    await audit(user, "order.steadfast", "order", o["id"], {"tracking_code": result["tracking_code"], "consignment_id": result.get("consignment_id")})
    if "status" in upd["$set"]:
        await notify("order_status", o["customer"].get("email") or o["customer"]["phone"],
                     f"Order {o['order_number']}: {CUSTOMER_STATUS_LABELS.get('dispatched')}", f"Tracking code: {result['tracking_code']}", o["id"])
    return await get_order(o["id"], user)


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


# Steadfast's own courier status wording -> this app's existing, TRANSITIONS-validated order statuses.
# "processing" is the closest existing equivalent to Steadfast's "pending"/"in_review" — in the normal
# flow the order is already "dispatched" by the time these arrive, so (per the TRANSITIONS check below)
# they end up being safely observed-only anyway; they're mapped mainly for the case an order hasn't
# reached "dispatched" yet. "partial_delivered" maps to "delivered" since this app doesn't track partial
# delivery as its own status — the raw Steadfast wording is preserved in the status_history note either way.
STEADFAST_STATUS_MAP = {
    "pending": "processing",
    "in_review": "processing",
    "delivered": "delivered",
    "partial_delivered": "delivered",
    "cancelled": "cancelled",
    "returned": "returned",
}

# Deliberately NOT mapped to a status change, even though Steadfast sends them: "hold" has no equivalent
# in this app's pipeline, and the three "*_approval_pending" statuses are explicitly provisional — e.g.
# mapping "cancelled_approval_pending" to "cancelled" would reverse this order's coupon usage and sales
# counts before the cancellation is even confirmed, which Steadfast could still reverse. All four are
# still acknowledged (200) and recorded as an internal order note, just without changing the order's
# actual status — an admin can act on it once Steadfast's review resolves one way or the other.
STEADFAST_OBSERVED_ONLY_STATUSES = {"hold", "delivered_approval_pending", "partial_delivered_approval_pending", "cancelled_approval_pending"}

WEBHOOK_ACTOR = {"email": "steadfast-webhook"}


@courier_router.post("/steadfast/webhook")
async def steadfast_webhook(request: Request):
    """Receives Steadfast's delivery-status push notifications.

    Auth: Steadfast's merchant portal lets you set a callback URL plus a bearer token it will send back
    on every call — NOT the Api-Key/Secret-Key pair used to create orders. Configure that same token here
    as STEADFAST_WEBHOOK_TOKEN. (This header convention is the best-documented one available for Steadfast's
    webhooks at the time of writing; confirm it still matches your portal's webhook settings before relying
    on it, and adjust the header check below if Steadfast signs requests differently for your account.)

    Always acknowledges with 200 once the request is authenticated and parseable, even if we don't end up
    changing anything (order not found, status not recognized, or not a valid transition from where the
    order currently is) — returning an error status for those cases would just make Steadfast retry
    indefinitely for events we're deliberately not acting on. Only a failed/missing auth token or an
    unparseable body get a non-200, since retrying either of those would never succeed anyway.
    """
    expected = os.environ.get("STEADFAST_WEBHOOK_TOKEN")
    if not expected:
        raise HTTPException(503, "Steadfast webhook is not configured — set STEADFAST_WEBHOOK_TOKEN")
    if request.headers.get("authorization") != f"Bearer {expected}":
        raise HTTPException(401, "Invalid or missing webhook token")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON body")

    # Exact keys per Steadfast's documented webhook payload — no alternate/legacy key names accepted.
    invoice = body.get("invoice")
    consignment_id = str(body["consignment_id"]) if body.get("consignment_id") is not None else None
    raw_status = str(body.get("status") or "").strip().lower()

    lookup = [q for q in ({"order_number": invoice} if invoice else None,
                          {"courier.consignment_id": consignment_id} if consignment_id else None) if q]
    if not lookup:
        raise HTTPException(400, "Payload must include an 'invoice' and/or a 'consignment_id'")
    o = await db.orders.find_one({"$or": lookup}, {"_id": 0})
    if not o:
        return {"ok": True, "handled": False, "reason": "No matching order for this invoice/consignment_id"}

    note = f"Steadfast reported '{raw_status}' (consignment {consignment_id or invoice})"
    new_status = STEADFAST_STATUS_MAP.get(raw_status)
    if raw_status in STEADFAST_OBSERVED_ONLY_STATUSES or not new_status or new_status not in TRANSITIONS.get(o["status"], []):
        # Record what Steadfast said without claiming it as a status change — as an internal note rather
        # than a status_history entry, since the order's actual status hasn't moved.
        await db.orders.update_one({"id": o["id"]}, {"$push": {"notes": {"text": note, "visible": False, "by": "steadfast-webhook", "at": now()}}})
        await audit(WEBHOOK_ACTOR, "order.steadfast_webhook", "order", o["id"], {"raw_status": raw_status, "mapped_status": None})
        return {"ok": True, "handled": False, "reason": f"Status '{raw_status}' noted but not applied (order is currently '{o['status']}')"}

    entry = {"status": new_status, "at": now(), "by": "steadfast-webhook", "note": note, "visible": True}
    res = await db.orders.update_one({"id": o["id"], "status": o["status"]},
                                     {"$set": {"status": new_status, "updated_at": now()}, "$push": {"status_history": entry}})
    if not res.modified_count:
        return {"ok": True, "handled": False, "reason": "Order status changed concurrently; skipped"}
    if new_status == "cancelled":
        # Same bookkeeping update_status already does for a manual cancel — inventory restock is
        # deliberately NOT automated here, since whether a returned/cancelled parcel is resalable
        # needs a human to actually look at it.
        if o.get("coupon_code"):
            await db.coupons.update_one({"code": o["coupon_code"], "used_count": {"$gt": 0}}, {"$inc": {"used_count": -1}})
        for i in o["items"]:
            await db.products.update_one({"id": i["product_id"]}, {"$inc": {"sales_count": -i["quantity"]}})
    await audit(WEBHOOK_ACTOR, "order.steadfast_webhook", "order", o["id"], {"raw_status": raw_status, "mapped_status": new_status})
    await notify("order_status", o["customer"].get("email") or o["customer"]["phone"],
                f"Order {o['order_number']}: {CUSTOMER_STATUS_LABELS.get(new_status)}", note, o["id"])
    return {"ok": True, "handled": True, "order_number": o["order_number"], "status": new_status}

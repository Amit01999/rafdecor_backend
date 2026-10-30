from datetime import datetime, timedelta
from typing import Optional, Literal, List
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field, EmailStr
from pymongo.errors import DuplicateKeyError
from core.db import db
from core.utils import now, new_id, safe_regex, page_params, audit, clean
from core.security import require_perm, require_staff, hash_password, STAFF_ROLES, ALL_PERMISSIONS, permissions_for
from routes.auth import create_reset_token
from services.notifications import notify, channel_configured

router = APIRouter(prefix="/admin", tags=["admin operations"])


def period(date_from: Optional[datetime], date_to: Optional[datetime]):
    end = date_to or now()
    start = date_from or (end - timedelta(days=30))
    return start, end


# ---------- dashboard ----------
@router.get("/dashboard")
async def dashboard(date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, user=Depends(require_perm("analytics.read"))):
    start, end = period(date_from, date_to)
    rng = {"created_at": {"$gte": start, "$lte": end}}
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    status_counts = {r["_id"]: r["n"] for r in await db.orders.aggregate([{"$match": rng}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(20)}
    fin = await db.orders.aggregate([{"$match": rng}, {"$group": {
        "_id": None, "orders": {"$sum": 1},
        "order_value": {"$sum": "$total"},
        "gross": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, "$subtotal"]}},
        "gross_total": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, "$total"]}},
        "discounts": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, "$discount"]}},
        "shipping": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, "$shipping"]}},
        "collected": {"$sum": "$payment.amount_paid"}, "refunds": {"$sum": {"$ifNull": ["$payment.amount_refunded", 0]}},
        "valid_orders": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, 1]}}}}]).to_list(1)
    f = fin[0] if fin else {"orders": 0, "order_value": 0, "gross": 0, "gross_total": 0, "discounts": 0, "shipping": 0, "collected": 0, "refunds": 0, "valid_orders": 0}
    series = await db.orders.aggregate([{"$match": rng}, {"$group": {
        "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at", "timezone": "Asia/Dhaka"}},
        "orders": {"$sum": 1}, "sales": {"$sum": {"$cond": [{"$eq": ["$status", "cancelled"]}, 0, "$total"]}}, "collected": {"$sum": "$payment.amount_paid"}}},
        {"$sort": {"_id": 1}}]).to_list(400)
    products_total = await db.products.count_documents({"status": {"$ne": "archived"}})
    low = 0
    out = await db.products.count_documents({"status": {"$ne": "archived"}, "in_stock": False})
    async for p in db.products.find({"manage_stock": True, "status": {"$ne": "archived"}}, {"_id": 0, "stock_quantity": 1, "variants": 1, "low_stock_threshold": 1}):
        thr = p.get("low_stock_threshold") or 0
        qtys = [v.get("stock_quantity") or 0 for v in p.get("variants") or []] or [p.get("stock_quantity") or 0]
        low += sum(1 for q in qtys if 0 < q <= thr)
    best = await db.orders.aggregate([{"$match": {**rng, "status": {"$ne": "cancelled"}}}, {"$unwind": "$items"},
                                      {"$group": {"_id": "$items.product_id", "name": {"$first": "$items.name"}, "units": {"$sum": "$items.quantity"}, "revenue": {"$sum": "$items.line_total"}}},
                                      {"$sort": {"units": -1}}, {"$limit": 5}]).to_list(5)
    ev = lambda t: [{"$match": {**rng, "type": t, "product_id": {"$ne": None}}}, {"$group": {"_id": "$product_id", "n": {"$sum": "$quantity" if t == "add_to_cart" else 1}}}, {"$sort": {"n": -1}}, {"$limit": 5}]
    viewed = await db.events.aggregate(ev("product_view")).to_list(5)
    carted = await db.events.aggregate(ev("add_to_cart")).to_list(5)
    names = {p["id"]: p["name"] async for p in db.products.find({"id": {"$in": [x["_id"] for x in viewed + carted]}}, {"id": 1, "name": 1})}
    recent = await db.orders.find({}, {"_id": 0, "id": 1, "order_number": 1, "customer.name": 1, "total": 1, "status": 1, "payment.status": 1, "created_at": 1}).sort("created_at", -1).limit(8).to_list(8)
    inq = await db.inquiries.find({}, {"_id": 0, "id": 1, "ref": 1, "name": 1, "subject": 1, "status": 1, "created_at": 1}).sort("created_at", -1).limit(5).to_list(5)
    vo = f["valid_orders"] or 0
    return {
        "range": {"from": start, "to": end},
        "orders": {"total": f["orders"], "today": await db.orders.count_documents({"created_at": {"$gte": today}}), "by_status": status_counts,
                   "pending": status_counts.get("placed", 0) + status_counts.get("confirmed", 0),
                   "processing": status_counts.get("processing", 0) + status_counts.get("packaging", 0) + status_counts.get("dispatched", 0) + status_counts.get("out_for_delivery", 0),
                   "delivered": status_counts.get("delivered", 0), "cancelled": status_counts.get("cancelled", 0)},
        "finance": {"order_value": round(f["order_value"], 2), "gross_sales": round(f["gross"], 2), "gross_total": round(f["gross_total"], 2),
                    "discounts": round(f["discounts"], 2), "shipping": round(f["shipping"], 2), "collected": round(f["collected"], 2),
                    "refunds": round(f["refunds"], 2), "net_revenue": round(f["collected"] - f["refunds"], 2),
                    "outstanding": round(f["gross_total"] - f["collected"], 2), "aov": round(f["gross_total"] / vo, 2) if vo else 0},
        "catalog": {"products": products_total, "low_stock": low, "out_of_stock": out},
        "customers": {"total": await db.users.count_documents({"role": "customer"}),
                      "new": await db.users.count_documents({"role": "customer", "created_at": {"$gte": start, "$lte": end}})},
        "series": [{"date": s["_id"], "orders": s["orders"], "sales": round(s["sales"], 2), "collected": round(s["collected"], 2)} for s in series],
        "bestsellers": [{"product_id": b["_id"], "name": b["name"], "units": b["units"], "revenue": round(b["revenue"], 2)} for b in best],
        "most_viewed": [{"product_id": v["_id"], "name": names.get(v["_id"]), "count": v["n"]} for v in viewed],
        "most_carted": [{"product_id": v["_id"], "name": names.get(v["_id"]), "count": v["n"]} for v in carted],
        "recent_orders": recent, "recent_inquiries": inq,
    }


@router.get("/analytics")
async def analytics(date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, user=Depends(require_perm("analytics.read"))):
    start, end = period(date_from, date_to)
    rng = {"created_at": {"$gte": start, "$lte": end}}
    per = {}
    async for r in db.events.aggregate([{"$match": {**rng, "product_id": {"$ne": None}}}, {"$group": {"_id": {"p": "$product_id", "t": "$type"}, "n": {"$sum": "$quantity"}, "c": {"$sum": 1}}}]):
        d = per.setdefault(r["_id"]["p"], {})
        d[r["_id"]["t"]] = r["c"] if r["_id"]["t"] == "product_view" else r["n"]
    sales = {r["_id"]: r async for r in db.orders.aggregate([{"$match": {**rng, "status": {"$ne": "cancelled"}}}, {"$unwind": "$items"},
                                                             {"$group": {"_id": "$items.product_id", "units": {"$sum": "$items.quantity"}, "revenue": {"$sum": "$items.line_total"}, "orders": {"$addToSet": "$id"}}}])}
    ids = set(per) | set(sales)
    names = {p["id"]: p["name"] async for p in db.products.find({"id": {"$in": list(ids)}}, {"id": 1, "name": 1})}
    rows = []
    for pid in ids:
        e, s = per.get(pid, {}), sales.get(pid, {})
        views, orders = e.get("product_view", 0), len(s.get("orders", []))
        rows.append({"product_id": pid, "name": names.get(pid, "(deleted product)"), "views": views, "add_to_cart": e.get("add_to_cart", 0),
                     "removed": e.get("remove_from_cart", 0), "units": s.get("units", 0), "revenue": round(s.get("revenue", 0), 2), "orders": orders,
                     "conversion": round(orders / views * 100, 1) if views else None})
    funnel = {}
    for t in ("product_view", "add_to_cart", "checkout_start", "order_completed"):
        funnel[t] = len(await db.events.distinct("session_id", {**rng, "type": t, "session_id": {"$nin": [None, ""]}}))
    orders_n = await db.orders.count_documents({**rng})
    searches = await db.events.aggregate([{"$match": {**rng, "type": "search", "query": {"$ne": None}}}, {"$group": {"_id": "$query", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 15}]).to_list(15)
    starts = await db.events.count_documents({**rng, "type": "checkout_start"})
    return {"range": {"from": start, "to": end}, "products": sorted(rows, key=lambda r: -r["views"]), "funnel": funnel, "orders": orders_n,
            "checkout_starts": starts, "abandonment_rate": round((1 - min(orders_n, starts) / starts) * 100, 1) if starts else None,
            "searches": [{"query": s["_id"], "count": s["n"]} for s in searches]}


# ---------- customers ----------
@router.get("/customers")
async def customers(q: Optional[str] = None, page: int = 1, limit: int = 20, user=Depends(require_perm("customers.read"))):
    query = {"role": "customer"}
    if q:
        r = safe_regex(q)
        query["$or"] = [{"name": r}, {"email": r}, {"phone": r}, {"id": q.strip()}]
    skip, limit = page_params(page, limit)
    items = await db.users.find(query, {"_id": 0, "password_hash": 0, "token_version": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    stats = {r["_id"]: r async for r in db.orders.aggregate([{"$match": {"user_id": {"$in": [i["id"] for i in items]}, "status": {"$ne": "cancelled"}}},
                                                            {"$group": {"_id": "$user_id", "orders": {"$sum": 1}, "spent": {"$sum": "$total"}}}])}
    for i in items:
        i["orders"] = stats.get(i["id"], {}).get("orders", 0)
        i["lifetime_value"] = round(stats.get(i["id"], {}).get("spent", 0), 2)
    return {"items": items, "total": await db.users.count_documents(query)}


@router.get("/customers/{cid}")
async def customer(cid: str, user=Depends(require_perm("customers.read"))):
    c = await db.users.find_one({"id": cid, "role": "customer"}, {"_id": 0, "password_hash": 0, "token_version": 0})
    if not c:
        raise HTTPException(404, "Customer not found")
    c["orders"] = await db.orders.find({"user_id": cid}, {"_id": 0, "id": 1, "order_number": 1, "total": 1, "status": 1, "payment.status": 1, "created_at": 1}).sort("created_at", -1).to_list(100)
    c["lifetime_value"] = round(sum(o["total"] for o in c["orders"] if o["status"] != "cancelled"), 2)
    c["inquiries"] = await db.inquiries.find({"user_id": cid}, {"_id": 0, "id": 1, "ref": 1, "subject": 1, "status": 1, "created_at": 1}).to_list(50)
    return c


class CustomerUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(default="", max_length=20)
    active: bool = True


@router.put("/customers/{cid}")
async def update_customer(cid: str, body: CustomerUpdate, user=Depends(require_perm("customers.write"))):
    c = await db.users.find_one({"id": cid, "role": "customer"})
    if not c:
        raise HTTPException(404, "Customer not found")
    upd = {"$set": body.model_dump()}
    if c.get("active", True) and not body.active:
        upd["$inc"] = {"token_version": 1}
        await db.sessions.update_many({"user_id": cid}, {"$set": {"revoked": True}})
    await db.users.update_one({"id": cid}, upd)
    await audit(user, "customer.update", "customer", cid, {"active": body.active, "name": body.name})
    return {"ok": True}


@router.post("/customers/{cid}/password-reset")
async def customer_reset(cid: str, request: Request, user=Depends(require_perm("customers.write"))):
    c = await db.users.find_one({"id": cid, "role": "customer"}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Customer not found")
    await create_reset_token(c, request.headers.get("origin") or "")
    await audit(user, "customer.password_reset_initiated", "customer", cid, {})
    return {"ok": True, "delivered": channel_configured(),
            "message": "Reset link generated. No email provider is configured, so it was recorded in the notification log only."}


# ---------- marketing ----------
class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["percent", "fixed"]
    value: float = Field(gt=0)
    min_order: float = Field(default=0, ge=0)
    max_discount: Optional[float] = Field(default=None, ge=0)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    usage_limit: Optional[int] = Field(default=None, ge=0)
    per_customer_limit: Optional[int] = Field(default=None, ge=0)
    active: bool = True
    description: str = Field(default="", max_length=200)


@router.get("/coupons")
async def coupons(user=Depends(require_perm("marketing.write"))):
    return {"items": await db.coupons.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)}


@router.post("/coupons")
async def create_coupon(body: CouponIn, user=Depends(require_perm("marketing.write"))):
    if body.type == "percent" and body.value > 100:
        raise HTTPException(422, "Percentage cannot exceed 100")
    d = {**body.model_dump(), "code": body.code.upper(), "used_count": 0, "id": new_id(), "created_at": now()}
    try:
        await db.coupons.insert_one(d)
    except DuplicateKeyError:
        raise HTTPException(400, "Coupon code already exists")
    await audit(user, "coupon.create", "coupon", d["code"], {"type": d["type"], "value": d["value"]})
    return clean(d)


@router.put("/coupons/{code}")
async def update_coupon(code: str, body: CouponIn, user=Depends(require_perm("marketing.write"))):
    d = {**body.model_dump(), "code": code.upper()}
    r = await db.coupons.update_one({"code": code.upper()}, {"$set": d})
    if not r.matched_count:
        raise HTTPException(404, "Coupon not found")
    await audit(user, "coupon.update", "coupon", code, {"active": d["active"], "value": d["value"]})
    return {"ok": True}


@router.delete("/coupons/{code}")
async def delete_coupon(code: str, user=Depends(require_perm("marketing.write"))):
    c = await db.coupons.find_one({"code": code.upper()})
    if not c:
        raise HTTPException(404, "Coupon not found")
    if c.get("used_count"):
        await db.coupons.update_one({"code": code.upper()}, {"$set": {"active": False}})
        return {"deactivated": True}
    await db.coupons.delete_one({"code": code.upper()})
    await audit(user, "coupon.delete", "coupon", code, {})
    return {"deleted": True}


class BannerIn(BaseModel):
    title: str = Field(max_length=120)
    subtitle: str = Field(default="", max_length=300)
    image: str = Field(max_length=600)
    link: str = Field(default="/shop", max_length=300)
    cta: str = Field(default="Shop now", max_length=40)
    placement: Literal["hero", "promo"] = "promo"
    active: bool = True
    sort_order: int = 0


@router.get("/banners")
async def banners(user=Depends(require_perm("marketing.write"))):
    return {"items": await db.banners.find({}, {"_id": 0}).sort("sort_order", 1).to_list(100)}


@router.post("/banners")
async def create_banner(body: BannerIn, user=Depends(require_perm("marketing.write"))):
    d = {"id": new_id(), **body.model_dump(), "created_at": now()}
    await db.banners.insert_one(d)
    await audit(user, "banner.create", "banner", d["id"], {"title": d["title"]})
    return clean(d)


@router.put("/banners/{bid}")
async def update_banner(bid: str, body: BannerIn, user=Depends(require_perm("marketing.write"))):
    await db.banners.update_one({"id": bid}, {"$set": body.model_dump()})
    await audit(user, "banner.update", "banner", bid, {"active": body.active})
    return {"ok": True}


@router.delete("/banners/{bid}")
async def delete_banner(bid: str, user=Depends(require_perm("marketing.write"))):
    await db.banners.delete_one({"id": bid})
    await audit(user, "banner.delete", "banner", bid, {})
    return {"ok": True}


@router.get("/newsletter")
async def subscribers(user=Depends(require_perm("marketing.write"))):
    return {"items": await db.newsletter.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)}


# ---------- content ----------
@router.get("/settings")
async def get_site_settings(user=Depends(require_perm("content.write"))):
    return await db.settings.find_one({"key": "site"}, {"_id": 0}) or {}


@router.put("/settings")
async def put_settings(body: dict, user=Depends(require_perm("content.write"))):
    allowed = {"announcement", "hero", "philosophy", "contact", "footer", "faqs", "seo", "social", "shipping", "payments", "districts", "home"}
    upd = {k: v for k, v in body.items() if k in allowed}
    if ("shipping" in upd or "payments" in upd) and "orders.finance" not in await permissions_for(user):
        raise HTTPException(403, "Changing shipping or payment settings requires finance permission")
    await db.settings.update_one({"key": "site"}, {"$set": {**upd, "updated_at": now()}}, upsert=True)
    await audit(user, "settings.update", "settings", "site", {"keys": list(upd)})
    return {"ok": True}


class PageIn(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(max_length=160)
    body: str = Field(default="", max_length=60000)
    status: Literal["published", "draft"] = "published"
    seo: dict = {}


@router.get("/pages")
async def pages(user=Depends(require_perm("content.write"))):
    return {"items": await db.pages.find({}, {"_id": 0}).sort("title", 1).to_list(200)}


@router.put("/pages/{slug}")
async def upsert_page(slug: str, body: PageIn, user=Depends(require_perm("content.write"))):
    await db.pages.update_one({"slug": slug}, {"$set": {**body.model_dump(), "updated_at": now()}, "$setOnInsert": {"created_at": now()}}, upsert=True)
    await audit(user, "page.update", "page", slug, {"title": body.title, "status": body.status})
    return {"ok": True}


# ---------- inquiries ----------
@router.get("/inquiries")
async def inquiries(q: Optional[str] = None, status: Optional[str] = None, kind: Optional[str] = None, page: int = 1,
                    user=Depends(require_perm("inquiries.read"))):
    query = {}
    if q:
        r = safe_regex(q)
        query["$or"] = [{"ref": r}, {"name": r}, {"email": r}, {"phone": r}, {"subject": r}, {"message": r}, {"order_number": r}]
    if status:
        query["status"] = status
    if kind:
        query["kind"] = kind
    skip, limit = page_params(page, 25)
    return {"items": await db.inquiries.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit),
            "total": await db.inquiries.count_documents(query), "reply_channel_configured": channel_configured()}


class InquiryUpdate(BaseModel):
    status: Optional[Literal["new", "open", "awaiting_customer", "resolved", "closed"]] = None
    assigned_to: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    reply: Optional[str] = Field(default=None, max_length=5000)


@router.patch("/inquiries/{iid}")
async def update_inquiry(iid: str, body: InquiryUpdate, user=Depends(require_perm("inquiries.write"))):
    inq = await db.inquiries.find_one({"id": iid}, {"_id": 0})
    if not inq:
        raise HTTPException(404, "Inquiry not found")
    upd = {"$set": {"updated_at": now()}}
    if body.status:
        upd["$set"]["status"] = body.status
    if body.assigned_to is not None:
        upd["$set"]["assigned_to"] = body.assigned_to or None
    push = {}
    if body.note:
        push["notes"] = {"text": body.note, "by": user["email"], "at": now()}
    delivered = None
    if body.reply:
        n = await notify("inquiry_reply", inq.get("email") or inq.get("phone"), f"Re: {inq['subject']} [{inq['ref']}]", body.reply, iid)
        delivered = n["delivered"]
        push["replies"] = {"message": body.reply, "by": user["email"], "at": now(), "channel": n["channel"], "delivered": delivered}
        upd["$set"].setdefault("status", "awaiting_customer")
    if push:
        upd["$push"] = push
    await db.inquiries.update_one({"id": iid}, upd)
    await audit(user, "inquiry.update", "inquiry", iid, {"status": body.status, "replied": bool(body.reply)})
    return {"ok": True, "reply_delivered": delivered}


# ---------- staff & roles ----------
class StaffIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=80)
    role: str
    password: Optional[str] = Field(default=None, min_length=10, max_length=128)
    active: bool = True


@router.get("/users")
async def staff(user=Depends(require_perm("users.manage"))):
    items = await db.users.find({"role": {"$in": list(STAFF_ROLES)}}, {"_id": 0, "password_hash": 0}).sort("created_at", 1).to_list(200)
    roles = await db.roles.find({}, {"_id": 0}).to_list(50)
    return {"items": items, "roles": roles, "all_permissions": ALL_PERMISSIONS}


@router.post("/users")
async def create_staff(body: StaffIn, user=Depends(require_perm("users.manage"))):
    if body.role not in STAFF_ROLES or not body.password:
        raise HTTPException(422, "Valid staff role and an initial password (10+ chars) are required")
    d = {"id": new_id(), "email": body.email.lower(), "name": body.name, "role": body.role, "active": body.active, "token_version": 0,
         "password_hash": hash_password(body.password), "addresses": [], "wishlist": [], "created_at": now()}
    try:
        await db.users.insert_one(d)
    except DuplicateKeyError:
        raise HTTPException(400, "A user with this email already exists")
    await audit(user, "staff.create", "user", d["id"], {"email": d["email"], "role": d["role"]})
    return {"ok": True, "id": d["id"]}


@router.put("/users/{uid}")
async def update_staff(uid: str, body: StaffIn, user=Depends(require_perm("users.manage"))):
    if body.role not in STAFF_ROLES:
        raise HTTPException(422, "Invalid role")
    target = await db.users.find_one({"id": uid, "role": {"$in": list(STAFF_ROLES)}})
    if not target:
        raise HTTPException(404, "Staff user not found")
    if uid == user["id"] and (body.role != "admin" or not body.active):
        raise HTTPException(400, "You cannot demote or disable your own account")
    upd = {"$set": {"name": body.name, "role": body.role, "active": body.active}}
    if body.password:
        upd["$set"]["password_hash"] = hash_password(body.password)
    if target["role"] != body.role or not body.active or body.password:
        upd["$inc"] = {"token_version": 1}
    await db.users.update_one({"id": uid}, upd)
    await audit(user, "staff.update", "user", uid, {"role": {"from": target["role"], "to": body.role}, "active": body.active})
    return {"ok": True}


class RoleIn(BaseModel):
    permissions: List[str]


@router.put("/roles/{key}")
async def update_role(key: str, body: RoleIn, user=Depends(require_perm("users.manage"))):
    if key == "admin":
        raise HTTPException(400, "Administrator role always has all permissions")
    perms = [p for p in body.permissions if p in ALL_PERMISSIONS]
    old = await db.roles.find_one({"key": key})
    if not old:
        raise HTTPException(404, "Role not found")
    await db.roles.update_one({"key": key}, {"$set": {"permissions": perms}})
    await audit(user, "role.permissions", "role", key, {"from": old["permissions"], "to": perms})
    return {"ok": True}


@router.get("/staff-directory")
async def directory(user=Depends(require_staff())):
    return {"items": await db.users.find({"role": {"$in": list(STAFF_ROLES)}, "active": True}, {"_id": 0, "id": 1, "name": 1, "email": 1}).to_list(200)}


# ---------- audit & migration ----------
@router.get("/audit-logs")
async def audit_logs(entity: Optional[str] = None, q: Optional[str] = None, page: int = 1, user=Depends(require_perm("audit.read"))):
    query = {}
    if entity:
        query["entity"] = entity
    if q:
        r = safe_regex(q)
        query["$or"] = [{"action": r}, {"actor_email": r}, {"entity_id": r}]
    skip, limit = page_params(page, 50)
    return {"items": await db.audit_logs.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit),
            "total": await db.audit_logs.count_documents(query)}


@router.get("/notifications")
async def notifications(user=Depends(require_perm("audit.read"))):
    return {"items": await db.notifications.find({}, {"_id": 0, "body": 0}).sort("created_at", -1).limit(100).to_list(100), "configured": channel_configured()}


@router.get("/migration/report")
async def migration_report(user=Depends(require_perm("migration.run"))):
    return await db.migration_reports.find_one({}, {"_id": 0}, sort=[("finished_at", -1)]) or {}


class MigrationIn(BaseModel):
    mode: Literal["insert_only", "update_existing"] = "insert_only"


@router.post("/migration/run")
async def migration_run(body: MigrationIn, bg: BackgroundTasks, user=Depends(require_perm("migration.run"))):
    from migration.wp_import import run_migration
    await audit(user, "migration.run", "migration", "wordpress", {"mode": body.mode})
    bg.add_task(run_migration, body.mode, user.get("email"))
    return {"started": True}

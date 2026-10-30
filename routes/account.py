from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, EmailStr
from pymongo.errors import DuplicateKeyError
from core.db import db
from core.utils import now, new_id, rate_limit, clean
from core.security import get_current_user, optional_user, verify_password, hash_password, public_user
from services.pricing import get_settings
from services.notifications import notify
from routes.catalog import public_product, live_query

router = APIRouter(tags=["customers"])


class ProfileIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(default="", max_length=20)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class Address(BaseModel):
    label: str = Field(default="Home", max_length=30)
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=11, max_length=14)
    address: str = Field(min_length=5, max_length=300)
    district: str = Field(min_length=2, max_length=60)
    area: str = Field(default="", max_length=80)
    is_default: bool = False


@router.get("/customers/me")
async def me(user=Depends(get_current_user)):
    return {**public_user(user), "addresses": user.get("addresses", [])}


@router.patch("/customers/me")
async def update_me(body: ProfileIn, user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$set": body.model_dump()})
    return {"ok": True}


@router.post("/customers/me/password")
async def change_password(body: PasswordIn, user=Depends(get_current_user)):
    full = await db.users.find_one({"id": user["id"]})
    if not verify_password(body.current_password, full["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    return {"ok": True}


@router.post("/customers/me/addresses")
async def add_address(body: Address, user=Depends(get_current_user)):
    addrs = user.get("addresses", [])
    if len(addrs) >= 10:
        raise HTTPException(400, "You can save up to 10 addresses")
    a = {"id": new_id(), **body.model_dump()}
    if a["is_default"] or not addrs:
        a["is_default"] = True
        for x in addrs:
            x["is_default"] = False
    addrs.append(a)
    await db.users.update_one({"id": user["id"]}, {"$set": {"addresses": addrs}})
    return {"items": addrs}


@router.put("/customers/me/addresses/{aid}")
async def edit_address(aid: str, body: Address, user=Depends(get_current_user)):
    addrs = user.get("addresses", [])
    if not any(a["id"] == aid for a in addrs):
        raise HTTPException(404, "Address not found")
    for i, a in enumerate(addrs):
        if a["id"] == aid:
            addrs[i] = {"id": aid, **body.model_dump()}
        elif body.is_default:
            a["is_default"] = False
    await db.users.update_one({"id": user["id"]}, {"$set": {"addresses": addrs}})
    return {"items": addrs}


@router.delete("/customers/me/addresses/{aid}")
async def del_address(aid: str, user=Depends(get_current_user)):
    addrs = [a for a in user.get("addresses", []) if a["id"] != aid]
    if addrs and not any(a.get("is_default") for a in addrs):
        addrs[0]["is_default"] = True
    await db.users.update_one({"id": user["id"]}, {"$set": {"addresses": addrs}})
    return {"items": addrs}


@router.get("/wishlist")
async def wishlist(user=Depends(get_current_user)):
    ids = user.get("wishlist", [])
    docs = {p["id"]: p async for p in db.products.find({**live_query(), "id": {"$in": ids}}, {"_id": 0})}
    return {"ids": ids, "items": [public_product(docs[i]) for i in ids if i in docs]}


@router.post("/wishlist/{product_id}")
async def wish_add(product_id: str, user=Depends(get_current_user)):
    if not await db.products.find_one({"id": product_id}):
        raise HTTPException(404, "Product not found")
    await db.users.update_one({"id": user["id"]}, {"$addToSet": {"wishlist": product_id}})
    return {"ok": True}


@router.delete("/wishlist/{product_id}")
async def wish_del(product_id: str, user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$pull": {"wishlist": product_id}})
    return {"ok": True}


class InquiryIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: Optional[EmailStr] = None
    phone: str = Field(default="", max_length=20)
    subject: str = Field(default="General inquiry", max_length=120)
    message: str = Field(min_length=5, max_length=3000)
    product_id: Optional[str] = Field(default=None, max_length=64)
    order_number: Optional[str] = Field(default=None, max_length=40)
    kind: Literal["general", "product", "order", "custom"] = "general"


@router.post("/inquiries")
async def create_inquiry(body: InquiryIn, request: Request):
    rate_limit(request, "inquiry", 5, 300)
    if not body.email and not body.phone:
        raise HTTPException(422, "Please provide an email or phone number so we can reply")
    user = await optional_user(request)
    product = await db.products.find_one({"id": body.product_id}, {"_id": 0, "name": 1, "slug": 1}) if body.product_id else None
    ref = f"INQ-{new_id()[:6].upper()}"
    doc = {"id": new_id(), "ref": ref, **body.model_dump(), "product_name": (product or {}).get("name"), "user_id": user["id"] if user else None,
           "status": "new", "assigned_to": None, "notes": [], "replies": [], "created_at": now(), "updated_at": now()}
    await db.inquiries.insert_one(doc)
    s = await get_settings()
    await notify("admin_new_inquiry", s.get("contact", {}).get("email", ""), f"New inquiry {ref}", body.subject, doc["id"])
    return {"ok": True, "ref": ref}


@router.get("/inquiries/mine")
async def my_inquiries(user=Depends(get_current_user)):
    docs = await db.inquiries.find({"user_id": user["id"]}, {"_id": 0, "notes": 0, "assigned_to": 0}).sort("created_at", -1).to_list(100)
    return {"items": docs}


@router.get("/content/site")
async def site():
    s = await get_settings()
    s.pop("payments", None)
    banners = await db.banners.find({"active": True}, {"_id": 0}).sort("sort_order", 1).to_list(20)
    featured_cats = await db.categories.find({"status": "active", "featured": True}, {"_id": 0}).sort("sort_order", 1).to_list(12)
    return {"settings": s, "banners": banners, "featured_categories": featured_cats}


@router.get("/content/pages/{slug}")
async def page(slug: str):
    p = await db.pages.find_one({"slug": slug, "status": "published"}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Page not found")
    return p


class NewsletterIn(BaseModel):
    email: EmailStr


@router.post("/newsletter")
async def newsletter(body: NewsletterIn, request: Request):
    rate_limit(request, "newsletter", 5, 300)
    try:
        await db.newsletter.insert_one({"email": body.email.lower(), "created_at": now(), "status": "subscribed"})
    except DuplicateKeyError:
        pass
    return {"ok": True}


EVENT_TYPES = {"product_view", "product_engagement", "add_to_cart", "remove_from_cart", "checkout_start", "search"}


class EventIn(BaseModel):
    type: str = Field(max_length=30)
    session_id: str = Field(min_length=8, max_length=64)
    product_id: Optional[str] = Field(default=None, max_length=64)
    query: Optional[str] = Field(default=None, max_length=80)
    quantity: int = Field(default=1, ge=1, le=99)


@router.post("/analytics/events")
async def event(body: EventIn, request: Request):
    rate_limit(request, "events", 120, 60)
    if body.type not in EVENT_TYPES:
        raise HTTPException(422, "Unknown event type")
    user = await optional_user(request)
    t = now()
    doc = {"type": body.type, "session_id": body.session_id, "product_id": body.product_id, "quantity": body.quantity,
           "query": body.query.strip().lower() if body.query else None, "user": bool(user), "created_at": t}
    if body.type in ("product_view", "checkout_start", "search", "product_engagement"):
        doc["dedupe"] = f"{body.type}:{body.session_id}:{body.product_id or body.query}:{t:%Y%m%d}"
    try:
        await db.events.insert_one(doc)
        if body.type == "product_view" and body.product_id:
            await db.products.update_one({"id": body.product_id}, {"$inc": {"views": 1}})
    except DuplicateKeyError:
        return {"ok": True, "deduplicated": True}
    return {"ok": True}

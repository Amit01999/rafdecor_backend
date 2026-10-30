from datetime import datetime
from typing import List, Optional, Literal, Dict
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError
from core.db import db
from core.utils import now, new_id, slugify, safe_regex, page_params, audit, diff
from core.security import require_perm
from services import inventory, media
from services.inventory import denormalize, refresh_flags

router = APIRouter(prefix="/admin", tags=["admin catalog"])


class Image(BaseModel):
    url: str = Field(max_length=600)
    public_id: Optional[str] = None
    alt: str = Field(default="", max_length=200)
    source_url: Optional[str] = None


class Spec(BaseModel):
    label: str = Field(max_length=80)
    value: str = Field(max_length=300)


class Attribute(BaseModel):
    name: str = Field(max_length=60)
    values: List[str]


class Variant(BaseModel):
    id: Optional[str] = None
    sku: str = Field(default="", max_length=60)
    attributes: Dict[str, str]
    price: float = Field(ge=0)
    sale_price: Optional[float] = Field(default=None, ge=0)
    stock_quantity: Optional[int] = Field(default=None, ge=0)
    stock_status: Literal["in_stock", "out_of_stock"] = "in_stock"
    wp_id: Optional[int] = None


class Seo(BaseModel):
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=320)


class ReorderIn(BaseModel):
    ids: List[str]


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: Optional[str] = Field(default=None, max_length=120)
    short_description: str = Field(default="", max_length=3000)
    description: str = Field(default="", max_length=30000)
    category_ids: List[str] = []
    status: Literal["draft", "published", "archived"] = "draft"
    publish_at: Optional[datetime] = None
    featured: bool = False
    sku: Optional[str] = Field(default="", max_length=60)
    price: float = Field(ge=0)
    sale_price: Optional[float] = Field(default=None, ge=0)
    manage_stock: bool = False
    stock_quantity: Optional[int] = Field(default=None, ge=0)
    stock_status: Literal["in_stock", "out_of_stock"] = "in_stock"
    low_stock_threshold: int = Field(default=2, ge=0)
    attributes: List[Attribute] = []
    variants: List[Variant] = []
    images: List[Image] = []
    specs: List[Spec] = []
    dimensions: str = Field(default="", max_length=200)
    materials: str = Field(default="", max_length=200)
    shipping_charge: float = Field(default=0, ge=0)
    weight: float = Field(default=0, ge=0)  # kg; used for multi-product weight-based delivery pricing
    extra_qty_charge: float = Field(default=0, ge=0)  # extra delivery charge per unit beyond the first, for this product
    tags: List[str] = []
    seo: Seo = Seo()
    sort_order: int = 0


TRACKED = ["name", "slug", "price", "sale_price", "status", "featured", "stock_quantity", "manage_stock", "category_ids", "shipping_charge", "weight", "extra_qty_charge", "sku"]


def _prepare(body: ProductIn) -> dict:
    d = body.model_dump()
    d["slug"] = slugify(d["slug"] or d["name"])
    if d["sale_price"] is not None and d["sale_price"] >= d["price"]:
        raise HTTPException(422, "Sale price must be lower than the regular price")
    for v in d["variants"]:
        v["id"] = v["id"] or new_id()
        if v["sale_price"] is not None and v["sale_price"] >= v["price"]:
            raise HTTPException(422, "Variant sale price must be lower than its regular price")
    d["sku"] = (d["sku"] or "").strip() or None
    return denormalize(d)


@router.get("/products")
async def list_products(q: Optional[str] = None, status: Optional[str] = None, category: Optional[str] = None,
                        stock: Optional[str] = None, featured: Optional[bool] = None, page: int = 1, limit: int = 20,
                        user=Depends(require_perm("products.read"))):
    query = {}
    if q:
        r = safe_regex(q)
        query["$or"] = [{"name": r}, {"sku": r}, {"slug": r}]
    if status:
        query["status"] = status
    if category:
        query["category_ids"] = category
    if stock == "out":
        query["in_stock"] = False
    if featured is not None:
        query["featured"] = featured
    skip, limit = page_params(page, limit)
    total = await db.products.count_documents(query)
    # Featured products are managed as an explicitly ordered list (homepage
    # "Exquisite Creations"), so sort by that order instead of recency when
    # the admin is specifically asking for the featured set.
    sort = [("sort_order", 1), ("created_at", -1)] if featured else [("created_at", -1)]
    items = await db.products.find(query, {"_id": 0, "description": 0}).sort(sort).skip(skip).limit(limit).to_list(limit)
    return {"items": items, "total": total}


@router.get("/products/{pid}")
async def get_product(pid: str, user=Depends(require_perm("products.read"))):
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found")
    return p


@router.post("/products")
async def create_product(body: ProductIn, user=Depends(require_perm("products.write"))):
    d = _prepare(body)
    d.update({"id": new_id(), "views": 0, "sales_count": 0, "created_at": now(), "updated_at": now(), "created_by": user["email"]})
    try:
        await db.products.insert_one(d)
    except DuplicateKeyError:
        raise HTTPException(400, "Slug or SKU already in use")
    await audit(user, "product.create", "product", d["id"], {"name": d["name"], "price": d["price"]})
    d.pop("_id", None)
    return d


@router.put("/products/{pid}")
async def update_product(pid: str, body: ProductIn, user=Depends(require_perm("products.write"))):
    old = await db.products.find_one({"id": pid}, {"_id": 0})
    if not old:
        raise HTTPException(404, "Product not found")
    d = _prepare(body)
    d["updated_at"] = now()
    try:
        await db.products.update_one({"id": pid}, {"$set": d})
    except DuplicateKeyError:
        raise HTTPException(400, "Slug or SKU already in use")
    changes = diff(old, d, TRACKED)
    if old.get("stock_quantity") != d.get("stock_quantity") and d["manage_stock"]:
        await db.stock_movements.insert_one({"id": new_id(), "product_id": pid, "variant_id": None, "delta": (d["stock_quantity"] or 0) - (old.get("stock_quantity") or 0),
                                             "reason": "product_edit", "actor_email": user["email"], "resulting_quantity": d["stock_quantity"], "created_at": now()})
    action = "product.price_change" if "price" in changes or "sale_price" in changes else "product.update"
    await audit(user, action, "product", pid, changes)
    return await db.products.find_one({"id": pid}, {"_id": 0})


class BulkIn(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=200)
    action: Literal["publish", "unpublish", "archive", "feature", "unfeature"]


@router.post("/products/bulk")
async def bulk(body: BulkIn, user=Depends(require_perm("products.write"))):
    upd = {"publish": {"status": "published"}, "unpublish": {"status": "draft"}, "archive": {"status": "archived"},
           "feature": {"featured": True}, "unfeature": {"featured": False}}[body.action]
    r = await db.products.update_many({"id": {"$in": body.ids}}, {"$set": {**upd, "updated_at": now()}})
    await audit(user, f"product.bulk_{body.action}", "product", ",".join(body.ids[:20]), {"count": r.modified_count})
    return {"modified": r.modified_count}


@router.post("/products/reorder")
async def reorder_products(body: ReorderIn, user=Depends(require_perm("products.write"))):
    for i, pid in enumerate(body.ids):
        await db.products.update_one({"id": pid}, {"$set": {"sort_order": i}})
    await audit(user, "product.reorder", "product", "*", {"order": body.ids})
    return {"ok": True}


@router.delete("/products/{pid}")
async def delete_product(pid: str, user=Depends(require_perm("products.write"))):
    p = await db.products.find_one({"id": pid}, {"_id": 0, "name": 1})
    if not p:
        raise HTTPException(404, "Product not found")
    if await db.orders.find_one({"items.product_id": pid}):
        await db.products.update_one({"id": pid}, {"$set": {"status": "archived", "updated_at": now()}})
        await audit(user, "product.archive", "product", pid, {"reason": "referenced by orders"})
        return {"archived": True, "message": "Product is referenced by orders, so it was archived instead of deleted."}
    await db.products.delete_one({"id": pid})
    await audit(user, "product.delete", "product", pid, {"name": p["name"]})
    return {"deleted": True}


@router.post("/uploads")
async def upload(file: UploadFile = File(...), user=Depends(require_perm("products.write"))):
    if not media.configured():
        raise HTTPException(503, "Image storage (Cloudinary) is not configured")
    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/avif"):
        raise HTTPException(415, "Only JPEG, PNG, WebP or AVIF images are allowed")
    data = await file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image must be 8 MB or smaller")
    res = media.upload(data, folder="rafdecor/products")
    await audit(user, "media.upload", "media", res["public_id"], {})
    return res


# ---------- categories ----------
class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    slug: Optional[str] = Field(default=None, max_length=90)
    description: str = Field(default="", max_length=2000)
    image: Optional[str] = Field(default=None, max_length=600)
    parent_id: Optional[str] = None
    featured: bool = False
    sort_order: int = 0
    status: Literal["active", "archived"] = "active"
    seo: Seo = Seo()


@router.get("/categories")
async def list_categories(user=Depends(require_perm("products.read"))):
    cats = await db.categories.find({}, {"_id": 0}).sort([("sort_order", 1), ("name", 1)]).to_list(500)
    counts = {r["_id"]: r["n"] for r in await db.products.aggregate([{"$unwind": "$category_ids"}, {"$group": {"_id": "$category_ids", "n": {"$sum": 1}}}]).to_list(500)}
    for c in cats:
        c["product_count"] = counts.get(c["id"], 0)
    return {"items": cats}


@router.post("/categories")
async def create_category(body: CategoryIn, user=Depends(require_perm("products.write"))):
    d = body.model_dump()
    d.update({"id": new_id(), "slug": slugify(d["slug"] or d["name"]), "created_at": now(), "updated_at": now()})
    try:
        await db.categories.insert_one(d)
    except DuplicateKeyError:
        raise HTTPException(400, "Slug already in use")
    await audit(user, "category.create", "category", d["id"], {"name": d["name"]})
    d.pop("_id", None)
    return d


@router.put("/categories/{cid}")
async def update_category(cid: str, body: CategoryIn, user=Depends(require_perm("products.write"))):
    if body.parent_id == cid:
        raise HTTPException(422, "A category cannot be its own parent")
    d = body.model_dump()
    d.update({"slug": slugify(d["slug"] or d["name"]), "updated_at": now()})
    r = await db.categories.update_one({"id": cid}, {"$set": d})
    if not r.matched_count:
        raise HTTPException(404, "Category not found")
    await audit(user, "category.update", "category", cid, {"name": d["name"], "status": d["status"]})
    return {"ok": True}


@router.post("/categories/reorder")
async def reorder(body: ReorderIn, user=Depends(require_perm("products.write"))):
    for i, cid in enumerate(body.ids):
        await db.categories.update_one({"id": cid}, {"$set": {"sort_order": i}})
    await audit(user, "category.reorder", "category", "*", {"order": body.ids})
    return {"ok": True}


# ---------- inventory ----------
@router.get("/inventory")
async def inventory_list(filter: Optional[str] = None, user=Depends(require_perm("products.read"))):
    rows = []
    async for p in db.products.find({"status": {"$ne": "archived"}}, {"_id": 0, "id": 1, "name": 1, "sku": 1, "manage_stock": 1, "stock_quantity": 1,
                                                                     "stock_status": 1, "low_stock_threshold": 1, "variants": 1, "images": {"$slice": 1}}):
        entries = [(v, v["id"], ", ".join(v["attributes"].values())) for v in p.get("variants") or []] or [(p, None, None)]
        for src, vid, label in entries:
            qty = src.get("stock_quantity")
            thr = p.get("low_stock_threshold") or 0
            state = ("untracked_in" if src.get("stock_status", "in_stock") == "in_stock" else "out") if not p.get("manage_stock") else ("out" if (qty or 0) <= 0 else "low" if qty <= thr else "ok")
            rows.append({"product_id": p["id"], "variant_id": vid, "name": p["name"], "variant": label, "sku": src.get("sku") or p.get("sku"),
                         "manage_stock": p.get("manage_stock", False), "stock_quantity": qty, "threshold": thr, "state": state,
                         "image": (p.get("images") or [{}])[0].get("url")})
    if filter:
        rows = [r for r in rows if r["state"] == filter]
    return {"items": rows}


class AdjustIn(BaseModel):
    product_id: str
    variant_id: Optional[str] = None
    delta: int = Field(ge=-10000, le=10000)
    reason: Literal["restock", "damaged", "correction", "returned_resalable", "lost", "other"]
    note: str = Field(default="", max_length=300)


@router.post("/inventory/adjust")
async def adjust(body: AdjustIn, user=Depends(require_perm("inventory.write"))):
    if body.delta == 0:
        raise HTTPException(422, "Adjustment cannot be zero")
    resulting = await inventory.adjust(body.product_id, body.variant_id, body.delta, body.reason, user, body.note or None)
    await refresh_flags(body.product_id)
    await audit(user, "inventory.adjust", "product", body.product_id, {"variant_id": body.variant_id, "delta": body.delta, "reason": body.reason, "resulting": resulting})
    return {"stock_quantity": resulting}


class TrackingIn(BaseModel):
    manage_stock: bool
    low_stock_threshold: int = Field(default=2, ge=0)


@router.post("/inventory/{pid}/tracking")
async def set_tracking(pid: str, body: TrackingIn, user=Depends(require_perm("inventory.write"))):
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found")
    upd = body.model_dump()
    if body.manage_stock and not p.get("manage_stock"):
        if p.get("variants"):
            await db.products.update_one({"id": pid}, {"$set": {f"variants.{i}.stock_quantity": v.get("stock_quantity") or 0 for i, v in enumerate(p["variants"])}})
        elif p.get("stock_quantity") is None:
            upd["stock_quantity"] = 0
    await db.products.update_one({"id": pid}, {"$set": upd})
    await refresh_flags(pid)
    await audit(user, "inventory.tracking", "product", pid, upd)
    return {"ok": True}


@router.get("/inventory/movements")
async def movements(product_id: Optional[str] = None, page: int = 1, user=Depends(require_perm("products.read"))):
    q = {"product_id": product_id} if product_id else {}
    skip, limit = page_params(page, 50)
    items = await db.stock_movements.find(q, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    names = {p["id"]: p["name"] async for p in db.products.find({"id": {"$in": list({i["product_id"] for i in items})}}, {"id": 1, "name": 1})}
    for i in items:
        i["product_name"] = names.get(i["product_id"])
    return {"items": items, "total": await db.stock_movements.count_documents(q)}


# ---------- reviews ----------
@router.get("/reviews")
async def list_reviews(status: Optional[str] = None, user=Depends(require_perm("products.read"))):
    q = {"status": status} if status else {}
    return {"items": await db.reviews.find(q, {"_id": 0}).sort("created_at", -1).to_list(200)}


class ReviewStatusIn(BaseModel):
    status: Literal["approved", "rejected", "pending"]


@router.patch("/reviews/{rid}")
async def moderate(rid: str, body: ReviewStatusIn, user=Depends(require_perm("products.write"))):
    r = await db.reviews.find_one_and_update({"id": rid}, {"$set": {"status": body.status}})
    if not r:
        raise HTTPException(404, "Review not found")
    agg = await db.reviews.aggregate([{"$match": {"product_id": r["product_id"], "status": "approved"}},
                                      {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}}]).to_list(1)
    await db.products.update_one({"id": r["product_id"]}, {"$set": {"rating": {"average": round(agg[0]["avg"], 1), "count": agg[0]["n"]} if agg else None}})
    await audit(user, "review.moderate", "review", rid, {"status": body.status})
    return {"ok": True}

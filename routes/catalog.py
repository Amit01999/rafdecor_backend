from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
from core.db import db
from core.utils import now, new_id, safe_regex, page_params
from core.security import get_current_user
from services.pricing import is_live, on_sale, effective, in_stock, stock_of, product_min_price

router = APIRouter(tags=["catalog"])

PUBLIC_FIELDS = ["id", "slug", "name", "short_description", "description", "category_ids", "featured", "sku", "price", "sale_price",
                 "attributes", "images", "specs", "dimensions", "materials", "shipping_charge", "seo", "created_at", "manage_stock", "tags"]


def public_product(p: dict, full: bool = False) -> dict:
    out = {k: p.get(k) for k in PUBLIC_FIELDS}
    if not full:
        out.pop("description", None)
        out["images"] = (p.get("images") or [])[:2]
    variants = []
    for v in p.get("variants") or []:
        s = stock_of(p, v)
        variants.append({"id": v["id"], "sku": v.get("sku"), "attributes": v.get("attributes"), "price": v.get("price"),
                         "sale_price": v.get("sale_price"), "effective_price": effective(v), "on_sale": on_sale(v),
                         "in_stock": s is None or s > 0, "available": s})
    out["variants"] = variants
    prices = [v["effective_price"] for v in variants] or [effective(p)]
    out["price_min"], out["price_max"] = min(prices), max(prices)
    out["on_sale"] = on_sale(p) or any(v["on_sale"] for v in variants)
    out["in_stock"] = in_stock(p)
    out["available"] = None if variants else stock_of(p)
    out["low_stock"] = bool(p.get("manage_stock") and out["available"] is not None and 0 < out["available"] <= (p.get("low_stock_threshold") or 0))
    out["rating"] = p.get("rating")
    return out


def live_query():
    return {"status": "published", "$or": [{"publish_at": None}, {"publish_at": {"$exists": False}}, {"publish_at": {"$lte": now()}}]}


SORTS = {"newest": [("created_at", -1)], "price_asc": [("effective_price", 1)], "price_desc": [("effective_price", -1)],
         "popular": [("sales_count", -1), ("views", -1)], "name": [("name", 1)], "featured": [("featured", -1), ("sort_order", 1), ("created_at", -1)]}


@router.get("/products")
async def list_products(q: Optional[str] = None, category: Optional[str] = None, min_price: Optional[float] = None,
                        max_price: Optional[float] = None, in_stock_only: bool = False, on_sale_only: bool = False,
                        featured: Optional[bool] = None, attr: Optional[str] = Query(None, description="name:value"),
                        sort: str = "featured", page: int = 1, limit: int = 12, ids: Optional[str] = None):
    query = live_query()
    and_ = []
    if q:
        r = safe_regex(q)
        and_.append({"$or": [{"name": r}, {"short_description": r}, {"sku": r}, {"tags": r}]})
    if category:
        cat = await db.categories.find_one({"slug": category, "status": "active"}, {"_id": 0})
        if not cat:
            raise HTTPException(404, "Category not found")
        child_ids = [c["id"] async for c in db.categories.find({"parent_id": cat["id"]}, {"id": 1})]
        query["category_ids"] = {"$in": [cat["id"], *child_ids]}
    if min_price is not None or max_price is not None:
        rng = {}
        if min_price is not None:
            rng["$gte"] = min_price
        if max_price is not None:
            rng["$lte"] = max_price
        query["effective_price"] = rng
    if in_stock_only:
        query["in_stock"] = True
    if on_sale_only:
        query["on_sale"] = True
    if featured is not None:
        query["featured"] = featured
    if attr and ":" in attr:
        n, v = attr.split(":", 1)
        query["attributes"] = {"$elemMatch": {"name": n[:60], "values": v[:60]}}
    if ids:
        query["id"] = {"$in": ids.split(",")[:50]}
    if and_:
        query["$and"] = and_
    skip, limit = page_params(page, limit, 48)
    total = await db.products.count_documents(query)
    docs = await db.products.find(query, {"_id": 0}).sort(SORTS.get(sort, SORTS["featured"])).skip(skip).limit(limit).to_list(limit)
    facet_q = live_query()
    agg = await db.products.aggregate([{"$match": facet_q}, {"$group": {"_id": None, "min": {"$min": "$effective_price"}, "max": {"$max": "$effective_price"}}}]).to_list(1)
    attrs = await db.products.aggregate([{"$match": facet_q}, {"$unwind": "$attributes"}, {"$unwind": "$attributes.values"},
                                         {"$group": {"_id": "$attributes.name", "values": {"$addToSet": "$attributes.values"}}}]).to_list(50)
    return {"items": [public_product(p) for p in docs], "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit,
            "facets": {"price_min": agg[0]["min"] if agg else 0, "price_max": agg[0]["max"] if agg else 0,
                       "attributes": [{"name": a["_id"], "values": sorted(a["values"])} for a in attrs]}}


@router.get("/products/bestsellers")
async def bestsellers(limit: int = 4):
    rows = await db.orders.aggregate([{"$match": {"status": {"$nin": ["cancelled"]}}}, {"$unwind": "$items"},
                                      {"$group": {"_id": "$items.product_id", "units": {"$sum": "$items.quantity"}}},
                                      {"$sort": {"units": -1}}, {"$limit": min(limit, 12)}]).to_list(12)
    ids = [r["_id"] for r in rows]
    prods = {p["id"]: p async for p in db.products.find({**live_query(), "id": {"$in": ids}}, {"_id": 0})}
    return {"items": [public_product(prods[i]) for i in ids if i in prods]}


@router.get("/products/{slug}")
async def get_product(slug: str):
    p = await db.products.find_one({"$or": [{"slug": slug}, {"id": slug}]}, {"_id": 0})
    if not p or not is_live(p):
        raise HTTPException(404, "Product not found")
    out = public_product(p, full=True)
    cats = await db.categories.find({"id": {"$in": p.get("category_ids", [])}}, {"_id": 0, "id": 1, "name": 1, "slug": 1}).to_list(10)
    out["categories"] = cats
    return out


@router.get("/products/{slug}/related")
async def related(slug: str, limit: int = 4):
    p = await db.products.find_one({"slug": slug}, {"_id": 0, "id": 1, "category_ids": 1})
    if not p:
        raise HTTPException(404, "Product not found")
    q = {**live_query(), "id": {"$ne": p["id"]}}
    same = await db.products.find({**q, "category_ids": {"$in": p.get("category_ids", [])}}, {"_id": 0}).limit(limit).to_list(limit)
    if len(same) < limit:
        more = await db.products.find({**q, "id": {"$nin": [p["id"], *[s["id"] for s in same]]}}, {"_id": 0}).limit(limit - len(same)).to_list(limit)
        same += more
    return {"items": [public_product(x) for x in same]}


@router.get("/search/suggest")
async def suggest(q: str = Query(min_length=1, max_length=80)):
    r = safe_regex(q)
    docs = await db.products.find({**live_query(), "$or": [{"name": r}, {"sku": r}, {"tags": r}]}, {"_id": 0}).limit(6).to_list(6)
    cats = await db.categories.find({"status": "active", "name": r}, {"_id": 0, "name": 1, "slug": 1}).limit(4).to_list(4)
    return {"products": [public_product(d) for d in docs], "categories": cats}


@router.get("/categories")
async def categories():
    cats = await db.categories.find({"status": "active"}, {"_id": 0}).sort([("sort_order", 1), ("name", 1)]).to_list(200)
    counts = {r["_id"]: r["n"] for r in await db.products.aggregate([{"$match": live_query()}, {"$unwind": "$category_ids"},
                                                                     {"$group": {"_id": "$category_ids", "n": {"$sum": 1}}}]).to_list(500)}
    for c in cats:
        c["product_count"] = counts.get(c["id"], 0)
    return {"items": cats}


@router.get("/categories/{slug}")
async def category(slug: str):
    c = await db.categories.find_one({"slug": slug, "status": "active"}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Category not found")
    c["children"] = await db.categories.find({"parent_id": c["id"], "status": "active"}, {"_id": 0}).to_list(50)
    return c


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    title: str = Field(default="", max_length=120)
    body: str = Field(min_length=5, max_length=2000)


@router.get("/products/{product_id}/reviews")
async def reviews(product_id: str):
    items = await db.reviews.find({"product_id": product_id, "status": "approved"}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(50)
    return {"items": items}


@router.post("/products/{product_id}/reviews")
async def add_review(product_id: str, body: ReviewIn, user=Depends(get_current_user)):
    bought = await db.orders.find_one({"user_id": user["id"], "status": "delivered", "items.product_id": product_id})
    if not bought:
        raise HTTPException(403, "Only customers who received this product can review it")
    if await db.reviews.find_one({"product_id": product_id, "user_id": user["id"]}):
        raise HTTPException(400, "You have already reviewed this product")
    doc = {"id": new_id(), "product_id": product_id, "user_id": user["id"], "author": user["name"].split(" ")[0], "verified": True,
           **body.model_dump(), "status": "pending", "created_at": now()}
    await db.reviews.insert_one(doc)
    return {"ok": True, "message": "Thank you! Your review will appear after moderation."}

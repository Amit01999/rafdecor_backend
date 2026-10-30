from fastapi import HTTPException
from core.db import db
from core.utils import now

PUBLIC_FILTER = {"status": "published"}


def is_live(p: dict) -> bool:
    if p.get("status") != "published":
        return False
    pa = p.get("publish_at")
    return not pa or pa <= now()


def on_sale(obj: dict) -> bool:
    sp, rp = obj.get("sale_price"), obj.get("price")
    return bool(sp) and rp is not None and 0 < sp < rp


def effective(obj: dict) -> float:
    return float(obj["sale_price"]) if on_sale(obj) else float(obj.get("price") or 0)


def product_min_price(p: dict) -> float:
    if p.get("variants"):
        return min(effective(v) for v in p["variants"])
    return effective(p)


def stock_of(p: dict, v: dict = None):
    """Returns available qty, or None if stock is not tracked."""
    target = v if v is not None else p
    if not p.get("manage_stock"):
        return None if target.get("stock_status", p.get("stock_status", "in_stock")) == "in_stock" else 0
    return int(target.get("stock_quantity") or 0)


def in_stock(p: dict) -> bool:
    if p.get("variants"):
        return any((stock_of(p, v) is None or stock_of(p, v) > 0) for v in p["variants"])
    s = stock_of(p)
    return s is None or s > 0


def variant_label(v: dict) -> str:
    return ", ".join(f"{k}: {val}" for k, val in (v.get("attributes") or {}).items())


async def get_settings() -> dict:
    return await db.settings.find_one({"key": "site"}, {"_id": 0}) or {}


async def evaluate_coupon(code: str, subtotal: float, user_id):
    c = await db.coupons.find_one({"code": code.strip().upper()}, {"_id": 0})
    if not c or not c.get("active"):
        return None, 0.0, "This coupon code is not valid."
    t = now()
    if c.get("starts_at") and c["starts_at"] > t:
        return None, 0.0, "This coupon is not active yet."
    if c.get("expires_at") and c["expires_at"] < t:
        return None, 0.0, "This coupon has expired."
    if c.get("usage_limit") and c.get("used_count", 0) >= c["usage_limit"]:
        return None, 0.0, "This coupon has reached its usage limit."
    if c.get("min_order") and subtotal < c["min_order"]:
        return None, 0.0, f"Minimum order of ৳{c['min_order']:,.0f} required for this coupon."
    if c.get("per_customer_limit"):
        if not user_id:
            return None, 0.0, "Please sign in to use this coupon."
        used = await db.orders.count_documents({"user_id": user_id, "coupon_code": c["code"], "status": {"$ne": "cancelled"}})
        if used >= c["per_customer_limit"]:
            return None, 0.0, "You have already used this coupon."
    disc = subtotal * c["value"] / 100 if c["type"] == "percent" else c["value"]
    if c.get("max_discount"):
        disc = min(disc, c["max_discount"])
    return c, round(min(disc, subtotal), 2), "Coupon applied."


async def build_quote(items: list, coupon_code=None, user_id=None) -> dict:
    ids = list({i["product_id"] for i in items})
    products = {p["id"]: p async for p in db.products.find({"id": {"$in": ids}}, {"_id": 0})}
    lines, issues = [], []
    shipping_by_product = {}
    for it in items:
        p = products.get(it["product_id"])
        qty = int(it.get("quantity") or 0)
        if not p or not is_live(p):
            issues.append({"product_id": it["product_id"], "variant_id": it.get("variant_id"), "code": "unavailable", "message": "This product is no longer available."})
            continue
        v = None
        if p.get("variants"):
            v = next((x for x in p["variants"] if x["id"] == it.get("variant_id")), None)
            if not v:
                issues.append({"product_id": p["id"], "variant_id": it.get("variant_id"), "code": "variant_required", "message": f"Please select an option for {p['name']}."})
                continue
        avail = stock_of(p, v)
        line_issue = None
        if qty < 1:
            line_issue = "Invalid quantity."
        elif avail is not None and avail < qty:
            line_issue = "Out of stock." if avail == 0 else f"Only {avail} available."
        if line_issue:
            issues.append({"product_id": p["id"], "variant_id": v["id"] if v else None, "code": "stock", "message": f"{p['name']}: {line_issue}", "available": avail})
        src = v or p
        unit = effective(src)
        img = (p.get("images") or [{}])[0].get("url")
        lines.append({
            "product_id": p["id"], "variant_id": v["id"] if v else None, "slug": p["slug"], "name": p["name"],
            "variant_label": variant_label(v) if v else None, "sku": (v or {}).get("sku") or p.get("sku"), "image": img,
            "unit_price": unit, "regular_price": float(src.get("price") or unit), "quantity": qty,
            "line_total": round(unit * max(qty, 0), 2), "available": avail, "valid": line_issue is None,
        })
        shipping_by_product[p["id"]] = float(p.get("shipping_charge") or 0)
    subtotal = round(sum(l["line_total"] for l in lines if l["valid"]), 2)
    settings = await get_settings()
    ship_cfg = settings.get("shipping", {})
    charges = list(shipping_by_product.values())
    if not lines:
        shipping = 0.0
    elif ship_cfg.get("mode") == "highest":
        shipping = max(charges)
    else:
        shipping = sum(charges)
    if ship_cfg.get("free_over") and subtotal >= ship_cfg["free_over"]:
        shipping = 0.0
    coupon_info, discount = None, 0.0
    if coupon_code:
        c, discount, msg = await evaluate_coupon(coupon_code, subtotal, user_id)
        coupon_info = {"code": coupon_code.strip().upper(), "valid": c is not None, "message": msg}
    total = round(max(subtotal - discount, 0) + shipping, 2)
    return {"lines": lines, "issues": issues, "subtotal": subtotal, "discount": discount, "shipping": round(shipping, 2),
            "total": total, "coupon": coupon_info, "currency": "BDT"}


async def require_valid_quote(items, coupon_code, user_id) -> dict:
    q = await build_quote(items, coupon_code, user_id)
    if q["issues"] or not q["lines"]:
        raise HTTPException(409, {"message": "Some items in your cart need attention.", "issues": q["issues"]})
    if coupon_code and not q["coupon"]["valid"]:
        raise HTTPException(409, {"message": q["coupon"]["message"], "issues": []})
    return q

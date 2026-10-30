from fastapi import HTTPException
from core.db import db
from core.utils import now, new_id
from services.pricing import product_min_price, on_sale, in_stock


def denormalize(p: dict) -> dict:
    p["effective_price"] = product_min_price(p)
    p["on_sale"] = on_sale(p) or any(on_sale(v) for v in p.get("variants") or [])
    p["in_stock"] = in_stock(p)
    return p


async def refresh_flags(pid: str):
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if p:
        d = denormalize(p)
        await db.products.update_one({"id": pid}, {"$set": {k: d[k] for k in ("effective_price", "on_sale", "in_stock")}})


async def _record(product_id, variant_id, delta, reason, ref=None, actor=None, resulting=None):
    await db.stock_movements.insert_one({
        "id": new_id(), "product_id": product_id, "variant_id": variant_id, "delta": delta, "reason": reason,
        "reference": ref, "actor_email": (actor or {}).get("email"), "resulting_quantity": resulting, "created_at": now(),
    })


async def _apply(product_id, variant_id, delta, guard_min=None):
    """Atomic conditional stock change. guard_min ensures stock >= -delta when decrementing."""
    if variant_id:
        q = {"id": product_id, "manage_stock": True, "variants": {"$elemMatch": {"id": variant_id, **({"stock_quantity": {"$gte": guard_min}} if guard_min else {})}}}
        upd = {"$inc": {"variants.$.stock_quantity": delta}, "$set": {"updated_at": now()}}
    else:
        q = {"id": product_id, "manage_stock": True, **({"stock_quantity": {"$gte": guard_min}} if guard_min else {})}
        upd = {"$inc": {"stock_quantity": delta}, "$set": {"updated_at": now()}}
    res = await db.products.find_one_and_update(q, upd, projection={"_id": 0, "stock_quantity": 1, "variants": 1}, return_document=True)
    if not res:
        return False, None
    if variant_id:
        v = next(x for x in res["variants"] if x["id"] == variant_id)
        return True, v.get("stock_quantity")
    return True, res.get("stock_quantity")


async def reserve_lines(lines: list, order_number: str):
    """Decrement stock for tracked items; rolls back on any failure. Prevents overselling under concurrency."""
    done = []
    tracked = {p["id"]: p.get("manage_stock") async for p in db.products.find({"id": {"$in": [l["product_id"] for l in lines]}}, {"_id": 0, "id": 1, "manage_stock": 1})}
    for l in lines:
        if not tracked.get(l["product_id"]):
            continue
        ok, resulting = await _apply(l["product_id"], l["variant_id"], -l["quantity"], guard_min=l["quantity"])
        if not ok:
            for d in done:
                await _apply(d["product_id"], d["variant_id"], d["quantity"])
            raise HTTPException(409, {"message": f"Sorry, {l['name']} just went out of stock.", "issues": [{"product_id": l["product_id"], "code": "stock"}]})
        done.append(l)
        await refresh_flags(l["product_id"])
        await _record(l["product_id"], l["variant_id"], -l["quantity"], "order_placed", order_number, resulting=resulting)
    return done


async def release_lines(lines: list, reason: str, ref: str, actor=None):
    for l in lines:
        ok, resulting = await _apply(l["product_id"], l.get("variant_id"), l["quantity"])
        if ok:
            await _record(l["product_id"], l.get("variant_id"), l["quantity"], reason, ref, actor, resulting)


async def adjust(product_id, variant_id, delta: int, reason: str, actor: dict, ref=None):
    guard = -delta if delta < 0 else None
    ok, resulting = await _apply(product_id, variant_id, delta, guard_min=guard)
    if not ok:
        raise HTTPException(409, "Adjustment would make stock negative, or stock tracking is disabled for this product.")
    await _record(product_id, variant_id, delta, reason, ref, actor, resulting)
    return resulting

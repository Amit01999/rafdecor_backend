"""Repeatable WordPress/WooCommerce -> MongoDB migration for rafdecor.shop.
Source: public WooCommerce Store API + WP REST API (no credentials required). Safe to rerun: records are matched by source.wp_id.
Usage: cd /app/backend && python -m migration.wp_import [--update-existing]"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from core.db import db  # noqa: E402
from core.utils import now, new_id, html_to_text  # noqa: E402
from services import media  # noqa: E402
from services.inventory import denormalize  # noqa: E402

log = logging.getLogger("migration")
SOURCE = os.environ.get("WP_SOURCE_URL", "https://rafdecor.shop").rstrip("/")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
SKIP_PAGES = {"home", "cart", "checkout", "my-account", "shop", "blog"}
DEFAULT_TEMPLATE_MARKERS = ["This is a sample page", "Suggested text:"]


def money(v, minor=2):
    return round(int(v) / (10 ** minor), 2) if v not in (None, "") else None


def strip_tags(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def clean_html(h):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", h or "", flags=re.S | re.I)
    h = re.sub(r'\s(style|class|id)="[^"]*"', "", h)
    return h.strip()


def extract_specs(desc_html):
    specs = []
    for line in re.split(r"<br\s*/?>|</p>|\n", desc_html or ""):
        t = strip_tags(line)
        if ":" not in t or t.endswith(":"):
            continue
        label, val = [x.strip() for x in t.split(":", 1)]
        if 1 < len(label) <= 40 and 0 < len(val) <= 160:
            specs.append({"label": label, "value": val})
    return specs


async def fetch_json(client, path):
    r = await client.get(f"{SOURCE}{path}", headers=UA, timeout=40)
    r.raise_for_status()
    return r.json(), r.headers


async def fetch_all(client, path):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        data, headers = await fetch_json(client, f"{path}{sep}per_page=100&page={page}")
        out += data
        if page >= int(headers.get("x-wp-totalpages", 1) or 1) or not data:
            return out
        page += 1


async def shipping_charge(client, slug):
    """Delivery charge configured on the product's CartFlows checkout step (flat rate)."""
    try:
        r = await client.get(f"{SOURCE}/step/{slug}/", headers=UA, timeout=40, follow_redirects=True)
        m = re.search(r"Charge:\s*([\d,.]+)", html.unescape(re.sub(r"<[^>]+>", " ", r.text)))
        return float(m.group(1).replace(",", "")) if m else None
    except httpx.HTTPError:
        return None


async def migrate_image(client, url, slug, idx, existing, report):
    prev = next((i for i in existing if i.get("source_url") == url), None)
    if prev and prev.get("public_id"):
        report["images"]["reused"] += 1
        return prev
    rec = {"url": url, "source_url": url, "public_id": None, "alt": ""}
    try:
        head = await client.get(url, headers=UA, timeout=60)
        if head.status_code != 200 or not head.headers.get("content-type", "").startswith("image"):
            report["images"]["failed"].append({"url": url, "reason": f"source HTTP {head.status_code}"})
            return rec
        src_hash = hashlib.sha256(head.content).hexdigest()
    except httpx.HTTPError as e:
        report["images"]["failed"].append({"url": url, "reason": str(e)})
        return rec
    if not media.configured():
        report["images"]["referenced_original"] += 1
        return rec
    pid = f"{idx:02d}-{hashlib.md5(url.encode()).hexdigest()[:10]}"
    try:
        up = await asyncio.to_thread(media.upload, head.content, pid, f"rafdecor/products/{slug}")
        check = await client.get(up["url"], timeout=60)
        if check.status_code == 200 and up.get("bytes"):
            rec.update({"url": up["url"], "public_id": up["public_id"], "width": up.get("width"), "height": up.get("height"), "source_sha256": src_hash})
            report["images"]["uploaded"] += 1
        else:
            report["images"]["failed"].append({"url": url, "reason": "uploaded asset not retrievable"})
    except Exception as e:  # noqa: BLE001
        report["images"]["failed"].append({"url": url, "reason": f"cloudinary: {e}"})
    return rec


async def run_migration(mode: str = "insert_only", actor: str = "system"):
    from pymongo.errors import DuplicateKeyError
    try:
        await db.locks.insert_one({"_id": "migration", "at": now()})
    except DuplicateKeyError:
        lock = await db.locks.find_one({"_id": "migration"})
        if (now() - lock["at"]).total_seconds() < 900:
            return {"status": "skipped", "reason": "another migration is running"}
        await db.locks.update_one({"_id": "migration"}, {"$set": {"at": now()}})
    try:
        return await _run(mode, actor)
    finally:
        await db.locks.delete_one({"_id": "migration"})


async def _run(mode, actor):
    report = {"id": new_id(), "source": SOURCE, "mode": mode, "actor": actor, "started_at": now(), "status": "running",
              "products": {"source": 0, "created": 0, "updated": 0, "unchanged_skipped": 0, "failed": []},
              "categories": {"source": 0, "created": 0, "updated": 0},
              "images": {"source_total": 0, "duplicates_in_source": 0, "uploaded": 0, "reused": 0, "referenced_original": 0, "failed": []},
              "pages": {"imported": [], "skipped": [], "flags": []}, "missing_fields": [], "duplicates": [], "price_reconciliation": [],
              "mappings": [], "customers": {"status": "not_migrated", "reason": "Customer accounts are not exposed by public APIs; WooCommerce REST API keys or a database export are required."},
              "orders": {"status": "not_migrated", "reason": "Historical orders require WooCommerce REST API keys (read) or a database export."},
              "cloudinary_configured": media.configured()}
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            cats = await fetch_all(client, "/wp-json/wc/store/v1/products/categories")
            report["categories"]["source"] = len(cats)
            cat_map = {}
            for c in sorted(cats, key=lambda x: x["parent"]):
                existing = await db.categories.find_one({"source.wp_id": c["id"]})
                doc = {"name": html.unescape(c["name"]), "slug": c["slug"], "description": strip_tags(c.get("description")),
                       "image": (c.get("image") or {}).get("src") if c.get("image") else None,
                       "parent_id": cat_map.get(c["parent"]), "source": {"wp_id": c["id"], "permalink": c.get("permalink")}}
                if existing:
                    cat_map[c["id"]] = existing["id"]
                    if mode == "update_existing":
                        await db.categories.update_one({"id": existing["id"]}, {"$set": {**doc, "updated_at": now()}})
                        report["categories"]["updated"] += 1
                else:
                    cid = new_id()
                    await db.categories.insert_one({"id": cid, **doc, "featured": True, "status": "active", "sort_order": 0, "seo": {"title": "", "description": ""},
                                                    "created_at": now(), "updated_at": now()})
                    cat_map[c["id"]] = cid
                    report["categories"]["created"] += 1

            prods = await fetch_all(client, "/wp-json/wc/store/v1/products")
            report["products"]["source"] = len(prods)
            seen_slugs = set()
            for p in prods:
                try:
                    if p["slug"] in seen_slugs:
                        report["duplicates"].append({"type": "slug", "slug": p["slug"], "wp_id": p["id"]})
                        continue
                    seen_slugs.add(p["slug"])
                    existing = await db.products.find_one({"source.wp_id": p["id"]}, {"_id": 0})
                    if existing and mode == "insert_only":
                        report["products"]["unchanged_skipped"] += 1
                        report["mappings"].append({"wp_id": p["id"], "id": existing["id"], "slug": existing["slug"], "old_url": p["permalink"], "new_path": f"/product/{existing['slug']}"})
                        continue
                    minor = p["prices"].get("currency_minor_unit", 2)
                    urls = [i["src"] for i in p.get("images", [])]
                    report["images"]["source_total"] += len(urls)
                    uniq = list(dict.fromkeys(urls))
                    report["images"]["duplicates_in_source"] += len(urls) - len(uniq)
                    images = []
                    for idx, u in enumerate(uniq):
                        rec = await migrate_image(client, u, p["slug"], idx, (existing or {}).get("images", []), report)
                        rec["alt"] = html.unescape(p["name"])
                        images.append(rec)
                    variants = []
                    for v in p.get("variations", []):
                        vd, _ = await fetch_json(client, f"/wp-json/wc/store/v1/products/{v['id']}")
                        names = {a["taxonomy"] if a.get("taxonomy") else a["name"]: a for a in p["attributes"]}
                        attrs = {}
                        for va in v["attributes"]:
                            a = next((x for x in p["attributes"] if x["name"] == va["name"]), None)
                            term = next((t for t in (a or {}).get("terms", []) if t["slug"] == va["value"]), None)
                            attrs[va["name"]] = term["name"] if term else va["value"]
                        vp = vd["prices"]
                        variants.append({"id": (next((x["id"] for x in (existing or {}).get("variants", []) if x.get("wp_id") == v["id"]), None) or new_id()),
                                         "wp_id": v["id"], "sku": vd.get("sku") or "", "attributes": attrs, "price": money(vp["regular_price"], minor),
                                         "sale_price": money(vp["sale_price"], minor) if vp["sale_price"] != vp["regular_price"] else None,
                                         "stock_quantity": vd.get("low_stock_remaining"), "stock_status": "in_stock" if vd.get("is_in_stock") else "out_of_stock"})
                        del names
                    pr = p["prices"]
                    regular = money(pr["regular_price"], minor)
                    sale = money(pr["sale_price"], minor) if pr["sale_price"] != pr["regular_price"] else None
                    ship = await shipping_charge(client, p["slug"])
                    doc = {
                        "name": html.unescape(p["name"]), "slug": p["slug"], "short_description": strip_tags(p.get("short_description")),
                        "description": html_to_text(p.get("description")), "category_ids": [cat_map[c["id"]] for c in p.get("categories", []) if c["id"] in cat_map],
                        "status": "published", "publish_at": None, "sku": p.get("sku") or None, "price": regular, "sale_price": sale,
                        "manage_stock": False, "stock_quantity": None, "stock_status": "in_stock" if p.get("is_in_stock") else "out_of_stock",
                        "low_stock_threshold": 2, "attributes": [{"name": a["name"], "values": [t["name"] for t in a.get("terms", [])]} for a in p.get("attributes", [])],
                        "variants": variants, "images": images, "specs": extract_specs(p.get("description")), "dimensions": "", "materials": "",
                        "shipping_charge": ship or 0, "tags": [html.unescape(t["name"]) for t in p.get("tags", [])],
                        "seo": {"title": "", "description": strip_tags(p.get("short_description"))[:300]},
                        "source": {"wp_id": p["id"], "permalink": p["permalink"], "type": p["type"], "imported_at": now(), "shipping_charge_source": "cartflows_step" if ship else None},
                    }
                    mat = next((s["value"] for s in doc["specs"] if s["label"] in ("উপাদান", "Material")), "")
                    doc["materials"] = mat
                    denormalize(doc)
                    if existing:
                        await db.products.update_one({"id": existing["id"]}, {"$set": {**doc, "updated_at": now()}})
                        pid = existing["id"]
                        report["products"]["updated"] += 1
                    else:
                        pid = new_id()
                        await db.products.insert_one({"id": pid, **doc, "featured": True, "views": 0, "sales_count": 0, "sort_order": 0,
                                                      "created_at": now(), "updated_at": now()})
                        report["products"]["created"] += 1
                    missing = [f for f in ("sku",) if not doc.get(f)]
                    if not variants:
                        missing.append("stock_quantity (stock not tracked in WooCommerce)")
                    if ship is None:
                        missing.append("shipping_charge")
                    if missing:
                        report["missing_fields"].append({"slug": p["slug"], "fields": missing})
                    src_min = money(pr["price_range"]["min_amount"], minor) if pr.get("price_range") else money(pr["price"], minor)
                    report["price_reconciliation"].append({"slug": p["slug"], "source_price": src_min, "imported_price": doc["effective_price"], "match": abs(src_min - doc["effective_price"]) < 0.01,
                                                           "variants": [{"attrs": v["attributes"], "price": v["price"]} for v in variants], "shipping_charge": ship})
                    report["mappings"].append({"wp_id": p["id"], "id": pid, "slug": p["slug"], "old_url": p["permalink"], "new_path": f"/product/{p['slug']}"})
                except Exception as e:  # noqa: BLE001
                    log.exception("product %s failed", p.get("id"))
                    report["products"]["failed"].append({"wp_id": p.get("id"), "slug": p.get("slug"), "error": str(e)})

            pages = await fetch_all(client, "/wp-json/wp/v2/pages?_fields=id,slug,title,content,link,modified")
            for pg in pages:
                if pg["slug"] in SKIP_PAGES:
                    report["pages"]["skipped"].append({"slug": pg["slug"], "reason": "functional page rebuilt natively"})
                    continue
                body = clean_html(pg["content"]["rendered"])
                if any(m in body for m in DEFAULT_TEMPLATE_MARKERS):
                    report["pages"]["flags"].append({"slug": pg["slug"], "flag": "Contains WordPress/WooCommerce default template text — review before launch"})
                if not strip_tags(body):
                    report["pages"]["flags"].append({"slug": pg["slug"], "flag": "Empty on source site — content must be written by the business"})
                existing = await db.pages.find_one({"slug": pg["slug"]})
                if existing and mode == "insert_only":
                    continue
                await db.pages.update_one({"slug": pg["slug"]}, {"$set": {"title": html.unescape(pg["title"]["rendered"]), "body": body, "status": "published",
                                                                          "source": {"wp_id": pg["id"], "link": pg["link"]}, "updated_at": now()},
                                                                 "$setOnInsert": {"created_at": now(), "seo": {}}}, upsert=True)
                report["pages"]["imported"].append(pg["slug"])
        report["status"] = "completed"
    except Exception as e:  # noqa: BLE001
        log.exception("migration failed")
        report["status"] = "failed"
        report["error"] = str(e)
    report["finished_at"] = now()
    report["destination"] = {"products": await db.products.count_documents({"source.wp_id": {"$exists": True}}),
                             "categories": await db.categories.count_documents({"source.wp_id": {"$exists": True}}),
                             "pages": await db.pages.count_documents({})}
    await db.migration_reports.insert_one(dict(report))
    out = Path(__file__).resolve().parent / "reports"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = asyncio.run(run_migration("update_existing" if "--update-existing" in sys.argv else "insert_only", "cli"))
    print(json.dumps({k: r[k] for k in ("status", "products", "categories", "images", "destination")}, default=str, indent=2))

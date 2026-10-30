from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
from urllib.parse import urlparse  # noqa: E402
from fastapi import FastAPI, APIRouter, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
from core.db import db, client, create_indexes  # noqa: E402
from core.security import seed_roles_and_admin  # noqa: E402
from migration.seed import seed_settings  # noqa: E402
from routes import auth, catalog, checkout, account, admin_catalog, admin_orders, admin_ops  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rafdecor")

app = FastAPI(title="RAF DECOR Commerce API", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
v1 = APIRouter(prefix="/api/v1")
for r in (auth.router, catalog.router, checkout.router, account.router, admin_catalog.router, admin_orders.router, admin_orders.pay_router, admin_ops.router):
    v1.include_router(r)


@v1.get("/health", tags=["system"])
async def health():
    await db.command("ping")
    return {"status": "ok"}


@v1.get("/seo/sitemap.xml", tags=["system"])
async def sitemap(request: Request):
    base = os.environ.get("PUBLIC_SITE_URL") or f"{request.url.scheme}://{request.headers.get('x-forwarded-host', request.url.netloc)}"
    urls = ["/", "/shop", "/contact-us", "/track-order"]
    urls += [f"/product/{p['slug']}" async for p in db.products.find({"status": "published"}, {"slug": 1})]
    urls += [f"/product-category/{c['slug']}" async for c in db.categories.find({"status": "active"}, {"slug": 1})]
    urls += [f"/{p['slug']}" async for p in db.pages.find({"status": "published"}, {"slug": 1})]
    body = "".join(f"<url><loc>{base}{u}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>', media_type="application/xml")


app.include_router(v1)

origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    """Cookie-authenticated state-changing requests must come from our own origin (CSRF defence alongside SameSite=Lax)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and (request.cookies.get("access_token") or request.cookies.get("refresh_token")):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if urlparse(origin).netloc != host and origin not in origins:
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@app.on_event("startup")
async def startup():
    await create_indexes()
    await seed_roles_and_admin()
    await seed_settings()
    # Serverless instances can be frozen as soon as a response finishes, so do not
    # launch a detached import task during Vercel cold starts. Run the importer from
    # the admin migration screen after deployment instead.
    if os.environ.get("VERCEL") != "1" and await db.products.count_documents({}) == 0:
        from migration.wp_import import run_migration
        asyncio.create_task(run_migration("insert_only", "startup"))


@app.on_event("shutdown")
async def shutdown():
    client.close()
